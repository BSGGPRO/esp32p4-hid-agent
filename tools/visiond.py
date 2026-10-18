"""常驻视觉服务：Qwen2.5-VL-3B 只加载一次，连接器每次问问题只是发一张小图。

为什么要有它：连接器每个作业都是一个新进程，模型每进程重载 ~20 秒；3B 模型本身
一次问答只要 1 秒左右。把加载提到进程外之后，"看一眼"从 20 多秒变成 1~2 秒。

    python tools/visiond.py --start     # 后台起服务，等它就绪再返回
    python tools/visiond.py --status    # 在不在、占多少显存
    python tools/visiond.py --stop
    python tools/visiond.py --serve     # 前台跑（调试用）

HTTP（只监听 127.0.0.1）：
    GET  /health            -> {"ready":bool,"ocr":bool,"model":...,"vram_mb":n}
    POST /see   {"png_b64":..., "q":"问题", "mt":32} -> {"a":"答案","ms":n}
    POST /ocr   {"png_b64":...}                      -> {"rows":[...],"ms":n}

OCR 也常驻在这里（RapidOCR 只初始化一次）：一次 OCR 从每次 2.7 秒降到一两百毫秒。
两个引擎各用一把锁，OCR 走 CPU、VLM 走显卡，互不排队。

没有密钥校验：它只绑定回环地址，不上网。别把它暴露到 0.0.0.0。
"""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"
PORT = int(os.environ.get("DSV_PORT", "8771"))
HOST = "127.0.0.1"
# 空转多久就把显卡还回去。用户要打游戏时，2.5 GB 显存比"下次问话慢 20 秒"值钱得多。
IDLE_UNLOAD = float(os.environ.get("DSV_IDLE_UNLOAD", "180"))
_lock = threading.Lock()          # 显卡：一次只让一个推理进去
_ocr_lock = threading.Lock()      # CPU：OCR 独立排队
_load_lock = threading.Lock()     # 加载/卸载互斥
_state = {"ready": False, "model": None, "vram_mb": 0, "ocr": None, "ocr_ready": False,
          "last_used": 0.0}


def _free_model() -> None:
    """把视觉模型和它的显存还回去（OCR 是 CPU 的，留着无所谓）。"""
    with _load_lock:
        if not _state["ready"]:
            return
        _state["model"] = None
        _state["ready"] = False
        _state["vram_mb"] = 0
        try:
            import gc

            import torch

            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:                   # noqa: BLE001
            pass


def _ensure_loaded() -> None:
    """要用了才加载（被空转卸载过就再装回来）。"""
    if _state["ready"]:
        return
    with _load_lock:
        if _state["ready"]:
            return
        _load()


