#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/ascii-to-image.py — 把 ASCII 字符画还原成 PNG（再看图/画出来）。

用途：别的模型/网站可能只给你一段「字符画」（HTML 的 <pre> 或 .txt），
这段文字本身就是一张图。本工具按每个字符的**实际字形承墨量**把它还原成灰度图，
再交给 tools/draw-image.py 规划笔画 / 真画。

用法：
    python tools\\ascii-to-image.py art.html                     # 默认还原到 inbox\\ascii-<时间>.png
    python tools\\ascii-to-image.py art.html --scale 2            # 每字符 2px（更清楚，体积大）
    python tools\\ascii-to-image.py art.txt --invert              # 反相（黑底白字 <-> 白底黑字）
    python tools\\ascii-to-image.py art.html --aspect 2.0         # 字符单元高/宽比（默认 1.0=方形）
    python tools\\ascii-to-image.py art.html --out D:\\x.png --json

输出一行 JSON：{"ok":true,"path":"...png","chars":[[w,h]],"distinct":N,"ink_mean":...,"scale":..}
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_WORKSPACE = os.path.dirname(_ROOT)
INBOX = os.path.join(_WORKSPACE, "inbox")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\cour.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
]

# 兜底：常见 ASCII 灰度梯度（越靠后越"满"）
RAMP = " .`',:;!ilI|/\\()[]{}1tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"

# 网上流传最广的 70 级「图片转字符画」梯度（很多转换器直接用这一串）。
# 命中它时按序号当亮度最准：字符画就是「亮度 -> 这串里的第几个字符」。
CLASSIC_RAMP = " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"


def read_ascii(path: str) -> list[str]:
    raw = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.S)
    text = html.unescape(m.group(1)) if m else raw
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def glyph_coverage(chars: list[str], font_path: str | None, size: int = 28) -> dict[str, float]:
    """每种字符的实际承墨比例（0..1），用真实等宽字体量出来。"""
    cov: dict[str, float] = {}
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:  # noqa: BLE001
            font = None
    if font is None:
        pos = {c: (i + 1) / len(RAMP) for i, c in enumerate(RAMP)}
        for c in chars:
            cov[c] = 0.0 if c == " " else pos.get(c, 0.5)
        return cov

    img = Image.new("L", (size * 2, size * 2), 255)
    dr = ImageDraw.Draw(img)
    for c in chars:
        if c == " ":
            cov[c] = 0.0
            continue
        img.paste(255, (0, 0, img.width, img.height))
        dr.text((size // 2, size // 4), c, font=font, fill=0)
        a = np.asarray(img, dtype=np.float32)
        cov[c] = float((255.0 - a).sum() / (a.size * 255.0))
    return cov


def build_ascii_map(lines: list[str]) -> np.ndarray:
    w = max(len(ln) for ln in lines)
    grid = [ln.ljust(w, " ") for ln in lines]
    # 先用兜底梯度量化每个字符，稍后按需替换成字形承墨量
    pos = {c: (i + 1) / len(RAMP) for i, c in enumerate(RAMP)}
    out = np.zeros((len(grid), w), dtype=np.float32)
    for y, ln in enumerate(grid):
        out[y] = [pos.get(c, 0.5) for c in ln]
    return out, grid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ASCII 字符画 -> PNG")
    ap.add_argument("src", help="字符画文件（.html/.htm/.txt）")
    ap.add_argument("--out", default=None, help="输出 PNG（默认 inbox\\ascii-<时间>.png）")
    ap.add_argument("--scale", type=int, default=2, help="每字符像素数（默认 2）")
    ap.add_argument("--aspect", type=float, default=1.0, help="字符单元 高/宽 比（默认 1.0 方形）")
    ap.add_argument("--invert", action="store_true", help="反相（白底黑字 <-> 黑底白字）")
    ap.add_argument("--no-normalize", action="store_true", help="不把最密的字符拉到全白（对比度会很低）")
    ap.add_argument("--equalize", action="store_true",
                    help="按字符承墨量排名均匀铺开对比度（推荐：还原照片时更接近原图）")
    ap.add_argument("--map", choices=["coverage", "classic"], default="coverage",
                    help="亮度映射：coverage=实测字形承墨量；classic=按常见 70 级梯度序号（字符画由它生成时最准）")
    ap.add_argument("--ramp", action="store_true", help="不用字体承墨量，用内置灰度梯度")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.src):
        print(json.dumps({"ok": False, "err": "文件不存在: %s" % a.src}, ensure_ascii=False))
        return 1
    os.makedirs(INBOX, exist_ok=True)

    lines = read_ascii(a.src)
    if not lines:
        print(json.dumps({"ok": False, "err": "没读到字符画内容"}, ensure_ascii=False))
        return 2
    ink, grid = build_ascii_map(lines)
    chars = sorted(set("".join(grid)) - {" "})

    if a.map == "classic":
        # 字符画常用 70 级梯度：按序号当亮度，最接近生成时的映射
        idx = {c: i for i, c in enumerate(CLASSIC_RAMP)}
        missing = [c for c in chars if c not in idx]
        if missing:
            raise SystemExit("这些字符不在 classic 70 级梯度里，改用 --map coverage: %r" % missing)
        n = len(CLASSIC_RAMP) - 1
        for y, ln in enumerate(grid):
            for x, c in enumerate(ln):
                ink[y, x] = idx[c] / n
        font_used = "classic-ramp"
    elif not a.ramp:
        font_path = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)
        cov = glyph_coverage(chars, font_path)
        if a.equalize:
            # 排名均匀铺开：字符画生成器用的梯度未必等于字形承墨量，
            # 按实测承墨量排序后等距铺 0..1，能还原出接近原图的对比度
            order = sorted(cov.items(), key=lambda kv: kv[1])
            n = max(1, len(order) - 1)
            cov = {c: i / n for i, (c, _v) in enumerate(order)}
            cov[" "] = 0.0
        for y, ln in enumerate(grid):
            for x, c in enumerate(ln):
                ink[y, x] = cov.get(c, 0.0)
        font_used = (os.path.basename(font_path) if font_path else "ramp") + \
                    ("+equalize" if a.equalize else "")
    else:
        font_used = "ramp"

    if a.invert:
        ink = 1.0 - ink
    if not a.no_normalize:
        hi = float(ink.max())
        ink = ink / hi if hi > 1e-9 else ink
    # 字符画显示成「亮字黑底」：承墨量 = 亮度
    lum = (np.clip(ink, 0.0, 1.0) * 255.0).astype(np.uint8)

    h, w = lum.shape
    pw = max(1, int(round(w * a.scale)))
    ph = max(1, int(round(h * a.scale * a.aspect)))
    img = Image.fromarray(lum, mode="L").resize((pw, ph), Image.NEAREST).convert("RGB")

    out = a.out or os.path.join(INBOX, "ascii-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out, "PNG")
    info = {"ok": True, "path": out, "chars": [w, h], "pixels": [pw, ph],
            "distinct": len(chars), "font": font_used, "scale": a.scale,
            "aspect": a.aspect, "invert": bool(a.invert),
            "ink_mean": round(float(ink.mean()), 4),
            "ink_std": round(float(ink.std()), 4),
            "hint": "下一步：python tools\\draw-image.py \"%s\" --mode sketch --preview-width 4" % out}
    print(json.dumps(info, ensure_ascii=False, indent=2 if not a.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
