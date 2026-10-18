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

    restore   title=<子串>                还原最小化/托盘里的窗口（不移动、不改大小）
    focus     title=<子串>                切到前台
    title     title=<子串>                报告当前窗口标题（facts 用 Win32，不靠模型）
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
    recipe    name=open-url  url=<网址> [expect=标题关键字] [new_tab=true]
              开浏览器到某网址（开着就 Ctrl+T 新标签，没开就 Win+R 交给系统，最后核对标题）

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


def pick(title: str) -> dict | None:
    """按标题子串挑窗口；可见的优先，其次是能拿到句柄的。"""
    best = None
    for w in windows():
        if clean(title) not in clean(w["title"]):
            continue
        iconic = w["rect"][0] <= -30000
        rank = (not iconic, w["rect"][2] - w["rect"][0])
        if best is None or rank > best[0]:
            best = (rank, w)
    return best[1] if best else None


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
        if not h:
            visiond.start()
        elif not h.get("ocr"):
            time.sleep(0.2)                  # OCR 引擎还在加载，等一下再试
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
        from rapidocr_onnxruntime import RapidOCR
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


def ocr(rect) -> list:
    """RapidOCR：一次拿回所有行的文字和屏幕坐标（坐标已经换算回整屏）。"""
    x0, y0, x1, y1 = [int(v) for v in rect]
    im = _screen().crop((x0, y0, x1, y1))
    rows = _ocr_daemon(im)
    if rows is None:
        rows = _ocr_local(im)
    for r in rows:
        r["x"] += x0
        r["y"] += y0
    return rows


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


def _brain(transport: str) -> Brain:
    b = Brain(transport=transport)
    b.__enter__()
    b.release()
    return b


def recipe_wechat_send(st: dict, transport: str, rec: dict) -> None:
    """微信给指定聊天发一句话。pieces 里 py:<拼音> 走输入法，raw:<ascii> 直接敲。"""
    to = st.get("to", "文件传输助手")
    pieces = st.get("pieces") or ["py:nihao"]
    w = pick("微信")
    if not w:
        raise RuntimeError("没找到微信窗口")
    restore(w)
    time.sleep(0.6)
    dr.focus_hwnd(int(w["hwnd"]))
    time.sleep(0.5)
    x0, y0, x1, y1 = w["rect"]
    W = x1 - x0
    # 标题条很窄：再往下就是消息区，宽的高亮行（比如消息里的网址）会冒充标题
    hdr_rect = (x0 + int(W * 0.30), y0 + 25, x1, y0 + 110)
    band_rect = (x0 + 340, y1 - 140, x1 - 40, y1 - 45)
    b = _brain(transport)
    try:
        rows = ocr(hdr_rect)
        head = max(rows, key=lambda r: ratio(to, r["text"])) if rows else None
        rec["header"] = head["text"] if head else None
        if not head or ratio(to, head["text"]) < 0.7:
            raise RuntimeError("当前聊天不是 %r（标题是 %r），不动手" % (to, rec["header"]))
        b.click((x0 + int(W * 0.55), y1 - 95))
        time.sleep(0.4)
        b.key("ctrl+a")
        b.key("backspace")
        time.sleep(0.4)
        base = {tight(r["text"]) for r in ocr(band_rect)}
        for piece in pieces:
            kind, _, val = piece.partition(":")
            b.type_text(val)
            time.sleep(0.9)
            band = ocr(band_rect)
            if kind == "raw":
                # 只有"候选条真的在"才按回车（候选行以序号开头）；否则回车会把没写完的消息发出去
                if any(r["text"].strip()[:1].isdigit() for r in band):
                    b.key("enter")
                    time.sleep(0.7)
                continue
            if not band:
                raise RuntimeError("敲完 %r 没有候选条，输入法可能不是中文" % val)
            b.key("1")
            time.sleep(0.8)
            # 候选条消失的准确标志：拼音串不再出现在输入框里（"0"/占位符那类噪声不能当依据）
            key = alnum(val)
            left = [r["text"] for r in ocr(band_rect) if key and key in alnum(r["text"])]
            if left:
                raise RuntimeError("拼音还留在输入框里（%r），没敢回车" % left)
        blob = alnum("".join(r["text"] for r in ocr(band_rect)))
        raws = [p.split(":", 1)[1] for p in pieces if p.startswith("raw:")]
        miss = [r for r in raws if alnum(r) not in blob]
        if miss:
            raise RuntimeError("输入框里没看到 %r，没敢回车" % miss)
        msg_rect = (x0 + 340, y0 + 700, x1, y1)
        before = {(tight(r["text"]), int(r["y"])) for r in ocr(msg_rect)}
        b.key("enter")
        time.sleep(1.6)
        after = ocr(msg_rect)
        still = [r["text"] for r in ocr(band_rect) if tight(r["text"]) not in base]
        new = [r["text"] for r in after
               if (tight(r["text"]), int(r["y"])) not in before and cjk(r["text"])]
        rec.update(input_after_send=still, new_bubbles=new)
        if still or not new:
            raise RuntimeError("回车发完了但没核对上（输入框剩 %r / 新气泡 %r）" % (still, new))
    finally:
        b.release()


