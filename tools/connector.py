#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/connector.py — 唯一入口：从 stdin 收一份 JSON 作业，用板子执行，打印结果。

**不再为每个任务新建脚本**。要做的事写成 JSON 从管道喂进来，执行完就没了：

    echo {"steps":[{"op":"focus","title":"微信"},{"op":"key","combo":"ctrl+f"}]} ^
        | python tools/connector.py

也可以直接调 main 里的 run_job(dict) 拿返回字典。

作业格式::

    {"steps": [ {...}, {...} ]}          或直接 [ {...} ]

支持的操作（每条都可带 "require"，不满足就整单失败退出，绝不带病往下走）：

    restore   title=<子串> | exe=<进程名>   还原最小化/托盘里的窗口（不移动、不改大小）
    focus     title=<子串> | exe=<进程名>   切到前台
    title     [title=<子串> | exe=<进程名>]  报告窗口标题（facts 用 Win32，不靠模型）
    ui        [title=.. | exe=..] [buttons=true]
                                          把整个窗口读成结构：文字行 + 可疑按钮，都带屏幕坐标
                                          PrintWindow 渲染——窗口被挡住也能读
    key       combo=<ctrl+t>              按组合键
    type      text=<...>                  敲字（只支持能直接映射的键）
    click     x= y=  [button=left|right]  点坐标；或 on=<变量名> 点某个 find 结果
    move      x= y=  或 on=<变量名>        移动光标
    point     x= y=  或 on=<变量名>        闭环定位：自己纠偏到目标（比 move 准）
    scroll    amount=<n>                  滚轮
    wait      ms=<n>                      等
    ocr       rect=[x0,y0,x1,y1]          逐行文字+坐标（常驻 RapidOCR，小框约 120 ms）
    find      text=<...> [rect=..] [thresh=0.7] [as=名字]
                                          在区域里找一行字，存成变量（含中心点）
    see       q=<问题> [rect=..] [speed=fast|normal|fine] [longest=N] [max_new_tokens=64]
                                          speed: fast=长边480 / normal=640(默认) / fine=1280
                                          默认只要一句话（20 字内）；要细节就加大 max_new_tokens
    check     q=<问题> [rect=..] [speed=fast]   是非题：只让它回「是/否」，结果在 yes 字段
    vision    [action=status|start|stop|unload] 常驻视觉服务（unload 立刻还显存，打游戏前用）

    recipe    name=wechat-send  to=<聊天> pieces=[py:拼音, raw:ascii]
              微信发消息（标题核对 → 清空输入框 → 拼音选字/直接敲 → 发前核对 → 回车 → 发后核对）
    recipe    name=open-url  url=<网址> [expect=标题关键字]
              交给系统打开（Win+R，不用地址栏，避免自动补全抢回车），最后核对标题

按进程名找窗口：播放器这类程序标题就是当前内容（网易云的标题是歌名），
按应用名找不到，用 exe=cloudmusic 这种进程名找。

变量（as=）之后可以用 on=<名字>（click / move / point 都认）。

自检：`python tools/connector.py --selfcheck` 一次报出屏幕/OCR/板子/指针/视觉五项状态。

铁律照旧：不动用户窗口大小位置；核对不上就停；退出前一定释放板子。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

# 模型已经缓存在本地：离线加载，别再跑去 huggingface 上重试（那边不通，一次要等几分钟）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "pc"))
sys.path.insert(0, HERE)

_spec = importlib.util.spec_from_file_location("draw_rage", os.path.join(HERE, "draw-rage.py"))
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)
import eyes  # noqa: E402
from brain import Brain, screen_size  # noqa: E402

TMP = os.environ.get("TEMP", ".")
ZERO = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_ocr_engine = None
_vlm = None      # (model, processor)，只有 see 用到才加载
_last = {}       # 最近一次的耗时等观测值，给 vision 操作回报


def clean(s: str) -> str:
    return (s or "").translate(ZERO).lower()


def tight(s: str) -> str:
    return "".join(ch for ch in (s or "") if not ch.isspace())


def alnum(s: str) -> str:
    """只留字母数字汉字：OCR 把 deepseek 读成 deep'seek / deep seek，标点空格不能算差异。"""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def cjk(s: str) -> str:
    return "".join(ch for ch in tight(s) if "\u4e00" <= ch <= "\u9fff")


def ratio(target: str, text: str) -> float:
    """最长公共子序列命中率——OCR 会把「件」读成「亻牛」，不能死等字符串。"""
    t, s = tight(target), tight(text)
    if not t:
        return 0.0
    prev = [0] * (len(s) + 1)
    for ch in t:
        cur = [0] * (len(s) + 1)
        for j, c in enumerate(s, 1):
            cur[j] = prev[j - 1] + 1 if c == ch else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1] / float(len(t))


# ------------------------------------------------------------------ 窗口（Win32）
def windows():
    return dr.list_windows()


_EXE: dict[int, str] = {}


def exe_of(pid: int) -> str:
    """进程可执行文件名（小写）。"""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        n = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k.CloseHandle(h)


def _exe_cached(pid: int) -> str:
    if pid not in _EXE:
        _EXE[pid] = exe_of(pid)
    return _EXE[pid]


