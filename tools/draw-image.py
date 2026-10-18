#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/draw-image.py — 把一张图「画」进 Windows 画图（ESP32-P4 HID 鼠标）。

它做什么
--------
输入任意图片（PNG/JPG/WebP/BMP/GIF/TIFF/ICO，SVG 先由 ingest-image 转 PNG），
输出一串「笔画折线」，然后（可选）由板子的鼠标在画图里一笔一笔画出来。

三种描线策略
------------
line    线稿/简笔画/卡通/logo/签名：阈值 -> 细化(skeleton) -> 追踪成折线 -> 简化
sketch  照片/复杂图：高斯模糊 + Sobel 边缘 -> 细化 -> 追踪（铅笔速写风格，密度可调）
grid    像素风/图标：按网格采样，暗格子点一个点（小图专用，别用在大图上）

安全设计
--------
* 默认 **只规划不落笔**（--dry-run 是默认行为），必须显式 --draw 才会动鼠标；
* --draw 时每笔开始前复核前台窗口必须是画图，否则立即停手；
* --draw 不会移动/最大化画图窗口（复用 draw-rage 的检测，不改窗口）；
* 用户抢鼠标（光标和预期差太远）会抬笔跳过该笔，记录事件；
* 规划结果和预览图都落盘，可以先看预览再决定画不画。

用法
----
  # 1) 只规划 + 出预览图（不动鼠标，最安全）
  python tools/draw-image.py inbox\\cat.png --canvas 0,120,1000,1120 --mode line
  # 2) 进画图（自动找画图窗口/画布），先看预览
  python tools/draw-image.py inbox\\cat.png --mode line
  # 3) 真画
  python tools/draw-image.py inbox\\cat.png --mode line --draw
  # 4) 照片转铅笔速写
  python tools/draw-image.py inbox\\photo.jpg --mode sketch --edge 40 --draw

输出：规划 JSON + 预览 PNG 写到 <workspace>\\inbox\\，并打印一行 JSON 摘要。
退出码：0 成功；1 参数/文件问题；2 找不到画图/画布；3 规划为空；4 画的时候前台不是画图。
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import math
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PC = os.path.join(_ROOT, "pc")
_WORKSPACE = os.path.dirname(_ROOT)
INBOX = os.path.join(_WORKSPACE, "inbox")
sys.path.insert(0, _PC)
sys.path.insert(0, _HERE)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageOps  # noqa: E402