def recipe_open_url(st: dict, transport: str, rec: dict) -> None:
    """把浏览器打开到某个网址：开着就新标签，没开就 Win+R 交给系统。"""
    url = st["url"]
    expect = st.get("expect") or url.split("//")[-1].split("/")[0].replace("www.", "")
    rec["expect"] = expect
    w = None
    for x in windows():
        t = clean(x["title"])
        if any(k in t for k in BROWSERS) and (not st.get("browser") or clean(st["browser"]) in t):
            if w is None or (x["rect"][0] > -30000 and w["rect"][0] <= -30000):
                w = x
    b = _brain(transport)
    try:
        if w:
            rec["mode"] = "existing"
            restore(w)
            time.sleep(0.6)
            dr.focus_hwnd(int(w["hwnd"]))
            time.sleep(0.5)
            if st.get("new_tab", True):
                b.key("ctrl+t")
                time.sleep(0.7)
            b.key("ctrl+l")
            time.sleep(0.4)
            b.type_text(url)
            time.sleep(0.7)
            for _ in range(3):        # 输入法若在组字，第一下回车只是提交
                b.key("enter")
                time.sleep(2.0)
                cur = [x for x in windows() if int(x["hwnd"]) == int(w["hwnd"])]
                if cur and clean(expect) in clean(cur[0]["title"]):
                    break
        else:
            rec["mode"] = "launch"
            b.key("win+r")
            time.sleep(1.2)
            b.type_text(url)
            time.sleep(0.6)
            b.key("enter")
            time.sleep(1.0)
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
                if op in ("restore", "focus", "title"):
                    w = pick(st.get("title", ""))
                    if not w:
                        raise RuntimeError("没找到窗口 %r" % st.get("title"))
                    if op == "restore":
                        restore(w)
                        time.sleep(st.get("wait", 0.6))
                    elif op == "focus":
                        restore(w)
                        dr.focus_hwnd(int(w["hwnd"]))
                        time.sleep(st.get("wait", 0.5))
                    rec["hwnd"] = int(w["hwnd"])
                    rec["title"] = pick(st.get("title", ""))["title"] if op != "title" else w["title"]
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

                elif op == "ocr":
                    rows = ocr(st["rect"])
                    rec["rows"] = [r["text"] for r in rows]
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
            raise RuntimeError("没在跑（首次 see 会自动起，或 visiond.py --start）")
        if not h.get("ready"):
            raise RuntimeError("起来了但模型还没加载完")
        return h

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
              "vision", "wait", "wait_change", "clip"}


def _is_disconnect(err: str) -> bool:
    e = (err or "").lower()
    return "portdisconnected" in e or "10053" in e or "10054" in e


def main() -> int:
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