def pick(title: str = "", exe: str | None = None) -> dict | None:
    """按标题子串、或按 exe=进程名 挑窗口；可见的优先，其次是能拿到句柄的。

    播放器这类程序窗口标题就是当前内容（网易云的标题是歌名 "Daylight - Seredris"），
    按应用名永远找不到——所以也能按进程名找：{"op": "focus", "exe": "cloudmusic"}。
    """
    want = clean(title)
    best = None
    for w in windows():
        ok = (exe.lower() in _exe_cached(w["pid"])) if exe else (want in clean(w["title"]))
        if not ok:
            continue
        iconic = w["rect"][0] <= -30000
        rank = (not iconic, w["rect"][2] - w["rect"][0])
        if best is None or rank > best[0]:
            best = (rank, w)
    return best[1] if best else None


def pick_st(st: dict) -> dict:
    """配置里怎么指窗口：title=<标题子串>、exe=<进程名>，两个都不给就取最宽的那个窗口。"""
    w = pick(st.get("title", ""), st.get("exe"))
    if not w:
        raise RuntimeError("没找到窗口（title=%r exe=%r）" % (st.get("title"), st.get("exe")))
    return w


def restore(w: dict) -> None:
    import ctypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    h = ctypes.c_void_p(int(w["hwnd"]))
    if u.IsIconic(h):
        u.ShowWindow(h, 9)     # SW_RESTORE
    u.ShowWindow(h, 5)         # SW_SHOW
    u.SetForegroundWindow(h)


# --------------------------------------------------------------------- 眼睛
# 一次作业里 3 个动作经常都要看屏幕（find→click→核对）。截一张 2560×1440 要 150 ms，
# 所以同一张画面反复用，直到有输入动作或 wait 说明屏幕可能变了才重截。
_cap = {"im": None, "t": 0.0}
CAP_MAX_AGE = 5.0          # 秒；超龄就当过期，宁可多截一次也不用旧画面骗自己
_CACHE_KILL = {"key", "type", "click", "move", "scroll", "drag", "drag_path",
               "hold", "point", "wait_change", "wait", "restore", "focus",
               "recipe", "clip"}


def _invalidate() -> None:
    _cap["im"] = None


def _tmp(name: str) -> str:
    """每进程一个临时文件名：两个连接器同时跑时不能抢同一个文件（会把对方的图读坏）。"""
    return os.path.join(TMP, "_conn_%d_%s.png" % (os.getpid(), name))


