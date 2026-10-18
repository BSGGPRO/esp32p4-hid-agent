#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/poster-plan.py — 照片 -> 画图「色块画」计划（用画图自带调色板的 20 个颜色）。

为什么是 20 个固定色：画图的「编辑颜色」对话框是 WinUI 模态窗，UIA 一碰就卡死
（实测把整个画图卡住），所以只能用调色板格子（点一下 = 设当前颜色）。
这 20 色是画图自带的经典调色板，够画人像：肤色→棕/粉/米/金，阴影→灰。

输出：*-poster-plan.json（分组落笔计划）+ *-preview.png（= 量化后的成品预览）
用法：python tools/poster-plan.py 照片.jpg --canvas 750x1050 --out inbox/xxx
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image

# 画图经典 20 色（调色板两行 9 格 + 黑/白），索引与 paint-color.ps1 对齐
PALETTE = [
    ("black", 0, 0, (0, 0, 0)),
    ("white", 0, 0, (255, 255, 255)),
    ("gray", 1, 0, (127, 127, 127)),
    ("darkred", 1, 1, (136, 0, 21)),
    ("red", 1, 2, (237, 28, 36)),
    ("orange", 1, 3, (255, 127, 39)),
    ("yellow", 1, 4, (255, 242, 0)),
    ("green", 1, 5, (34, 177, 76)),
    ("cyan", 1, 6, (0, 162, 232)),
    ("blue", 1, 7, (63, 72, 204)),
    ("purple", 1, 8, (163, 73, 164)),
    ("lightgray", 2, 0, (195, 195, 195)),
    ("brown", 2, 1, (185, 122, 87)),
    ("pink", 2, 2, (255, 174, 201)),
    ("gold", 2, 3, (255, 201, 14)),
    ("cream", 2, 4, (239, 228, 176)),
    ("lime", 2, 5, (181, 230, 29)),
    ("palecyan", 2, 6, (153, 217, 234)),
    ("steel", 2, 7, (112, 146, 190)),
    ("lavender", 2, 8, (200, 191, 231)),
]

MIN_AREA = 40          # 小于这个面积的色块不画（省时间，看不出来）
BRUSH_TIERS = (3, 5, 8, 12, 18, 26)   # 笔径档位（按画布大小缩放）：档位少=换笔少，画得快