def _fullscreen() -> bool:
    """前台窗口是不是真的"全屏"（盖住整块屏幕，连任务栏一起盖）。

    判据用监视器矩形而不是屏幕尺寸：**最大化窗口盖不住任务栏**，全屏游戏/全屏视频会。
    这样窗口化的游戏就算铺满桌面也不会被误判，DPI 缩放也不影响比较（两边同一套坐标）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        u = ctypes.WinDLL("user32", use_last_error=True)
        h = u.GetForegroundWindow()
        if not h:
            return False
        mon = u.MonitorFromWindow(wintypes.HWND(h), 2)      # MONITOR_DEFAULTTONEAREST
        if not mon:
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        r = wintypes.RECT()
        if not u.GetWindowRect(wintypes.HWND(h), ctypes.byref(r)):
            return False
        m = mi.rcMonitor
        return (r.left <= m.left + 2 and r.top <= m.top + 2
                and r.right >= m.right - 2 and r.bottom >= m.bottom - 2)
    except Exception:                       # noqa: BLE001 — 判不出来就当不是，别误伤
        return False


GAME_UNLOAD_AFTER = 5.0          # 全屏时：最后一次用完 5 秒就卸（比空转阈值激进得多）


def _game_mode() -> bool:
    """全屏 = 用户很可能在游戏/看全屏视频，这时候不抢显存。可用 DSV_GAME_MODE=0 关掉。"""
    return os.environ.get("DSV_GAME_MODE", "1") != "0" and _fullscreen()


def _idle_watch() -> None:
    """把显存还回去：空转 IDLE_UNLOAD 秒，或者一进全屏就尽快卸。"""
    while True:
        time.sleep(5)
        if not _state["ready"]:
            continue
        idle = time.time() - (_state["last_used"] or time.time())
        if _game_mode() and idle >= GAME_UNLOAD_AFTER:
            _free_model()
        elif IDLE_UNLOAD and idle >= IDLE_UNLOAD:
            _free_model()


def _load():
    import torch
    from transformers import (AutoProcessor, BitsAndBytesConfig,
                              Qwen2_5_VLForConditionalGeneration)

    q = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                           bnb_4bit_compute_dtype=torch.bfloat16)
    m = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        MODEL_ID, torch_dtype=torch.bfloat16, device_map="cuda", quantization_config=q).eval()
    _state["model"] = (m, AutoProcessor.from_pretrained(MODEL_ID))
    _state["vram_mb"] = int(torch.cuda.memory_allocated() / 1048576)
    _state["ready"] = True
    _state["last_used"] = time.time()
    # 先跑一次空的：把 CUDA 上下文、cuBLAS 句柄这些一次性开销在启动时付掉，
    # 否则第一句真问题会多等一两秒。
    try:
        im = _tiny_png()
        _answer(im, "what", 2)
    except Exception:                       # noqa: BLE001 — 预热失败不影响正确性
        pass


def _tiny_png() -> bytes:
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (64, 64), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def _ocr_threads() -> int:
    """onnxruntime 默认吃满所有核，小图上反而更慢：本机 16 核实测比 8 核慢 30%。"""
    return max(2, (os.cpu_count() or 4) // 2)


def _load_ocr() -> None:
    """OCR 引擎也常驻。它只吃 CPU，和显卡那套互不干扰。

    det_limit_type="max" 是这里的关键：RapidOCR 默认按"短边缩到 736"，
    一块 208×50 的小图会被放大 15 倍再检测——实测 625 ms，改完 64 ms，
    同一张图文字一个字没变。大图按长边压到 1280。
    """
    from rapidocr_onnxruntime import RapidOCR

    eng = RapidOCR(det_limit_type="max", det_limit_side_len=1280,
                   intra_op_num_threads=_ocr_threads())
    try:                                    # 用带字的图预热，把两种模型形状的开销先付掉
        import numpy as np

        eng(_warm_image())
    except Exception:                       # noqa: BLE001
        pass
    _state["ocr"] = eng
    _state["ocr_ready"] = True


def _warm_image():
    """一张 1280×480、三十来行字的白图：让检测/识别两个模型都把图算一遍。"""
    import numpy as np
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (1280, 480), (255, 255, 255))
    d = ImageDraw.Draw(im)
    line = "The quick brown fox jumps over the lazy dog 0123456789 中文测试"
    for i in range(30):
        d.text((8, 4 + i * 15), line[: 20 + (i % 30)], fill=(20, 20, 20))
    return np.array(im)


def _ocr_rows(png: bytes) -> list:
    import numpy as np
    from PIL import Image

    if _state["ocr"] is None and not _state["ocr_ready"]:
        with _ocr_lock:
            if _state["ocr"] is None:
                _load_ocr()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    with _ocr_lock:
        res, _ = _state["ocr"](np.array(im))
    rows = []
    for box, text, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append({"text": text, "score": float(score),
                     "x": float(min(xs)), "y": float(min(ys)),
                     "w": float(max(xs) - min(xs)), "h": float(max(ys) - min(ys))})
    return rows


def _answer(png: bytes, question: str, mt: int) -> str:
    import torch
    from PIL import Image

    model, proc = _state["model"]
    _state["last_used"] = time.time()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": question}]}]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = proc(text=[text], images=[im], return_tensors="pt").to("cuda")
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=int(mt), do_sample=False)
    return proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()


def _serve() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/health"):
                idle = int(time.time() - (_state["last_used"] or time.time())) if _state["ready"] else 0
                self._send(200, {"ready": _state["ready"], "ocr": _state["ocr_ready"],
                                 "model": MODEL_ID, "vram_mb": _state["vram_mb"],
                                 "idle_s": idle, "idle_unload_s": int(IDLE_UNLOAD),
                                 "fullscreen": _fullscreen(), "game_mode": _game_mode(),
                                 "pid": os.getpid()})
            else:
                self._send(404, {"err": "unknown"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/see", "/ocr", "/warm", "/unload"):
                self._send(404, {"err": "unknown"})
                return
            try:
                if path == "/unload":            # 立刻把显存还回去（打游戏前用）
                    _free_model()
                    self._send(200, {"ready": _state["ready"], "vram_mb": _state["vram_mb"]})
                    return
                if path == "/warm":              # 预热：后台加载，请求立刻返回
                    threading.Thread(target=_ensure_loaded, daemon=True).start()
                    self._send(200, {"loading": not _state["ready"]})
                    return
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                png = base64.b64decode(req["png_b64"])
                t0 = time.time()
                if path == "/ocr":
                    rows = _ocr_rows(png)
                    self._send(200, {"rows": rows, "ms": int((time.time() - t0) * 1000)})
                    return
                _ensure_loaded()                 # 被空转卸载过就在这里装回来
                with _lock:                      # 单卡，串行；并发请求排队而不是抢显存
                    a = _answer(png, req.get("q", "描述这张图"), req.get("mt", 32))
                self._send(200, {"a": a, "ms": int((time.time() - t0) * 1000)})
            except Exception as exc:             # noqa: BLE001 — 服务不因单次请求死掉
                self._send(500, {"err": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *a) -> None:       # 安静：别刷屏
            pass

    srv = ThreadingHTTPServer((HOST, PORT), H)
    threading.Thread(target=_load, daemon=True).start()
    threading.Thread(target=_load_ocr, daemon=True).start()   # 两个引擎并行加载
    threading.Thread(target=_idle_watch, daemon=True).start()  # 空转就把显存还回去
    srv.serve_forever()


def _ping(timeout: float = 0.4) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:                            # noqa: BLE001
        return None


def _post(path: str, body: dict | None = None, timeout: float = 200) -> dict | None:
    try:
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:                            # noqa: BLE001
        return None


def warm(wait_s: int = 120) -> dict:
    """确保模型在显存里：没加载就让服务去加载，等它就绪。"""
    h = _ping(1.0)
    if not h:
        return start(wait_s)
    if h.get("ready"):
        return h
    _post("/warm", timeout=5)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        h = _ping(1.0)
        if h and h.get("ready"):
            return h
        time.sleep(0.5)
    raise RuntimeError(f"视觉模型 {wait_s}s 内没加载完")


def unload() -> dict:
    """立刻释放显存（打游戏前跑一下）。进程和 OCR 还留着。"""
    r = _post("/unload", timeout=30)
    return r or {"ready": None}


def _port_free() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) != 0


def start(wait_s: int = 120) -> dict:
    """后台起服务并等就绪。已经在跑就直接返回；跑了但模型被卸了就让它装回来。"""
    h = _ping()
    if h:
        if h.get("ready"):
            return h
        return warm(wait_s)
    import subprocess
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags,
                     cwd=os.path.dirname(os.path.abspath(__file__)))
    t0 = time.time()
    while time.time() - t0 < wait_s:
        h = _ping(1.0)
        if h and h.get("ready"):
            return h
        time.sleep(0.5)
    raise RuntimeError(f"视觉服务 {wait_s}s 内没就绪")


def stop() -> bool:
    h = _ping()
    if not h:
        return False
    pid = h.get("pid")
    try:
        os.kill(int(pid), 9)
        return True
    except Exception:                            # noqa: BLE001
        return False


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "--status"
    if cmd == "--serve":
        _serve()
    elif cmd == "--start":
        print(json.dumps(start(), ensure_ascii=False))
    elif cmd == "--stop":
        print("stopped" if stop() else "not running")
    elif cmd == "--unload":
        print(json.dumps(unload(), ensure_ascii=False))
    else:
        h = _ping(1.0)
        print(json.dumps(h or {"ready": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