def _cleanup_tmp() -> None:
    for name in ("full", "crop", "see"):
        p = _tmp(name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _screen():
    """整屏画面（带缓存）。"""
    now = time.time()
    if _cap["im"] is not None and now - _cap["t"] <= CAP_MAX_AGE:
        return _cap["im"]
    from PIL import Image

    path = eyes.capture(_tmp("full")).path
    im = Image.open(path).convert("RGB")
    _cap["im"], _cap["t"] = im, now
    return im


def _ocr_daemon(im):
    """把裁剪图发给常驻服务做 OCR；服务不在就返回 None（调用方退回落本地引擎）。"""
    import base64
    import io
    import urllib.request

    try:
        import visiond

        h = visiond._ping()
        if not h or not h.get("ocr"):
            visiond.start()                  # 只保证服务起来了（约 1 秒）
            # 「服务起来了」不等于「OCR 引擎装好了」（还要约 4 秒）。等它真能干活再发图，
            # 否则会退回本地引擎——在本进程里白建一个 OCR 引擎，多花好几秒。
            t0 = time.time()
            while time.time() - t0 < 15:
                h = visiond._ping(1.0)
                if h and h.get("ocr"):
                    break
                time.sleep(0.15)
            if not (h and h.get("ocr")):
                return None
        buf = io.BytesIO()
        im.save(buf, "PNG")
        body = json.dumps({"png_b64": base64.b64encode(buf.getvalue()).decode()}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/ocr" % visiond.PORT, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        if out.get("err") or "rows" not in out:
            return None
        _last["ocr_ms"] = out.get("ms")
        return out["rows"]
    except Exception:  # noqa: BLE001 — 服务只是加速手段，不该成为失败点
        return None


def _ocr_local(im) -> list:
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            # 打包版控制器不带 OCR（本地冷启动要 6.6 s，常驻服务只要 0.12 s）。
            raise RuntimeError(
                "没有可用的 OCR：视觉服务（8771）没在跑，本地也没装 rapidocr。"
                "先在装了依赖的机器上运行 `python tools/visiond.py`。") from e
        # 和 visiond 同一套参数：短边放大是 OCR 卡顿的主因（见 visiond._load_ocr）
        _ocr_engine = RapidOCR(det_limit_type="max", det_limit_side_len=1280,
                               intra_op_num_threads=max(2, (os.cpu_count() or 4) // 2))
    import numpy as np

    res, _ = _ocr_engine(np.array(im))
    rows = []
    for box, text, score in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append({"text": text, "score": float(score),
                     "x": float(min(xs)), "y": float(min(ys)),
                     "w": float(max(xs) - min(xs)), "h": float(max(ys) - min(ys))})
    return rows


def _ocr_rows(im) -> list:
    """对一张图识字，坐标是图内坐标。"""
    rows = _ocr_daemon(im)
    if rows is None:
        rows = _ocr_local(im)
    return rows


def ocr(rect, fresh: bool = False) -> list:
    """RapidOCR：一次拿回所有行的文字和屏幕坐标（坐标已经换算回整屏）。

    fresh=True 强制重截——轮询「输入完没有」时要看新画面，不能吃 5 秒内的缓存。
    """
    x0, y0, x1, y1 = [int(v) for v in rect]
    if fresh:
        _invalidate()
    im = _screen().crop((x0, y0, x1, y1))
    rows = _ocr_rows(im)
    for r in rows:
        r["x"] += x0
        r["y"] += y0
    return rows


def poll(fn, timeout: float = 2.5, every: float = 0.15):
    """反复试到 fn 返回真值为止，超时返回 None。

    替掉「固定 sleep 等界面反应」：界面快就立刻继续，慢就等够。
    """
    end = time.time() + float(timeout)
    while True:
        v = fn()
        if v:
            return v
        if time.time() >= end:
            return None
        time.sleep(every)


def _hwnd_shot(hwnd: int):
    """PrintWindow 渲染整个窗口（被别的窗口挡住也能拿到真实画面）。返回 (图, 屏幕原点)。"""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    h = ctypes.c_void_p(int(hwnd))
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    w, ht = r.right - r.left, r.bottom - r.top
    if w <= 0 or ht <= 0:
        raise RuntimeError("窗口尺寸 %dx%d，没法渲染" % (w, ht))

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hdc = u.GetWindowDC(h)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, ht)
    g.SelectObject(mem, bmp)
    try:
        if not u.PrintWindow(h, mem, 2):            # PW_RENDERFULLCONTENT
            raise RuntimeError("PrintWindow 渲染失败")
        bi = BIH()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth, bi.biHeight = w, -ht
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * ht * 4)
        g.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(bi), 0)
        im = Image.frombuffer("RGBA", (w, ht), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(h, hdc)
    return im, (r.left, r.top)


def _blobs(im) -> list:
    """亮块粗定位：返回 [(x, y, w, h, 面积), ...]（图内坐标）。

    按钮/图标没有文字，OCR 抓不到，但它们是比背景亮的一块——按亮度找块就出来了。
    """
    import numpy as np
    from PIL import Image

    g = im.convert("L")
    s = max(1, int(max(g.size) / 420.0))            # 降到 ~420 像素宽再找，够用且快
    a = np.asarray(g.resize((max(1, g.width // s), max(1, g.height // s)), Image.BILINEAR))
    a = a.astype(np.float32)
    m = a >= (a.mean() + 0.9 * a.std() + 8.0)
    h, w = m.shape
    seen = np.zeros((h, w), dtype=bool)
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if not m[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            minx = maxx = x0
            miny = maxy = y0
            n = 0
            while stack:
                cy, cx = stack.pop()
                n += 1
                if cx < minx:
                    minx = cx
                if cx > maxx:
                    maxx = cx
                if cy < miny:
                    miny = cy
                if cy > maxy:
                    maxy = cy
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            out.append((minx * s, miny * s, (maxx - minx + 1) * s, (maxy - miny + 1) * s, n * s * s))
    return out


def _blank(im) -> bool:
    """几乎一片纯色：PrintWindow 对浏览器这类自绘窗口经常只给空白。"""
    import numpy as np

    a = np.asarray(im.convert("L").resize((64, 64))).astype(float)
    return float(a.std()) < 6.0


def _screen_shot(rect):
    """从整屏截图里裁出这块（窗口真的露在屏幕上时，这才是真像素）。"""
    W, H = screen_size()
    x0, y0, x1, y1 = [int(v) for v in rect]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 - x0 < 60 or y1 - y0 < 40:
        return None
    return _screen().crop((x0, y0, x1, y1)), (x0, y0)


def ui_map(w: dict, with_buttons: bool = True, max_items: int = 60) -> dict:
    """把一个窗口读成结构：文字行 + 疑似按钮，全部带屏幕坐标。

    为什么要有这个：图标按钮没有文字，ocr 看不见；整屏截图又被自己（或者别的窗口）挡住。
    这里用 PrintWindow 渲染窗口本身，再按亮度找块——「这界面上有什么、能点什么」
    一条命令拿全，不用再猜一个界面半小时。

    但 Chromium 这类自绘窗口（浏览器、Electron 程序）PrintWindow 只给空白，
    所以渲染出来是空白时，退回截屏那块（窗口在前面时屏幕里就是它的真像素）。
    """
    if w["rect"][0] <= -30000:
        raise RuntimeError("窗口 %r 是最小化的，先 restore / focus 再读界面" % w["title"])
    im, origin = _hwnd_shot(w["hwnd"])
    source = "printwindow"
    if _blank(im):
        shot = _screen_shot(w["rect"])
        if shot:
            im, origin = shot
            source = "screen"
    if im.width < 120 or im.height < 60:
        raise RuntimeError("窗口 %r 太小（%dx%d），读不出界面" % (w["title"], im.width, im.height))
    x0, y0 = origin
    texts = []
    for r in _ocr_rows(im):
        texts.append({
            "text": r["text"], "x": int(r["x"]), "y": int(r["y"]),
            "w": int(r["w"]), "h": int(r["h"]),
            "cx": x0 + int(r["x"] + r["w"] // 2), "cy": y0 + int(r["y"] + r["h"] // 2),
        })
    out = {"hwnd": int(w["hwnd"]), "title": w["title"], "origin": [x0, y0],
           "size": [im.width, im.height], "texts": texts[:max_items],
           "buttons": [], "source": source}
    if not with_buttons:
        return out
    boxes = [(t["x"], t["y"], t["w"], t["h"]) for t in texts]
    for bx, by, bw, bh, _area in _blobs(im):
        if not (10 <= bw <= 90 and 10 <= bh <= 90):
            continue                                # 太细是笔画，太大是整块背景
        if not 0.35 <= bw / float(bh) <= 2.8:
            continue
        if any(bx < tx + tw and tx < bx + bw and by < ty + th and ty < by + bh
               for tx, ty, tw, th in boxes):
            continue                                # 落在文字里的亮像素不算按钮
        out["buttons"].append({
            "x": bx, "y": by, "w": bw, "h": bh,
            "cx": x0 + bx + bw // 2, "cy": y0 + by + bh // 2,
        })
    out["buttons"].sort(key=lambda b: (b["y"] // 24, b["x"]))
    return out


def _shot(rect=None, longest: int = 640):
    """截屏 + 裁剪 + 缩小。整屏直接喂模型又慢又容易编，先裁到关心的那块。"""
    from PIL import Image

    im = _screen()
    if rect:
        im = im.crop(tuple(int(v) for v in rect))
    if max(im.size) > longest:
        s = longest / float(max(im.size))
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    # Qwen2.5-VL 的 patch 是 14、合并 2，任一边小于 28 像素直接报错
    # （"height:26 must be larger than factor:28"）。任务栏这种又长又扁的条补白到 56。
    if min(im.size) < 56:
        w, h = im.size
        canvas = Image.new("RGB", (max(w, 56), max(h, 56)), (255, 255, 255))
        canvas.paste(im, (0, 0))
        im = canvas
    return im


_NEG = ("否", "不是", "不对", "没有", "不存在", "no", "not")
_POS = ("是", "对", "有", "存在", "yes", "true")


def _yesno(text: str):
    """把模型的自由回答压成一个布尔。判不了返回 None（宁可报错，也不猜）。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    for w in _NEG:                     # 先看否定词：「不是」里也有「是」
        if w in t:
            return False
    for w in _POS:
        if w in t:
            return True
    return None


def check(question: str, rect=None, speed: str = "fast", max_new_tokens: int = 8) -> dict:
    """是非题：只让它回答「是 / 否」。选择题比开放问答更快也更准。"""
    prompt = "看图，只回答一个字：是 或 否。不要解释。问题：" + question
    ans = _see_raw(prompt, rect, 0, max_new_tokens, speed)
    yes = _yesno(ans)
    if yes is None:                    # 答跑偏了就再问一次，这次给多一点思考空间
        ans = _see_raw(prompt, rect, 0, 24, "normal")
        yes = _yesno(ans)
    if yes is None:
        raise RuntimeError("问不出是/否（模型答：%r）" % (ans or "")[:60])
    return {"yes": yes, "answer": ans}


_BRIEF = "用一句话回答，不超过 20 个字，不要解释。问题："


def see(question: str, rect=None, longest: int = 0, max_new_tokens: int = 0,
        speed: str = "normal") -> str:
    """问一句"这是什么"。

    默认只许它说一句话（20 字内、64 token 到顶）：3B 模型一旦放开就会长篇大论，
    说得多错得也多，还慢。要细节就自己给 max_new_tokens。
    """
    return _see_raw(_BRIEF + question, rect, longest, max_new_tokens, speed)


def _see_raw(prompt: str, rect=None, longest: int = 0, max_new_tokens: int = 0,
             speed: str = "normal") -> str:
    if not longest:
        longest = {"fast": 480, "normal": 640, "fine": 1280}.get(speed, 640)
    if not max_new_tokens:
        max_new_tokens = 64
    im = _shot(rect, longest)
    ans = _see_daemon(im, prompt, max_new_tokens)
    if ans is not None:
        return ans
    return _see_local(im, prompt, max_new_tokens)


def _see_daemon(im, question: str, max_new_tokens: int):
    """发给 visiond，返回答案；服务起不来或出错返回 None（调用方退回落本地）。"""
    import base64
    import io
    import urllib.request

    try:
        import visiond

        h = visiond._ping()
        if not h or not h.get("ready"):
            visiond.warm()               # 没起就起，被空转卸载过就装回来
        buf = io.BytesIO()
        im.save(buf, "PNG")
        body = json.dumps({"png_b64": base64.b64encode(buf.getvalue()).decode(),
                           "q": question, "mt": int(max_new_tokens)}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/see" % visiond.PORT, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read().decode())
        if out.get("err"):
            return None
        _last["vision_ms"] = out.get("ms")
        return out.get("a") or ""
    except Exception:  # noqa: BLE001 — 服务只是加速手段，不该成为失败点
        return None


def _see_local(im, question: str, max_new_tokens: int) -> str:
    """没有常驻服务时的退路：本进程加载一次 4bit 模型（约 20 秒）。"""
    global _vlm
    import torch
    from transformers import (AutoProcessor, BitsAndBytesConfig,
                              Qwen2_5_VLForConditionalGeneration)

    if _vlm is None:
        mid = "Qwen/Qwen2.5-VL-3B-Instruct"
        q = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                               bnb_4bit_compute_dtype=torch.bfloat16)
        m = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            mid, torch_dtype=torch.bfloat16, device_map="cuda", quantization_config=q).eval()
        _vlm = (m, AutoProcessor.from_pretrained(mid))

    model, proc = _vlm
    msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": question}]}]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = proc(text=[text], images=[im], return_tensors="pt").to("cuda")
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    return proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()


# ---------------------------------------------------------------- 组合动作
# 这两个是"以前写成一整个脚本"的活，现在收进连接器，不再单独留文件。
BROWSERS = ("microsoft edge", "google chrome", "chrome", "firefox", "brave", "opera")


class _Fresh:
    """板子动作之后立刻作废截屏缓存。

    成品动作直接调板子（b.key / b.type_text），绕过了 op 层那句 `if op in _CACHE_KILL`，
    于是下一步 OCR 读到的还是动手之前的截图——"敲完 ni 没有候选条"就是这么来的。
    包一层之后，将来写新动作不用再记得这条规矩。
    """

    _KILL = {"key", "type_text", "click", "move", "scroll", "drag", "drag_path", "hold"}

    def __init__(self, b):
        self._b = b

    def __getattr__(self, name):
        fn = getattr(self._b, name)
        if name not in self._KILL:
            return fn

        def call(*a, **k):
            try:
                return fn(*a, **k)
            finally:
                _invalidate()

        return call


def _brain(transport: str) -> Brain:
    b = Brain(transport=transport)
    b.__enter__()
    b.release()
    return _Fresh(b)


def recipe_wechat_send(st: dict, transport: str, rec: dict) -> None:
    """微信给指定聊天发一句话。pieces 里 py:<拼音> 走输入法，raw:<ascii> 直接敲。"""
    to = st.get("to", "文件传输助手")
    pieces = st.get("pieces") or ["py:nihao"]
    w = pick("微信")
    if not w:
        raise RuntimeError("没找到微信窗口")
    restore(w)
    time.sleep(0.15)              # 只是给窗口一点出现时间，真正的等待交给下面的标题轮询
    dr.focus_hwnd(int(w["hwnd"]))
    time.sleep(0.15)
    x0, y0, x1, y1 = w["rect"]
    W = x1 - x0
    # 标题条很窄：再往下就是消息区，宽的高亮行（比如消息里的网址）会冒充标题
    hdr_rect = (x0 + int(W * 0.30), y0 + 25, x1, y0 + 110)
    band_rect = (x0 + 340, y1 - 140, x1 - 40, y1 - 45)
    b = _brain(transport)
    try:
        # 等标题对得上（原来死等 1.1 秒；现在窗口一出来就往下走）
        head = {}

        def header_ok():
            rows = ocr(hdr_rect, fresh=True)
            if not rows:
                return False
            head["row"] = max(rows, key=lambda r: ratio(to, r["text"]))
            return ratio(to, head["row"]["text"]) >= 0.7

        poll(header_ok, 3.0)
        rec["header"] = head.get("row", {}).get("text")
        if not head.get("row") or ratio(to, head["row"]["text"]) < 0.7:
            raise RuntimeError("当前聊天不是 %r（标题是 %r），不动手" % (to, rec["header"]))
        b.click((x0 + int(W * 0.55), y1 - 95))
        time.sleep(0.25)
        for _ in range(3):        # 先退掉可能残留的输入法组字（"ni" 这种半截拼音）
            b.key("backspace")
        b.key("ctrl+a")
        b.key("backspace")
        base = {tight(r["text"]) for r in ocr(band_rect, fresh=True)}

        def typed(val):           # 敲完没有：候选条出来了，或者敲进去的字已经出现在输入框里
            band = ocr(band_rect, fresh=True)
            has_cand = any(r["text"].strip()[:1].isdigit() for r in band)
            txt = alnum("".join(r["text"] for r in band))
            return (band, has_cand) if (has_cand or alnum(val) in txt) else None

        for piece in pieces:
            kind, _, val = piece.partition(":")
            b.type_text(val)
            band, has_cand = poll(lambda: typed(val), 2.0) or ([], False)
            if kind == "raw":
                # 只有"候选条真的在"才按回车（候选行以序号开头）；否则回车会把没写完的消息发出去
                if has_cand:
                    b.key("enter")
                    poll(lambda: alnum(val) in alnum("".join(
                        r["text"] for r in ocr(band_rect, fresh=True))) or None, 1.5)
                continue
            if not has_cand:
                raise RuntimeError("敲完 %r 没有候选条，输入法可能不是中文" % val)
            b.key("1")
            # 候选条消失的准确标志：拼音串不再出现在输入框里（"0"/占位符那类噪声不能当依据）
            key = alnum(val)

            def pinyin_gone():
                rows = ocr(band_rect, fresh=True)
                return rows if not [r for r in rows if key and key in alnum(r["text"])] else None

            if poll(pinyin_gone, 2.0) is None:      # 原来固定 0.8 秒
                left = [r["text"] for r in ocr(band_rect, fresh=True)
                        if key and key in alnum(r["text"])]
                raise RuntimeError("拼音还留在输入框里（%r），没敢回车" % left)
        blob = alnum("".join(r["text"] for r in ocr(band_rect, fresh=True)))
        raws = [p.split(":", 1)[1] for p in pieces if p.startswith("raw:")]
        miss = [r for r in raws if alnum(r) not in blob]
        if miss:
            raise RuntimeError("输入框里没看到 %r，没敢回车" % miss)
        msg_rect = (x0 + 340, y0 + 700, x1, y1)
        before = {(tight(r["text"]), int(r["y"])) for r in ocr(msg_rect, fresh=True)}
        b.key("enter")

        def sent():               # 新气泡出现就算发成功（原来死等 1.6 秒）
            after = ocr(msg_rect, fresh=True)
            new = [r["text"] for r in after
                   if (tight(r["text"]), int(r["y"])) not in before and cjk(r["text"])]
            return (after, new) if new else None

        after, new = poll(sent, 3.0) or (ocr(msg_rect, fresh=True), [])
        still = [r["text"] for r in ocr(band_rect, fresh=True) if tight(r["text"]) not in base]
        new = [r["text"] for r in after
               if (tight(r["text"]), int(r["y"])) not in before and cjk(r["text"])]
        rec.update(input_after_send=still, new_bubbles=new)
        if still or not new:
            raise RuntimeError("回车发完了但没核对上（输入框剩 %r / 新气泡 %r）" % (still, new))
    finally:
        b.release()


def recipe_open_url(st: dict, transport: str, rec: dict) -> None:
    """把网址交给系统打开（Win+R），最后按窗口标题核对。

    不走浏览器地址栏：那里有自动补全，回车可能选中的是一条历史记录/个人主页，
    实测把「打开仓库页」变成了「打开 Your Repositories」。Win+R 里回车就是执行，
    没有这层歧义；浏览器开着就在里面开新标签，没开就启动它。
    """
    url = st["url"]
    expect = st.get("expect") or url.split("//")[-1].split("/")[0].replace("www.", "")
    rec["expect"] = expect
    b = _brain(transport)
    try:
        b.key("win+r")
        time.sleep(1.2)
        b.type_text(url)
        time.sleep(0.6)
        b.key("enter")
    finally:
        b.release()

    end = time.time() + float(st.get("wait", 25.0))
    last = ""
    while time.time() < end:
        for x in windows():
            if any(k in clean(x["title"]) for k in BROWSERS):
                last = x["title"]
                if clean(expect) in clean(last):
                    rec["window_after"] = last
                    return
        time.sleep(1.0)
    raise RuntimeError("标题里没等到 %r（现在是 %r）" % (expect, last))


RECIPES = {"wechat-send": recipe_wechat_send, "open-url": recipe_open_url}


# --------------------------------------------------------------------- 执行
def _point_of(st: dict, vars: dict):
    """要动到哪儿：on=<find 存的框> 优先，否则 x/y。click / move / point 共用一套规则。"""
    if "on" in st:
        box = vars.get(st["on"])
        if not box:
            raise RuntimeError("变量 %r 不存在（先 find ... as=%r）" % (st["on"], st["on"]))
        return int(box["cx"]), int(box["cy"])
    if "x" in st and "y" in st:
        return int(st["x"]), int(st["y"])
    raise RuntimeError("%s 需要 x,y 或 on=<find 的变量>" % st.get("op"))


_INPUT_OPS = {"key", "type", "click", "move", "point", "drag", "drag_path",
              "hold", "scroll", "recipe"}


def _game_guard(st: dict) -> None:
    """全屏应用在跑（游戏/全屏视频）时，绝不往里发键鼠。

    这是安全约定，不是建议：全屏下按键直接进游戏，手滑一下就是误操作。
    确实要操作就显式写 "force": true。
    """
    if st.get("force") or st.get("op") not in _INPUT_OPS:
        return
    try:
        import visiond

        fs = visiond._fullscreen()
    except Exception:  # noqa: BLE001 — 判不出来就不拦，别把工具卡死
        return
    if fs:
        raise RuntimeError("前台是全屏应用（全屏游戏/视频），按安全约定不发送键鼠；"
                           "确认要操作就加 \"force\": true")


def run_job(job: dict | list, transport: str = "tcp") -> dict:
    steps = job.get("steps") if isinstance(job, dict) else job
    steps = steps or []
    res: dict = {"ok": True, "steps": [], "vars": {}}
    vars: dict = res["vars"]
    brain = None
    try:
        for i, st in enumerate(steps):
            op = st.get("op")
            rec: dict = {"i": i, "op": op}
            try:
                _game_guard(st)          # 全屏里不发送键鼠（除非 force）
                if op in ("restore", "focus", "title"):
                    w = pick_st(st)
                    if op == "restore":
                        restore(w)
                        time.sleep(st.get("wait", 0.6))
                    elif op == "focus":
                        restore(w)
                        dr.focus_hwnd(int(w["hwnd"]))
                        time.sleep(st.get("wait", 0.5))
                    rec["hwnd"] = int(w["hwnd"])
                    rec["title"] = w["title"]
                    if st.get("as"):
                        vars[st["as"]] = int(w["hwnd"])

                elif op in ("key", "type", "click", "move", "scroll",
                            "drag", "drag_path", "hold", "point", "wait_change",
                            "look", "read_text", "diff"):
                    if brain is None:
                        brain = Brain(transport=transport)
                        brain.__enter__()
                        brain.release()
                    if op == "key":
                        brain.key(st["combo"])
                    elif op == "type":
                        brain.type_text(st["text"])
                    elif op == "move":
                        brain.move(*_point_of(st, vars))
                    elif op == "scroll":
                        brain.scroll(int(st.get("amount", -3)))
                    elif op == "click":
                        brain.click(_point_of(st, vars), button=st.get("button", "left"))
                    elif op == "drag":
                        brain.drag(int(st["x1"]), int(st["y1"]), int(st["x2"]), int(st["y2"]),
                                   button=st.get("button", "left"))
                    elif op == "drag_path":
                        brain.drag_path([tuple(int(v) for v in p) for p in st["points"]],
                                        button=st.get("button", "left"))
                    elif op == "hold":
                        brain.hold(st["keys"])
                    elif op == "point":      # 闭环定位：比 move 准（自己纠偏到目标）
                        px, py = _point_of(st, vars)
                        rec["result"] = brain.point(px, py, tol=int(st.get("tol", 2)))
                    elif op == "look":
                        rec["result"] = brain.look(st.get("path"), st.get("region"))
                    elif op == "read_text":
                        rec["result"] = brain.read_text(
                            select_all=bool(st.get("select_all", True)),
                            restore_clip=bool(st.get("restore_clip", True)))
                        rec["text"] = (rec["result"] or {}).get("text")
                    elif op == "diff":
                        if "a" not in st or "b" not in st:
                            raise RuntimeError("diff 需要 a 和 b 两个变量（先 look 截两张，把 as= 存的变量名传进来）")
                        rec["result"] = brain.diff(st["a"], st["b"], int(st.get("threshold", 0)))
                    else:                    # wait_change：等画面变化，等不到就报错
                        rec["result"] = brain.wait_change(
                            timeout_ms=int(st.get("timeout_ms", 3000)),
                            threshold=int(st.get("threshold", 0)))
                    rec["at"] = st.get("combo") or st.get("text") or (
                        vars.get(st["on"], {}).get("cx") if "on" in st else (st.get("x"), st.get("y")))

                elif op == "launch":
                    if brain is not None:
                        brain.release()
                        brain = None
                    b = _brain(transport)
                    try:
                        rec["result"] = b.launch(st["cmd"])
                    finally:
                        b.release()

                elif op == "clip":
                    from brain import clip_get, clip_set
                    if "set" in st:
                        clip_set(st["set"])
                        rec["set_len"] = len(st["set"])
                    else:
                        rec["text"] = clip_get()

                elif op == "wait":
                    time.sleep(float(st.get("ms", 500)) / 1000.0)

                elif op == "ui":
                    # 一次把这个窗口读成结构：文字 + 可疑按钮（都带屏幕坐标）
                    w = pick_st(st)
                    rec["result"] = ui_map(w, bool(st.get("buttons", True)))
                    if st.get("as"):
                        vars[st["as"]] = rec["result"]

                elif op == "ocr":
                    rows = ocr(st["rect"])
                    rec["rows"] = [r["text"] for r in rows]
                    # 文字和坐标一起给：只有 rows 的话，想点某一行的 AI 只能猜坐标
                    rec["boxes"] = [
                        {"text": r["text"],
                         "x": int(r["x"]), "y": int(r["y"]),
                         "w": int(r["w"]), "h": int(r["h"]),
                         "cx": int(r["x"] + r["w"] / 2),
                         "cy": int(r["y"] + r["h"] / 2)}
                        for r in rows]
                    if st.get("as"):
                        vars[st["as"]] = rows

                elif op == "find":
                    rect = st.get("rect") or [0, 0, *screen_size()]
                    rows = ocr(rect)
                    want = st.get("text", "")
                    hit, best = None, 0.0
                    for r in rows:
                        s = ratio(want, r["text"])
                        if s > best:
                            hit, best = r, s
                    if hit is None or best < float(st.get("thresh", 0.7)):
                        raise RuntimeError("区域里没找到 %r（最高 %.2f）" % (want, best))
                    box = dict(hit)
                    box["cx"] = int(hit["x"] + hit["w"] / 2)
                    box["cy"] = int(hit["y"] + hit["h"] / 2)
                    if st.get("as"):
                        vars[st["as"]] = box
                    rec["found"] = {k: box[k] for k in ("text", "x", "y", "w", "h", "cx", "cy")}
                    rec["score"] = round(best, 3)

                elif op == "see":
                    ans = see(st["q"], st.get("rect"), int(st.get("longest", 0)),
                              int(st.get("max_new_tokens", 0)),
                              str(st.get("speed", "normal")))
                    rec["answer"] = ans
                    if _last.get("vision_ms") is not None:
                        rec["ms"] = _last["vision_ms"]
                    if st.get("as"):
                        vars[st["as"]] = ans

                elif op == "check":
                    # 是非题：只回答是/否。比开放问答快，也更容易核对
                    r = check(st["q"], st.get("rect"), str(st.get("speed", "fast")),
                              int(st.get("max_new_tokens", 8)))
                    rec["yes"] = r["yes"]
                    rec["answer"] = r["answer"]
                    if _last.get("vision_ms") is not None:
                        rec["ms"] = _last["vision_ms"]
                    if st.get("as"):
                        vars[st["as"]] = r["yes"]

                elif op == "vision":
                    # 常驻服务的状态；action=start/stop 时顺带起停
                    import visiond

                    act = st.get("action", "status")
                    if act == "start":
                        rec["result"] = visiond.warm()
                    elif act == "stop":
                        rec["result"] = {"stopped": visiond.stop()}
                    elif act == "unload":       # 立刻还显存（打游戏前）
                        rec["result"] = visiond.unload()
                    else:
                        rec["result"] = visiond._ping(1.0) or {"ready": False}

                elif op == "recipe":
                    name = st.get("name")
                    fn = RECIPES.get(name or "")
                    if not fn:
                        raise RuntimeError("不认识的组合动作 %r（有：%s）"
                                           % (name, ", ".join(sorted(RECIPES))))
                    if brain is not None:      # 组合动作自己开会话，先把这边的放掉
                        brain.release()
                        brain = None
                    fn(st, transport, rec)
                    rec["recipe"] = name

                else:
                    raise RuntimeError("不认识的操作 %r" % op)

                if op in _CACHE_KILL:      # 屏幕多半变了，下次看屏幕重新截
                    _invalidate()
                if _last.get("ocr_ms") is not None and op in ("ocr", "find"):
                    rec["ms"] = _last["ocr_ms"]
                if "require" in st:
                    blob = json.dumps(rec, ensure_ascii=False)
                    if clean(st["require"]) not in clean(blob):
                        raise RuntimeError("require 不满足：%r" % st["require"])
                rec["ok"] = True
            except Exception as e:  # noqa: BLE001
                rec.update(ok=False, err="%s: %s" % (type(e).__name__, e))
                res["steps"].append(rec)
                res["ok"] = False
                res["err"] = "第 %d 步 %s 失败：%s" % (i, op, e)
                return res
            res["steps"].append(rec)
        return res
    finally:
        if brain is not None:
            try:
                brain.release()
            except Exception:  # noqa: BLE001
                pass
        _cleanup_tmp()


def selfcheck(transport: str = "tcp") -> dict:
    """一条命令回答"这台机器现在能不能用"。任何一项硬检查失败就返回 ok=false。"""
    checks = []

    def add(name, fn, hard=True):
        t0 = time.time()
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "ms": int((time.time() - t0) * 1000),
                           "detail": detail})
        except Exception as e:  # noqa: BLE001
            checks.append({"check": name, "ok": False, "hard": hard,
                           "err": "%s: %s" % (type(e).__name__, e)})

    def screen():
        w, h = screen_size()
        p = eyes.capture(_tmp("full")).path
        from PIL import Image

        im = Image.open(p)
        if im.width < 640:
            raise RuntimeError("截到的图只有 %dx%d，不对劲" % (im.size))
        return {"logical": [w, h], "shot": list(im.size)}

    def ocr_ok():
        rows = ocr([0, 0, min(600, screen_size()[0]), 200])
        return {"engine": "rapidocr", "rows": len(rows)}

    def board():
        b = _brain(transport)
        try:
            info = b.info()
        finally:
            b.release()
        return {"transport": transport, "info": info}

    def vision():
        import visiond

        h = visiond._ping(1.0)
        if not h:
            raise RuntimeError("没在跑（首次 ocr/see 会自动起，或 visiond.py --start）")
        if not h.get("ocr"):
            raise RuntimeError("起来了但 OCR 引擎还没加载完")
        # 大模型按需加载（见 visiond._serve），没装进显存不算故障——真要看图时自己会装
        return {"ready": h.get("ready"), "ocr": h.get("ocr"), "vram_mb": h.get("vram_mb")}

    def profile():
        p = r"C:\ProgramData\dsh\pointer-profile.json"
        if not os.path.exists(p):
            raise RuntimeError("没有指针校准文件，point 会不准")
        return {"path": p}

    add("屏幕截取", screen)
    add("OCR 引擎", ocr_ok)
    add("板子连接", lambda: board())
    add("指针校准", profile, hard=False)
    add("视觉服务", vision, hard=False)
    _cleanup_tmp()
    hard_fail = [c for c in checks if not c["ok"] and c.get("hard", True)]
    return {"ok": not hard_fail, "checks": checks}


_READ_ONLY = {"look", "ocr", "find", "read_text", "diff", "title", "see",
              "vision", "ui", "wait", "wait_change", "clip"}


def _is_disconnect(err: str) -> bool:
    e = (err or "").lower()
    return "portdisconnected" in e or "10053" in e or "10054" in e


def _init_stdio() -> None:
    """中文 Windows 的 stdout 是 cp936：OCR 读到的中文打出去就变乱码，AI 读到的是问号。

    管道输出（AI/脚本读它）统一 UTF-8；终端输出保留控制台编码，只把编不了的字符换成 ?。
    """
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            tty = s.isatty()
        except Exception:  # noqa: BLE001
            tty = False
        try:
            s.reconfigure(encoding=(s.encoding or "utf-8") if tty else "utf-8",
                          errors="replace")
        except Exception:  # noqa: BLE001 — 老 Python / 被重定向的流，改了也不亏
            pass


def main() -> int:
    _init_stdio()
    if "--selfcheck" in sys.argv:
        out = selfcheck(os.environ.get("CONNECTOR_TRANSPORT", "tcp"))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"ok": False, "err": "stdin 里没有作业 JSON"}, ensure_ascii=False))
        return 2
    job = json.loads(raw)
    transport = os.environ.get("CONNECTOR_TRANSPORT", "tcp")
    out = run_job(job, transport=transport)
    # 串口/TCP 会话被对面掐掉（10053）时：只读作业重试一次是安全的；
    # 有输入动作的作业绝不重试——宁可报错，也不赌第二次点击。
    steps = (job.get("steps") if isinstance(job, dict) else job) or []
    if not out.get("ok") and _is_disconnect(out.get("err", "")):
        if all((s or {}).get("op") in _READ_ONLY for s in steps):
            time.sleep(0.6)
            out = run_job(job, transport=transport)
            out["retried"] = True
        else:
            out["err"] += "（板子连接断了，这一步没执行；重跑整条命令即可）"
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