def quantize(rgb: np.ndarray) -> np.ndarray:
    """每像素取最近的调色板颜色（RGB 欧氏距离）。"""
    pal = np.array([c[3] for c in PALETTE], dtype=np.int32)
    flat = rgb.reshape(-1, 3).astype(np.int32)
    d = ((flat[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2)
    return d.argmin(axis=1).reshape(rgb.shape[:2])


def region_strokes(mask: np.ndarray, brush: int, ox: int, oy: int) -> list:
    """把一个色块涂满：蛇形横扫，上下两行能接上就并成同一笔（少抬笔=快得多）。

    笔是圆的，直径≈brush，所以路径按 0.55*brush 行距走；mask 先内缩一点，
    防止笔的半径把颜色洇到旁边的白底上。
    """
    h, w = mask.shape
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return []
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    step = max(2, int(round(brush * 0.55)))
    paths = []
    cur = []
    last_x = None
    row_i = 0
    for y in range(y0, y1 + 1, step):
        yy = min(h - 1, y + step // 2)
        row = mask[yy]
        runs = []
        start = None
        for x in range(x0, x1 + 2):
            on = x <= x1 and row[x]
            if on and start is None:
                start = x
            elif not on and start is not None:
                runs.append((start, x - 1))
                start = None
        if not runs:
            continue
        if row_i % 2:
            runs = [(b, a) for a, b in runs[::-1]]
        for a, b in runs:
            lo, hi = (a, b) if a <= b else (b, a)
            if hi - lo < 1:
                continue
            n = max(2, int((hi - lo) / 6) + 1)
            pts = [[ox + lo + (hi - lo) * i / (n - 1), oy + yy] for i in range(n)]
            if a > b:
                pts = pts[::-1]
            joinable = (last_x is not None and cur and lo - 6 <= last_x <= hi + 6)
            if joinable:
                cur.extend(pts)
            else:
                if cur:
                    paths.append(cur)
                cur = pts
            last_x = pts[-1][0]
        row_i += 1
    if cur:
        paths.append(cur)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="照片 -> 画图调色板色块画计划")
    ap.add_argument("photo")
    ap.add_argument("--canvas", default="750x1050", help="画布 宽x高")
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-area", type=int, default=MIN_AREA)
    ap.add_argument("--brush-factor", type=float, default=0.7,
                    help="笔径 = 色块半径 * 这个系数（大了颜色会洇到旁边）")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.photo):
        print(json.dumps({"ok": False, "err": "照片不存在: %s" % a.photo}, ensure_ascii=False))
        return 1
    cw, ch = (int(v) for v in a.canvas.lower().split("x"))
    base = a.out or os.path.splitext(a.photo)[0] + "-poster"

    src = Image.open(a.photo).convert("RGB")
    sc = min(cw / float(src.width), ch / float(src.height))
    nw, nh = max(2, int(src.width * sc)), max(2, int(src.height * sc))
    small = src.resize((nw, nh), Image.LANCZOS).filter(__import__("PIL.ImageFilter", fromlist=["x"]).MedianFilter(5))
    ox, oy = (cw - nw) // 2, (ch - nh) // 2
    rgb = np.asarray(small, dtype=np.int32)
    labels = quantize(rgb)

    try:
        import cv2
    except ImportError:
        print(json.dumps({"ok": False, "err": "需要 opencv（pip install opencv-python-headless）"},
                         ensure_ascii=False))
        return 2

    scale = min(cw, ch) / 750.0
    tiers = sorted({max(2, int(round(t * scale))) for t in BRUSH_TIERS})

    groups = []
    total_strokes = total_verts = 0
    preview = np.full((ch, cw, 3), 255, dtype=np.uint8)
    for idx, (name, row, col, rgbv) in enumerate(PALETTE):
        if name == "white":
            continue                      # 白 = 留白，不用画
        mask = (labels == idx).astype(np.uint8)
        if not mask.any():
            continue
        # 先做一点开运算，把椒盐碎块吃掉（少几百个碎块 = 少几百笔）
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        if not mask.any():
            continue
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        per_brush = {}
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < a.min_area:
                continue
            comp = (lab == i)
            # 笔径看"这块有多粗"，不是看面积：又长又窄的块用大笔会糊一片
            dt = cv2.distanceTransform(comp.astype(np.uint8), cv2.DIST_L2, 5)
            # 用「典型半厚度」而不是最大值：又长又细的块用大笔会糊掉
            r_in = float(np.percentile(dt[comp], 60)) if comp.any() else 1.0
            want = 2.0 * max(1.0, r_in) * a.brush_factor
            brush = tiers[-1]
            for t in tiers:
                if want <= t:
                    brush = t
                    break
            # 内缩一点：笔是圆的，半径会往外洇，不缩就会把颜色刷到白底上
            k = max(1, min(3, int(round(brush * 0.3))))
            ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
            er = cv2.erode(comp.astype(np.uint8), ker)
            if er.any():
                comp = er.astype(bool)
            strokes = region_strokes(comp, brush, ox, oy)
            if not strokes:
                continue
            per_brush.setdefault(brush, []).extend(strokes)
            full = np.zeros((ch, cw), dtype=bool)
            full[oy:oy + nh, ox:ox + nw] = comp
            preview[full] = rgbv
        for brush, strokes in per_brush.items():
            groups.append({"color": name, "rgb": list(rgbv), "row": row, "col": col,
                           "brush": brush, "strokes": strokes})
            total_strokes += len(strokes)
            total_verts += sum(len(s) for s in strokes)

    # 大笔先画、小笔后画（细节压在上面），同笔径按颜色聚在一起减少换色
    groups.sort(key=lambda g: (-g["brush"], g["color"]))
    groups = [{"color": g["color"], "rgb": g["rgb"], "row": g["row"], "col": g["col"],
               "brush": g["brush"], "strokes": g["strokes"]} for g in groups]

    os.makedirs(os.path.dirname(os.path.abspath(base)) or ".", exist_ok=True)
    plan_path = base + "-plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"ok": True, "canvas": [cw, ch], "offset": [ox, oy], "groups": groups},
                  f, ensure_ascii=False)
    prev_path = base + "-preview.png"
    Image.fromarray(preview, mode="RGB").save(prev_path, "PNG")

    est = total_verts * 0.013 + total_strokes * 0.06
    print(json.dumps({"ok": True, "plan": plan_path, "preview": prev_path,
                      "groups": len(groups), "colors": sorted({g["color"] for g in groups}),
                      "strokes": total_strokes, "vertices": total_verts,
                      "est_seconds": round(est, 1)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