# 复用 draw-rage 的窗口/画布检测（文件名带连字符，用 importlib 加载）
def _load_draw_rage():
    path = os.path.join(_HERE, "draw-rage.py")
    spec = importlib.util.spec_from_file_location("draw_rage", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    dr = _load_draw_rage()
except Exception as exc:  # noqa: BLE001
    dr = None
    _DR_ERR = exc
else:
    _DR_ERR = None


# --------------------------------------------------------------------------- #
# 图像 -> 笔画
# --------------------------------------------------------------------------- #
def load_rgb(path: str) -> Image.Image:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _gray_array(im: Image.Image, invert_auto: bool = True) -> np.ndarray:
    g = np.asarray(im.convert("L"), dtype=np.float32)
    if invert_auto and g.mean() < 115:      # 深底白线 -> 反色，统一成"暗=墨"
        g = 255.0 - g
    return g


def thin_zhang_suen(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen 细化：把粗线压成 1 px 骨架（numpy 向量化）。"""
    img = mask.astype(np.int16).copy()
    for _ in range(40):
        before = int(img.sum())
        for first in (True, False):
            p = np.pad(img, 1)
            P2, P3, P4 = p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:]
            P5, P6, P7 = p[2:, 2:], p[2:, 1:-1], p[2:, :-2]
            P8, P9 = p[1:-1, :-2], p[:-2, :-2]
            B = P2 + P3 + P4 + P5 + P6 + P7 + P8 + P9
            seq = (P2, P3, P4, P5, P6, P7, P8, P9, P2)
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.int16) for i in range(8))
            if first:
                cond = ((img == 1) & (B >= 2) & (B <= 6) & (A == 1)
                        & ((P2 * P4 * P6) == 0) & ((P4 * P6 * P8) == 0))
            else:
                cond = ((img == 1) & (B >= 2) & (B <= 6) & (A == 1)
                        & ((P2 * P4 * P8) == 0) & ((P2 * P6 * P8) == 0))
            img[cond] = 0
        if int(img.sum()) == before:
            break
    return img.astype(bool)


def trace_skeleton(skel: np.ndarray) -> list[list[tuple[int, int]]]:
    """把骨架追踪成折线。

    8 连通骨架的斜线会出现"台阶"：一个像素同时有正交和对角邻居，度数=3，
    按"度数!=2 就断"会把一条斜线切成一堆 2 px 碎片。所以这里用**方向连续性**
    走法：每步在没走过的邻居里挑方向变化最小的（对角优先），走到无路可走为止；
    交叉点（真分叉）会顺着继续走，另一条分支之后从剩余像素另起一笔。
    """
    H, W = skel.shape
    ys, xs = np.nonzero(skel)
    S = set(zip(xs.tolist(), ys.tolist()))
    if not S:
        return []
    NB = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
    nbr: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (x, y) in S:
        nbr[(x, y)] = [(x + dx, y + dy) for dx, dy in NB if (x + dx, y + dy) in S]

    visited: set[tuple[int, int]] = set()
    out: list[list[tuple[int, int]]] = []

    def step(cur, prev, d):
        """从 cur 出发挑下一像素；d 是上一步方向（可能为 0,0）。"""
        cands = [q for q in nbr[cur] if q not in visited]
        if prev is not None and len(cands) > 1:
            # 不要 180 度掉头（除非别无选择）
            back = (prev[0] - cur[0], prev[1] - cur[1])
            no_back = [q for q in cands if (q[0] - cur[0], q[1] - cur[1]) != back]
            if no_back:
                cands = no_back
        if len(cands) > 1 and d != (0, 0):
            def score(q):
                vx, vy = q[0] - cur[0], q[1] - cur[1]
                dot = vx * d[0] + vy * d[1]
                diag = 1 if (vx and vy) else 0     # 台阶里优先走对角，线更直
                same = 1 if (vx, vy) == d else 0
                return (same, dot, diag)
            cands.sort(key=score, reverse=True)
        return cands[0] if cands else None

    def walk(start, second=None):
        path = [start]
        visited.add(start)
        cur = start
        first = second if second is not None else step(start, None, (0, 0))
        prev = start
        d = (0, 0)
        if first is not None:
            d = (first[0] - start[0], first[1] - start[1])
            path.append(first)
            visited.add(first)
            prev, cur = start, first
        while True:
            q = step(cur, prev, d)
            if q is None:
                break
            path.append(q)
            visited.add(q)
            d = (q[0] - prev[0], q[1] - prev[1])
            prev, cur = cur, q
            if len(path) > 200000:
                break
        return path

    # 先从未走过的端点起笔（保证长线完整），再从剩下像素起笔
    seeds = [p for p in S if len(nbr[p]) == 1 and p not in visited]
    seeds.sort(key=lambda p: (len(nbr[p]), p[1], p[0]))
    for p in seeds:
        if p in visited:
            continue
        seg = walk(p)
        if len(seg) >= 2:
            out.append(seg)
    rest = [p for p in S if p not in visited]
    rest.sort(key=lambda p: (len(nbr[p]), p[1], p[0]))
    for p in rest:
        if p in visited:
            continue
        seg = walk(p)
        if len(seg) >= 2:
            out.append(seg)
    return out


def rdp(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Douglas-Peucker 简化（迭代版）。"""
    if len(points) < 3 or eps <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-9
        best_i, best_d = -1, -1.0
        for i in range(a + 1, b):
            px, py = points[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if d > best_d:
                best_i, best_d = i, d
        if best_d > eps and best_i > 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for i, p in enumerate(points) if keep[i]]


def resample(points: list[tuple[float, float]], step: float) -> list[tuple[float, float]]:
    """按弧长等距重采样（步长 step px），让曲线由很多小段拼出来。"""
    if len(points) < 2 or step <= 0:
        return points
    out = [points[0]]
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 1e-9:
            continue
        n = max(1, int(round(seg / step)))
        for k in range(1, n + 1):
            t = k / float(n)
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def smooth_polyline(points: list[tuple[float, float]], passes: int) -> list[tuple[float, float]]:
    """移动平均平滑（端点固定）：把像素台阶磨成顺滑曲线。"""
    if len(points) < 3 or passes <= 0:
        return points
    pts = list(points)
    for _ in range(passes):
        nxt = [pts[0]]
        for i in range(1, len(pts) - 1):
            x = (pts[i - 1][0] + 2.0 * pts[i][0] + pts[i + 1][0]) / 4.0
            y = (pts[i - 1][1] + 2.0 * pts[i][1] + pts[i + 1][1]) / 4.0
            nxt.append((x, y))
        nxt.append(pts[-1])
        pts = nxt
    return pts


def join_strokes(strokes: list, max_gap: float) -> list:
    """把端点相邻的碎笔画接成一条长笔画（骨架在交叉/断裂处会切碎，接起来更顺、抬笔更少）。"""
    if max_gap <= 0 or len(strokes) < 2:
        return strokes
    n = len(strokes)
    used = [False] * n

    def dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def nearest(pt):
        best_j, best_d, best_end = -1, max_gap, 0
        for j in range(n):
            if used[j]:
                continue
            s = strokes[j]
            d0, d1 = dist(s[0], pt), dist(s[-1], pt)
            if d0 <= best_d:
                best_j, best_d, best_end = j, d0, 0
            if d1 <= best_d:
                best_j, best_d, best_end = j, d1, 1
        return best_j, best_end

    out = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        chain = list(strokes[i])
        while True:                                   # 往后接
            j, end = nearest(chain[-1])
            if j < 0:
                break
            seg = list(strokes[j])
            used[j] = True
            chain = chain + (seg if end == 0 else seg[::-1])
        while True:                                   # 往前接
            j, end = nearest(chain[0])
            if j < 0:
                break
            seg = list(strokes[j])
            used[j] = True
            chain = (seg if end == 1 else seg[::-1]) + chain
        out.append(chain)
    return out


def prune_spurs(strokes: list, max_len: float) -> list:
    """去掉"刺"：很短、且一端贴着另一条长线的笔画（骨架在交叉处会岔出小毛刺）。"""
    if max_len <= 0 or not strokes:
        return strokes
    def length(s):
        return sum(math.hypot(s[i][0] - s[i - 1][0], s[i][1] - s[i - 1][1])
                   for i in range(1, len(s)))

    long_pts = {}
    cell = 3.0
    for s in strokes:
        if length(s) >= max_len * 2 and len(s) >= 2:
            for p in s:
                long_pts.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(p)

    def touches(p):
        gx, gy = int(p[0] // cell), int(p[1] // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for q in long_pts.get((gx + dx, gy + dy), ()):
                    if abs(q[0] - p[0]) <= 1.6 and abs(q[1] - p[1]) <= 1.6:
                        return True
        return False

    out = []
    for s in strokes:
        if length(s) < max_len and (touches(s[0]) or touches(s[-1])):
            continue
        out.append(s)
    return out


def plan_line(img: Image.Image, W: int, H: int, args) -> tuple[list, dict]:
    scale = min(W / float(img.width), H / float(img.height))
    nw, nh = max(2, int(img.width * scale)), max(2, int(img.height * scale))
    small = img.resize((nw, nh), Image.LANCZOS)
    if args.mode == "sketch":
        blur = small.filter(ImageFilter.GaussianBlur(args.blur))
        g = np.asarray(blur.convert("L"), dtype=np.float32)
        gy, gx = np.gradient(g)
        mag = np.hypot(gx, gy)
        mask = mag > float(args.edge)
        src = "sobel(edge=%s, blur=%s)" % (args.edge, args.blur)
    else:
        g = _gray_array(small, invert_auto=not getattr(args, "no_invert", False))
        mask = g < float(args.thresh)
        src = "threshold(%s)" % args.thresh
    if args.dilate:
        m2 = mask.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx or dy:
                    m2 |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
        mask = m2
    clean_area = int(getattr(args, "clean", 0) or 0)
    if clean_area > 0:
        try:
            import cv2
            m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE,
                                 np.ones((3, 3), np.uint8))
            n_cc, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            keep = np.zeros_like(m)
            for i in range(1, n_cc):
                if stats[i, cv2.CC_STAT_AREA] >= clean_area:
                    keep[lab == i] = 1
            mask = keep.astype(bool)
        except Exception:  # noqa: BLE001
            pass
    skel = thin_zhang_suen(mask)
    raw = trace_skeleton(skel)
    ox, oy = (W - nw) // 2, (H - nh) // 2
    strokes = []
    step = float(getattr(args, "step", 0.0) or 0.0)
    smooth_n = int(getattr(args, "smooth", 0) or 0)
    for line in raw:
        if len(line) < args.min_len:
            continue
        simp = rdp([(float(x), float(y)) for x, y in line], args.simplify)
        if len(simp) < 2:
            continue
        pts = [(x + ox, y + oy) for x, y in simp]
        if step > 0:
            pts = resample(pts, step)
        if smooth_n > 0:
            pts = smooth_polyline(pts, smooth_n)
        if len(pts) < 2:
            continue
        strokes.append([[float(x), float(y)] for x, y in pts])
    spur = float(getattr(args, "spur", 0.0) or 0.0)
    if spur > 0:
        strokes = prune_spurs(strokes, spur)
    join_gap = float(getattr(args, "join", 0.0) or 0.0)
    if join_gap > 0:
        strokes = join_strokes(strokes, join_gap)
    meta = {"mode": args.mode, "source": src, "image_scaled": [nw, nh],
            "skeleton_px": int(skel.sum()), "polylines_raw": len(raw)}
    return strokes, meta


def plan_grid(img: Image.Image, W: int, H: int, args) -> tuple[list, dict]:
    cell = max(3, int(args.cell))
    cols, rows = max(1, W // cell), max(1, H // cell)
    # 先在原分辨率判「有没有墨」，再按格子统计墨占比（取平均会把细线抹掉）
    sc = min(W / float(img.width), H / float(img.height))
    nw, nh = max(2, int(img.width * sc)), max(2, int(img.height * sc))
    small = img.resize((nw, nh), Image.LANCZOS)
    mask = _gray_array(small, invert_auto=not getattr(args, "no_invert", False)) < float(args.thresh)
    ox, oy = (W - cols * cell) // 2, (H - rows * cell) // 2
    strokes = []
    for r in range(rows):
        y0 = oy + r * cell
        ry0, ry1 = max(0, int(y0 / sc)), max(1, min(nh, int((y0 + cell) / sc)))
        for c in range(cols):
            x0 = ox + c * cell
            rx0, rx1 = max(0, int(x0 / sc)), max(1, min(nw, int((x0 + cell) / sc)))
            block = mask[ry0:ry1, rx0:rx1]
            if block.size and (block.mean() >= 0.08 or block.sum() >= 2):
                x = x0 + cell // 2
                y = y0 + cell // 2
                strokes.append([[float(x), float(y)], [float(x + 1), float(y)]])
    return strokes, {"mode": "grid", "cell": cell, "grid": [cols, rows]}


def plan_engrave(img: Image.Image, W: int, H: int, args) -> tuple[list, dict]:
    """多级等值线（版画/雕刻风格）：把灰度按若干阈值切层，逐层取轮廓线。

    比 sketch 多保留明暗层次（中间调不再是空白），比逐像素排线省很多笔画
    —— 轮廓线本身连续，同样墨量下路径更短、更像"画画"。
    """
    if isinstance(args.levels, str):
        levels = [float(v) for v in args.levels.replace(" ", "").split(",") if v]
    else:
        levels = [float(v) for v in args.levels]
    if not levels:
        levels = [90.0, 130.0, 170.0, 210.0]
    scale = min(W / float(img.width), H / float(img.height))
    nw, nh = max(2, int(img.width * scale)), max(2, int(img.height * scale))
    small = img.resize((nw, nh), Image.LANCZOS)
    g = _gray_array(small, invert_auto=not getattr(args, "no_invert", False))
    ox, oy = (W - nw) // 2, (H - nh) // 2
    strokes: list = []
    per_level: list[int] = []
    for thr in levels:
        mask = g < thr
        if not mask.any():
            per_level.append(0)
            continue
        skel = thin_zhang_suen(mask)
        raw = trace_skeleton(skel)
        cnt = 0
        for line in raw:
            if len(line) < args.min_len:
                continue
            simp = rdp([(float(x), float(y)) for x, y in line], args.simplify)
            if len(simp) < 2:
                continue
            strokes.append([[x + ox, y + oy] for x, y in simp])
            cnt += 1
        per_level.append(cnt)
    meta = {"mode": "engrave", "source": "iso(levels=%s)" % levels,
            "image_scaled": [nw, nh], "levels": per_level}
    return strokes, meta


def plan_dots(img: Image.Image, W: int, H: int, args) -> tuple[list, dict]:
    """网点（点彩）画法：照片切成小格，每格按明暗点一个直径不同的圆点。

    这是唯一真正「像照片」的画法：密集圆点在正常观看距离下就是一张缩小版照片，
    而且每个点只是一次落笔，成本 ~30 ms，比描线便宜得多。
    """
    pitch = float(getattr(args, "pitch", 12.0) or 12.0)
    levels = max(2, int(getattr(args, "dot_levels", 4) or 4))
    bmin = float(getattr(args, "dot_min", 2.0) or 2.0)
    bmax = float(getattr(args, "dot_max", 0.0) or pitch * 0.92)
    scale = min(W / float(img.width), H / float(img.height))
    nw, nh = max(2, int(img.width * scale)), max(2, int(img.height * scale))
    small = img.resize((nw, nh), Image.LANCZOS).convert("L")
    g = np.asarray(small, dtype=np.float32)
    ox, oy = (W - nw) // 2, (H - nh) // 2
    cols, rows = max(1, int(nw / pitch)), max(1, int(nh / pitch))
    strokes: list = []
    tiers: list = []
    sizes: list = []
    for t in range(1, levels):
        start = len(strokes)
        for r in range(rows):
            y0, y1 = int(r * pitch), int((r + 1) * pitch)
            for c in range(cols):
                x0, x1 = int(c * pitch), int((c + 1) * pitch)
                block = g[y0:y1, x0:x1]
                if block.size == 0:
                    continue
                dark = 1.0 - float(block.mean()) / 255.0
                if int(round(dark * (levels - 1))) != t:
                    continue
                cx = ox + (x0 + x1) * 0.5
                cy = oy + (y0 + y1) * 0.5
                strokes.append([[cx, cy], [cx, cy]])
        tiers.append([start, len(strokes)])
        sizes.append(round(bmin + (bmax - bmin) * t / float(levels - 1), 1))
    meta = {"mode": "dots", "source": "dots(pitch=%s levels=%s)" % (pitch, levels),
            "image_scaled": [nw, nh], "tiers": tiers, "tier_sizes": sizes}
    return strokes, meta


def render_preview_dots(strokes, meta, W: int, H: int) -> Image.Image:
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for (start, end), size in zip(meta.get("tiers") or [], meta.get("tier_sizes") or []):
        r = max(0.8, float(size) / 2.0)
        for s in strokes[start:end]:
            x, y = s[0]
            d.ellipse([x - r, y - r, x + r, y + r], fill=(20, 20, 20))
    return im


def plan(img: Image.Image, W: int, H: int, args):
    if args.mode == "grid":
        strokes, meta = plan_grid(img, W, H, args)
    elif args.mode == "engrave":
        strokes, meta = plan_engrave(img, W, H, args)
    elif args.mode == "dots":
        strokes, meta = plan_dots(img, W, H, args)
    else:
        strokes, meta = plan_line(img, W, H, args)
    meta["strokes"] = len(strokes)
    meta["vertices"] = sum(len(s) for s in strokes)
    if args.max_strokes and len(strokes) > args.max_strokes and not meta.get("tiers"):
        strokes = sorted(strokes, key=len, reverse=True)[: args.max_strokes]
        meta["truncated_to"] = args.max_strokes
        meta["strokes"] = len(strokes)
        meta["vertices"] = sum(len(s) for s in strokes)
    return strokes, meta


def _ink_map(im: Image.Image, side: int = 256) -> np.ndarray:
    g = np.asarray(im.convert("L").resize((side, side), Image.BOX), dtype=np.float32)
    if g.mean() < 115:
        g = 255.0 - g
    return (255.0 - g) / 255.0          # 0=纸，1=墨


def compare_with_source(src: Image.Image, preview: Image.Image) -> dict:
    """预览 vs 原图的数值相似度（不用眼睛也能判断像不像）。

    pearson : 墨迹图的相关性（1=完全一致）
    ink_iou : 二值墨迹交集/并集（对线条位置最敏感）
    """
    a = _ink_map(src)
    b = _ink_map(preview)
    a1, b1 = a.ravel(), b.ravel()
    den = (a1.std() * b1.std())
    pear = float(((a1 - a1.mean()) * (b1 - b1.mean())).mean() / den) if den > 1e-9 else 0.0
    am, bm = a > 0.12, b > 0.12
    inter = float((am & bm).sum())
    union = float((am | bm).sum()) or 1.0
    return {"pearson": round(pear, 4), "ink_iou": round(inter / union, 4),
            "src_ink": round(float(am.mean()), 4), "preview_ink": round(float(bm.mean()), 4)}


def render_preview(strokes, W: int, H: int, width: int = 2) -> Image.Image:
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for s in strokes:
        if len(s) >= 2:
            d.line([(p[0], p[1]) for p in s], fill=(25, 25, 25), width=width, joint="curve")
    return im


# --------------------------------------------------------------------------- #
# 笔形 / 粗细（画图没有命令行参数，靠 UI Automation 设「画笔」和「大小」）
# --------------------------------------------------------------------------- #
def set_paint_brush(size: int, brush: str = "Brush") -> dict:
    """把画图的笔形/粗细设好。

    照片画不好的头号根因是 1 px 铅笔：铅笔/炭笔的线宽约占画面 0.5-1%，
    而 1 px 在 800 px 画布上只有 0.12%。画图的「画笔」最大 90 px，用 UIA 设。
    """
    import subprocess
    if size <= 1:
        return {"ok": True, "size": 1, "mode": "pencil"}
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paint-brush.ps1")
    if not os.path.exists(script):
        return {"ok": False, "err": "paint-brush.ps1 missing"}
    try:
        out = subprocess.run(["pwsh", "-NoProfile", "-File", script,
                              "-Size", str(int(size)), "-Brush", brush],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": repr(exc)}
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"ok": False, "err": "no json", "stdout": (out.stdout or "")[-300:]}


# --------------------------------------------------------------------------- #
# 执行：快速笔画器（开环 + 周期闭环校正）
# --------------------------------------------------------------------------- #
def _gain_table() -> list:
    try:
        from pointer_profile import get as _get
        prof = _get()
        if prof and prof.get("gain_table"):
            return [(float(m), float(g)) for m, g in prof["gain_table"] if float(g) > 0]
    except Exception:  # noqa: BLE001
        pass
    import brain
    return list(brain._GAIN_TABLE)


def _gain(m: float, table) -> float:
    if m <= table[0][0]:
        return table[0][1]
    if m >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        m0, g0 = table[i]
        m1, g1 = table[i + 1]
        if m0 <= m <= m1:
            t = (m - m0) / (m1 - m0)
            return g0 + (g1 - g0) * t
    return table[-1][1]


def counts_for(px: float, table) -> int:
    """想要光标走 px 像素 -> 该发多少个 count（按实测增益反推）。"""
    if px == 0:
        return 0
    sign = 1 if px > 0 else -1
    a = abs(px)
    m = a                                    # 起点：1:1
    for _ in range(6):
        g = _gain(m, table)
        if g <= 0:
            break
        m = a / g
        if m < 1:
            m = 1.0
            break
    return sign * max(1, int(round(m)))


def counts_vec(dx: float, dy: float, table) -> tuple[int, int]:
    s = math.hypot(dx, dy)
    if s < 1e-9:
        return (0, 0)
    m = counts_for(s, table)
    k = m / s
    cx, cy = int(round(dx * k)), int(round(dy * k))
    if cx == 0 and cy == 0:
        cx = 1 if dx > 0 else -1 if dx < 0 else 0
    return (cx, cy)


VK_ESCAPE = 0x1B


def stop_requested() -> bool:
    """用户按 ESC = 立刻停笔（PC 键盘；板子只发键、收不到键）。"""
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
    except Exception:  # noqa: BLE001 - 非 Windows/受限环境就别拦
        return False


def run_strokes(b, strokes, args, table, paint_hwnd: int, log: list) -> dict:
    res = {"strokes_done": 0, "interruptions": 0, "aborted": False, "ms": 0}
    t0 = time.time()
    for si, pts in enumerate(strokes):
        if len(pts) < 2:
            continue
        if args.stop_key and stop_requested():
            res["aborted"] = True
            res["reason"] = "用户按了 ESC，已停手"
            log.append({"stroke": si, "event": "esc_stop"})
            break
        if dr is not None and paint_hwnd and int(dr.foreground()) != int(paint_hwnd):
            res["aborted"] = True
            res["reason"] = "前台不再是画图窗口，已停手"
            break
        r = b.locate(int(round(pts[0][0])), int(round(pts[0][1])), tol=1)
        if not r.get("ok"):
            continue
        b.bridge.mouse_btn("left", "down")
        down = True
        prev = pts[0]
        sent = 0
        try:
            for p in pts[1:]:
                dx, dy = p[0] - prev[0], p[1] - prev[1]
                if abs(dx) + abs(dy) < 0.5:
                    continue
                b.bridge.mouse_move(*counts_vec(dx, dy, table))
                prev = p
                sent += 1
                if args.stop_key and sent % 4 == 0 and stop_requested():
                    b.bridge.mouse_btn("left", "up")
                    down = False
                    res["aborted"] = True
                    res["reason"] = "用户按了 ESC，已停手"
                    log.append({"stroke": si, "at": [round(p[0]), round(p[1])],
                                "event": "esc_stop"})
                    break
                if args.anchor_every and sent % args.anchor_every == 0:
                    from brain import cursor_pos
                    ax, ay = cursor_pos()
                    ex, ey = p[0] - ax, p[1] - ay
                    if abs(ex) + abs(ey) > args.interfere_px:
                        b.bridge.mouse_btn("left", "up")
                        down = False
                        res["interruptions"] += 1
                        log.append({"stroke": si, "at": [round(p[0]), round(p[1])],
                                    "err": [round(ex), round(ey)],
                                    "event": "user_interference"})
                        break
                    if max(abs(ex), abs(ey)) > 1:
                        b.bridge.mouse_move(*counts_vec(ex, ey, table))
        finally:
            if down:
                b.bridge.mouse_btn("left", "up")
        if res.get("aborted"):
            break
        res["strokes_done"] += 1
        if args.progress and si % args.progress == 0:
            print("   ... %d/%d 笔" % (si + 1, len(strokes)), file=sys.stderr, flush=True)
    res["ms"] = int((time.time() - t0) * 1000)
    return res


# --------------------------------------------------------------------------- #
# 画布
# --------------------------------------------------------------------------- #
def parse_canvas(spec: str) -> list[int]:
    s = spec.lower().replace(" ", "")
    if "x" in s and "," not in s:
        w, h = s.split("x", 1)
        return [0, 0, int(w), int(h)]
    parts = [int(float(v)) for v in s.split(",")]
    if len(parts) != 4:
        raise SystemExit("--canvas 要写 'x1,y1,x2,y2' 或 '宽x高'")
    return parts


def clear_canvas() -> bool:
    """把当前前台窗口（画图）里的内容清空：Ctrl+A -> Delete。"""
    try:
        u = ctypes.windll.user32
        VK_CONTROL, VK_A, VK_DELETE, KEYUP = 0x11, 0x41, 0x2E, 0x0002
        u.keybd_event(VK_CONTROL, 0, 0, 0)
        u.keybd_event(VK_A, 0, 0, 0)
        u.keybd_event(VK_A, 0, KEYUP, 0)
        u.keybd_event(VK_CONTROL, 0, KEYUP, 0)
        time.sleep(0.15)
        u.keybd_event(VK_DELETE, 0, 0, 0)
        u.keybd_event(VK_DELETE, 0, KEYUP, 0)
        time.sleep(0.25)
        return True
    except Exception:  # noqa: BLE001
        return False


def _blank_png(w: int, h: int) -> str:
    """准备一张 w x h 的白图：用它启动画图，画布尺寸就由它决定（画图没有尺寸参数）。"""
    os.makedirs(INBOX, exist_ok=True)
    path = os.path.join(INBOX, "blank-%dx%d.png" % (w, h))
    if not (os.path.exists(path) and Image.open(path).size == (w, h)):
        Image.new("RGB", (w, h), (255, 255, 255)).save(path)
    return path


def ensure_paint(args=None) -> dict | None:
    """找画图窗口；没有就自己打开（可指定画布尺寸），并摆到屏幕右侧。

    只在「画图是我们自己打开的时候」才动窗口，用户已经开着的窗口一概不碰。
    """
    import subprocess
    paint = dr.find_paint() if dr is not None else None
    if paint is not None:
        return paint
    if args is not None and getattr(args, "no_launch_paint", False):
        return None
    cw = ch = 0
    try:
        cw, ch = (int(v) for v in str(getattr(args, "paint_canvas", "1040x760") or "").lower().split("x"))
    except Exception:  # noqa: BLE001
        cw = ch = 0
    args_list = ["cmd", "/c", "start", "", "mspaint"]
    if cw >= 200 and ch >= 200:
        args_list.append(_blank_png(cw, ch))
    CREATE_NO_WINDOW = 0x08000000
    try:
        subprocess.Popen(args_list, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return None
    for _ in range(80):                      # 最多等 20 秒
        time.sleep(0.25)
        paint = dr.find_paint()
        if paint is not None:
            break
    if paint is None:
        return None
    time.sleep(1.6)                          # 等它把画布画出来
    spec = str(getattr(args, "paint_size", "") or "")
    try:
        w, h = (int(v) for v in spec.lower().split("x"))
    except Exception:  # noqa: BLE001
        w = h = 0
    if cw >= 200 and ch >= 200:              # 窗口 = 画布 + 工具栏/功能区余量
        w, h = cw + 280, ch + 320
    if w >= 400 and h >= 300:
        sw = int(ctypes.windll.user32.GetSystemMetrics(0))
        sh = int(ctypes.windll.user32.GetSystemMetrics(1))
        w, h = min(w, sw - 40), min(h, sh - 80)
        x, y = max(0, sw - w - 20), max(0, (sh - h) // 2)
        try:
            dr.set_rect(int(paint["hwnd"]), x, y, w, h)
            time.sleep(0.9)
            paint = dr.find_paint() or paint
        except Exception:  # noqa: BLE001
            pass
    return paint


def detect_canvas_auto(args=None) -> tuple[list[int], int, str]:
    if dr is None:
        raise SystemExit("检测画布需要 draw-rage.py: %r" % (_DR_ERR,))
    import eyes
    paint = ensure_paint(args)
    if not paint:
        raise SystemExit("没找到画图窗口（自动打开失败，请手动打开 mspaint）")
    if args is not None and getattr(args, "clear_canvas", False):
        dr.focus_hwnd(int(paint["hwnd"]))
        clear_canvas()
        time.sleep(0.35)
        paint = dr.find_paint() or paint
    shot_path = os.path.join(INBOX, "canvas-%s.png" % time.strftime("%H%M%S"))
    os.makedirs(INBOX, exist_ok=True)
    shot = eyes.capture(shot_path)
    img = Image.open(shot.path)
    det = dr.detect_canvas(img, paint["rect"])
    if not det.get("ok"):
        raise SystemExit("画布检测失败: %s（窗口 %s）" % (det.get("reason"), paint["rect"]))
    return det["box"], int(paint["hwnd"]), shot.path


def load_plan_file(path: str):
    """读「文字计划」，返回 (strokes, canvas_box|None, kind)。

    两种格式：
      1) 本工具输出的 plan JSON：{"canvas_box":[...], "strokes":[[[x,y],...],...]}
         —— 坐标已是窗口坐标，画布由 canvas_box 指定；
      2) 纯文本，一行一条折线：
             # 注释行
             canvas: 24,24,976,976        （可选，画布在窗口里的位置）
             100,100 200,150 300,120      （x,y 空格或逗号分隔，分号也认）
         没有 canvas: 就当作画布局部坐标（真画时自动检测画布或 --canvas 指定）。
    """
    txt = open(path, encoding="utf-8").read()
    obj = None
    try:
        obj = json.loads(txt)
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("strokes"), list):
        strokes = []
        for s in obj["strokes"]:
            pts = [(float(p[0]), float(p[1])) for p in s if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(pts) >= 2:
                strokes.append(pts)
        box = obj.get("canvas_box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            box = [int(float(v)) for v in box]
        else:
            box = None
        return strokes, box, "json"

    strokes = []
    box = None
    num_re = re.compile(r"-?\d+(?:\.\d+)?")
    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("canvas:"):
            nums = num_re.findall(line.split(":", 1)[1])
            if len(nums) >= 4:
                box = [int(float(v)) for v in nums[:4]]
            continue
        # 一行可以写多条折线（分号分隔）；每段里数字按 xy 配对
        for chunk in line.split(";"):
            nums = num_re.findall(chunk)
            if len(nums) < 4:
                continue
            pts = [(float(nums[i]), float(nums[i + 1]))
                   for i in range(0, len(nums) - 1, 2)]
            if len(pts) >= 2:
                strokes.append(pts)
    return strokes, box, "text"


def ink_report(after_path: str, box, before_path: str | None) -> dict:
    try:
        img = Image.open(after_path)
        st = dr.ink_stats(img, box)
        out = {"canvas_ink": st}
        if before_path and os.path.exists(before_path):
            out["changed_px"] = dr.changed_pixels(before_path, after_path, box)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"err": repr(exc)}


# --------------------------------------------------------------------------- #
def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文/符号输出会 UnicodeEncodeError；统一改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="把图片/文字折线画进画图（先预览，--draw 才落笔）")
    ap.add_argument("image", nargs="?", help="图片路径（任意格式先跑 tools/ingest-image.py）")
    ap.add_argument("--from-plan", default=None,
                    help="文字计划：plan JSON，或每行 'x,y x,y ...' 的文本（也认 # 注释、canvas:x1,y1,x2,y2）")
    ap.add_argument("--mode", default="line",
                    choices=["line", "sketch", "grid", "engrave", "dots"])
    ap.add_argument("--pitch", type=float, default=12.0, help="dots 的网点间距 px")
    ap.add_argument("--dot-levels", type=int, default=4, help="dots 的明暗级数")
    ap.add_argument("--dot-min", type=float, default=2.0, help="dots 最浅一级的笔径 px")
    ap.add_argument("--dot-max", type=float, default=0.0, help="dots 最深一级的笔径 px（0=0.92*pitch）")
    ap.add_argument("--canvas", default=None, help="'x1,y1,x2,y2' 或 '宽x高'；不给就检测画图窗口")
    ap.add_argument("--margin", type=int, default=24, help="画布内边距 px")
    ap.add_argument("--thresh", type=float, default=150, help="line/grid 阈值（暗=墨）")
    ap.add_argument("--edge", type=float, default=45, help="sketch 的 Sobel 阈值")
    ap.add_argument("--blur", type=float, default=1.2, help="sketch 模糊半径")
    ap.add_argument("--cell", type=int, default=10, help="grid 的格子边长")
    ap.add_argument("--levels", default="90,130,170,210",
                    help="engrave 的灰度分层（逗号分隔，值越小只留最暗处）")
    ap.add_argument("--min-len", type=int, default=6, help="丢弃短于该长度(px)的折线")
    ap.add_argument("--simplify", type=float, default=1.2, help="RDP 简化容差 px")
    ap.add_argument("--dilate", action="store_true", help="笔画偏细时先膨胀 1 px")
    ap.add_argument("--no-invert", action="store_true",
                    help="深底照片不做反色（默认深底图会反色成'亮处落笔'，负片效果）")
    ap.add_argument("--brush-size", type=int, default=1,
                    help="落笔粗细 px（1=1px 铅笔；4-8 更像铅笔/炭笔画，需要画图窗口在前台）")
    ap.add_argument("--step", type=float, default=0.0,
                    help="line 模式按弧长重采样步长 px（0=关；2-4 让曲线顺滑）")
    ap.add_argument("--smooth", type=int, default=0,
                    help="line 模式平滑遍数（0=关；2-3 磨掉像素台阶）")
    ap.add_argument("--join", type=float, default=0.0,
                    help="端点相距多少 px 内就连成一条笔画（0=关；2-3 让线条连贯）")
    ap.add_argument("--clean", type=int, default=0,
                    help="line 模式先去掉面积小于 N 像素的碎块（0=关；20-60 清杂点）")
    ap.add_argument("--spur", type=float, default=0.0,
                    help="去掉短于 N px 且贴在其他线上的毛刺（0=关；5-8 清乱线）")
    ap.add_argument("--brush-kind", default="Brush",
                    choices=["Brush", "Marker", "Crayon", "OilBrush", "WaterColor", "AirBrush"],
                    help="画图的笔形（Brush=画笔, Marker=记号笔, Crayon=蜡笔…）")
    ap.add_argument("--max-strokes", type=int, default=0, help="最多保留多少笔（0=不限）")
    ap.add_argument("--preview-width", type=int, default=2)
    ap.add_argument("--draw", action="store_true", help="真的落笔（默认只规划）")
    ap.add_argument("--anchor-every", type=int, default=8, help="每 N 个顶点做一次闭环校正")
    ap.add_argument("--interfere-px", type=int, default=40, help="偏差超过它就判定用户抢鼠标")
    ap.add_argument("--stop-key", default="esc", choices=["esc", "none"],
                    help="按这个键立刻停笔（默认 esc；none=不检测）")
    ap.add_argument("--no-countdown", action="store_true",
                    help="真画前不倒数 5 秒（默认倒数，按 ESC 可取消）")
    ap.add_argument("--progress", type=int, default=0, help="每 N 笔打印进度（0=不打印）")
    ap.add_argument("--transport", default="tcp", choices=["tcp", "uart"])
    ap.add_argument("--out", default=None, help="预览/规划输出前缀")
    ap.add_argument("--no-window-move", action="store_true", help="兼容参数：永不移动窗口（本工具本来就不移）")
    ap.add_argument("--no-launch-paint", action="store_true",
                    help="找不到画图窗口时不要自动打开（默认会自动打开）")
    ap.add_argument("--paint-size", default="",
                    help="自动打开画图时的窗口大小，如 1400x1100（默认按画布尺寸自动算）")
    ap.add_argument("--paint-canvas", default="1040x760",
                    help="自动打开画图时用的白画布尺寸（画图没有尺寸参数，靠打开一张白图来定）")
    ap.add_argument("--clear-canvas", action="store_true",
                    help="开画前先清空画图里已有的内容（Ctrl+A / Delete）")
    a = ap.parse_args(argv)

    if not a.from_plan and not a.image:
        print(json.dumps({"ok": False, "err": "二选一：给图片路径，或给 --from-plan 文字计划"},
                         ensure_ascii=False))
        return 1
    if a.image and not os.path.exists(a.image):
        print(json.dumps({"ok": False, "err": "图片不存在: %s" % a.image}, ensure_ascii=False))
        return 1
    os.makedirs(INBOX, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = a.out or os.path.join(INBOX, "plan-%s" % ts)

    img = None
    paint_hwnd = 0
    before_shot = None
    plan_box = None
    strokes_canvas = None
    strokes_win = None

    if a.from_plan:
        if not os.path.exists(a.from_plan):
            print(json.dumps({"ok": False, "err": "计划文件不存在: %s" % a.from_plan},
                             ensure_ascii=False))
            return 1
        strokes_def, plan_box, kind = load_plan_file(a.from_plan)
        if not strokes_def:
            print(json.dumps({"ok": False, "err": "计划里没解析出折线（每行至少两个 x,y）",
                              "file": a.from_plan}, ensure_ascii=False))
            return 3
        if plan_box:                       # JSON 里自带画布：坐标已是窗口坐标
            box = [int(v) for v in plan_box]
            strokes_win = [[[float(p[0]), float(p[1])] for p in s] for s in strokes_def]
            strokes_canvas = [[[float(p[0]) - box[0], float(p[1]) - box[1]] for p in s]
                              for s in strokes_def]
        else:                              # 文字计划：坐标算画布局部坐标
            if a.canvas:
                b = parse_canvas(a.canvas)
                box = [b[0] + a.margin, b[1] + a.margin, b[2] - a.margin, b[3] - a.margin]
            elif a.draw:
                det, paint_hwnd, before_shot = detect_canvas_auto(a)
                box = [det[0] + a.margin, det[1] + a.margin, det[2] - a.margin, det[3] - a.margin]
            else:
                box = [0, 0, 1000, 1000]
            strokes_canvas = [[[float(p[0]), float(p[1])] for p in s] for s in strokes_def]
            strokes_win = [[[p[0] + box[0], p[1] + box[1]] for p in s] for s in strokes_canvas]
        canvas = [box[2] - box[0], box[3] - box[1]]
        meta = {"mode": "plan:" + kind, "source": a.from_plan,
                "strokes": len(strokes_win),
                "vertices": sum(len(s) for s in strokes_win), "plan_ms": 0}
    else:
        img = load_rgb(a.image)
        if a.canvas:
            box = parse_canvas(a.canvas)
            box = [box[0] + a.margin, box[1] + a.margin, box[2] - a.margin, box[3] - a.margin]
            canvas = [box[2] - box[0], box[3] - box[1]]
        elif a.draw:
            box, paint_hwnd, before_shot = detect_canvas_auto(a)
            box = [box[0] + a.margin, box[1] + a.margin, box[2] - a.margin, box[3] - a.margin]
            canvas = [box[2] - box[0], box[3] - box[1]]
        else:
            # 纯预览：默认 1000x1000 画布
            canvas = [1000, 1000]
            box = [0, 0, canvas[0], canvas[1]]

        if canvas[0] < 50 or canvas[1] < 50:
            print(json.dumps({"ok": False, "err": "画布太小", "canvas": canvas}, ensure_ascii=False))
            return 2

        t_plan = time.time()
        strokes, meta = plan(img, canvas[0], canvas[1], a)
        meta["plan_ms"] = int((time.time() - t_plan) * 1000)
        if not strokes:
            print(json.dumps({"ok": False, "err": "没规划出笔画（阈值太严？图太淡？）",
                              "meta": meta}, ensure_ascii=False))
            return 3

        # 画布局部坐标（预览/比对用），窗口坐标（真画用）
        strokes_canvas = strokes
        strokes_win = [[[p[0] + box[0], p[1] + box[1]] for p in s] for s in strokes]

    # 预览 + 计划落盘（预览线宽 = 实际落笔粗细，免得预览比真画细一大截）
    if meta.get("tiers"):
        prev = render_preview_dots(strokes_canvas, meta, canvas[0], canvas[1])
    else:
        prev_w = int(a.brush_size) if getattr(a, "brush_size", 0) > 0 else a.preview_width
        prev = render_preview(strokes_canvas, canvas[0], canvas[1], prev_w)
    prev_path = base + "-preview.png"
    prev.save(prev_path, "PNG")
    similarity = None
    if img is not None:
        # 参考图：把原图按同样比例放进同样大小画布（用于数值比对）
        sc = min(canvas[0] / float(img.width), canvas[1] / float(img.height))
        rw, rh = max(2, int(img.width * sc)), max(2, int(img.height * sc))
        ref = Image.new("RGB", (canvas[0], canvas[1]), (255, 255, 255))
        ref.paste(img.resize((rw, rh), Image.LANCZOS),
                  ((canvas[0] - rw) // 2, (canvas[1] - rh) // 2))
        similarity = compare_with_source(ref, prev)
    plan_path = base + "-plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"image": a.image or a.from_plan, "mode": meta.get("mode") or a.mode,
                   "canvas_box": box, "meta": meta, "strokes": strokes_win},
                  f, ensure_ascii=False)

    n_verts = sum(len(s) for s in strokes_win)
    est_s = n_verts * 0.013 + len(strokes_win) * 0.06
    summary = {"ok": True, "mode": meta.get("mode") or a.mode, "canvas_box": box,
               "image": ({"size": list(img.size)} if img is not None else None),
               "meta": meta, "strokes": len(strokes_win), "vertices": n_verts,
               "est_seconds": round(est_s, 1),
               "similarity": similarity,
               "preview": prev_path, "plan": plan_path, "draw": bool(a.draw)}

    if not a.draw:
        summary["hint"] = "只规划没落笔。确认预览没问题后，加 --draw 真画。"
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # ---- 真画 ----
    if not a.no_countdown:
        print("   5 秒后开始画，按 ESC 取消 …", file=sys.stderr, flush=True)
        for _ in range(5):
            time.sleep(1.0)
            if a.stop_key and stop_requested():
                print(json.dumps({"ok": False, "err": "已取消（按了 ESC）"}, ensure_ascii=False))
                return 5
    from brain import Brain
    if not paint_hwnd and dr is not None:          # 指定了 --canvas 但画图还没开
        paint = ensure_paint(a)
        if paint:
            paint_hwnd = int(paint["hwnd"])
    if dr is not None and paint_hwnd:
        if not dr.focus_hwnd(paint_hwnd):
            print(json.dumps({"ok": False, "err": "切不到画图窗口", "canvas_box": box},
                             ensure_ascii=False))
            return 4
        if a.clear_canvas:
            clear_canvas()
            time.sleep(0.35)
        # 清空会把工具切到「选择」，所以设笔形/粗细必须放在清空之后
        if getattr(a, "brush_size", 1) > 1 and not meta.get("tiers"):
            summary["brush"] = set_paint_brush(int(a.brush_size), a.brush_kind)
            time.sleep(0.2)
    log: list = []
    table = _gain_table()
    if meta.get("tiers"):
        # 网点：按明暗级分组，每组换一次笔径，然后一组点点点
        total = {"strokes_done": 0, "interruptions": 0, "aborted": False, "ms": 0}
        summary["brush"] = []
        with Brain(transport=a.transport) as b:
            b.release()
            for (s0, s1), size in zip(meta["tiers"], meta["tier_sizes"]):
                if s1 <= s0:
                    continue
                summary["brush"].append(set_paint_brush(int(round(size)), a.brush_kind))
                r = run_strokes(b, strokes_win[s0:s1], a, table, paint_hwnd, log)
                total["strokes_done"] += r.get("strokes_done", 0)
                total["interruptions"] += r.get("interruptions", 0)
                total["ms"] += r.get("ms", 0)
                if r.get("aborted"):
                    total["aborted"] = True
                    total["reason"] = r.get("reason")
                    break
            b.release()
        res = total
    else:
        with Brain(transport=a.transport) as b:
            b.release()
            res = run_strokes(b, strokes_win, a, table, paint_hwnd, log)
            b.release()
    summary["run"] = res
    summary["events"] = log[:20]

    # 画完留证据
    try:
        import eyes
        after = eyes.capture(os.path.join(INBOX, "after-%s.png" % ts))
        summary["after_shot"] = after.path
        summary["ink"] = ink_report(after.path, box, before_shot)
    except Exception as exc:  # noqa: BLE001
        summary["ink"] = {"err": repr(exc)}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not res.get("aborted") else 4


if __name__ == "__main__":
    sys.exit(main())
