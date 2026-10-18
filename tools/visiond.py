"""常驻视觉服务：Qwen2.5-VL-3B 只加载一次，连接器每次问问题只是发一张小图。

为什么要有它：连接器每个作业都是一个新进程，模型每进程重载 ~20 秒；3B 模型本身
一次问答只要 1 秒左右。把加载提到进程外之后，"看一眼"从 20 多秒变成 1~2 秒。

    python tools/visiond.py --start     # 后台起服务，等它就绪再返回
    python tools/visiond.py --status    # 在不在、占多少显存
    python tools/visiond.py --stop
    python tools/visiond.py --serve     # 前台跑（调试用）

HTTP（只监听 127.0.0.1）：
    GET  /health            -> {"ready":bool,"model":...,"vram_mb":n}
    POST /see   {"png_b64":..., "q":"问题", "mt":32} -> {"a":"答案","ms":n}

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
_lock = threading.Lock()
_state = {"ready": False, "model": None, "proc": None, "vram_mb": 0}


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


def _answer(png: bytes, question: str, mt: int) -> str:
    import torch
    from PIL import Image

    model, proc = _state["model"]
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
                self._send(200, {"ready": _state["ready"], "model": MODEL_ID,
                                 "vram_mb": _state["vram_mb"], "pid": os.getpid()})
            else:
                self._send(404, {"err": "unknown"})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/see"):
                self._send(404, {"err": "unknown"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                png = base64.b64decode(req["png_b64"])
                t0 = time.time()
                with _lock:                      # 单卡，串行；并发请求排队而不是抢显存
                    if not _state["ready"]:
                        self._send(503, {"err": "loading"})
                        return
                    a = _answer(png, req.get("q", "描述这张图"), req.get("mt", 32))
                self._send(200, {"a": a, "ms": int((time.time() - t0) * 1000)})
            except Exception as exc:             # noqa: BLE001 — 服务不因单次请求死掉
                self._send(500, {"err": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *a) -> None:       # 安静：别刷屏
            pass

    srv = ThreadingHTTPServer((HOST, PORT), H)
    threading.Thread(target=_load, daemon=True).start()
    srv.serve_forever()


def _ping(timeout: float = 0.4) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:                            # noqa: BLE001
        return None


def _port_free() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) != 0


def start(wait_s: int = 120) -> dict:
    """后台起服务并等就绪。已经在跑就直接返回。"""
    h = _ping()
    if h:
        return h
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
    else:
        h = _ping(1.0)
        print(json.dumps(h or {"ready": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
