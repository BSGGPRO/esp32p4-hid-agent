#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/draw-rage.py — 用 ESP32-P4 的 HID 鼠标在 Windows「画图」里画一张暴走漫画脸。

设计（为什么这么写）
--------------------
* **不靠眼睛**：本模型没有图像输入能力。窗口位置用 ``GetWindowRect`` 读，画布用
  像素统计找（窗口内最大的纯白连通块），画没画上/画到哪用**截屏差分 + 墨迹几何
  采样**判定。所有结论都能被读者用同一份 PNG 复算。
* **不放大窗口**：每次运行都重新 ``GetWindowRect`` 检测画图窗口当前 rect；**绝不
  SetWindowPos 改大小、绝不最大化**。只有画图窗口与聊天窗口（DeepSeek Harness）
  重叠超过阈值时，才做一次**等尺寸最小移动**（挑重叠面积最小的角落），并写进日志。
* **只画在画图里**：每一步动笔前都复核 ``GetForegroundWindow()`` 的 hwnd 必须是
  画图窗口，否则立即停手（历史事故：盲打把字灌进了 README.md）。
* **抗打断**：用户会在画的时候动真鼠标。``PointerGuard`` 每一步都比较"我们发的
  位移"和"光标实际位移"，偏差超阈值且方向解释不了就判定用户介入：抬笔、把当前
  真实位置当新锚点、从下一个规划点续画（留个小缺口，绝不把用户乱动画成长线）。
  连续多次介入则暂停并提示。
* **可复用接口**：定位/拖拽全部走 ``pc/brain.py`` 的 ``locate()`` / ``drag_path()``，
  位移只由板子发出，绝不调用 SetCursorPos。

用法
----
    python tools/draw-rage.py --detect                 # 只检测窗口/画布，不动笔
    python tools/draw-rage.py --mock                   # 离屏渲染校验几何（无副作用）
    python tools/draw-rage.py --port COM7              # 真机画
    python tools/draw-rage.py --port COM7 --no-move --weight 2

退出码: 0 成功 / 1 找不到画图 / 2 画布不合法 / 3 前台不是画图（安全中止）
        4 没画出墨迹 / 5 被用户打断暂停 / 6 形状验证不通过
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PC = os.path.join(_ROOT, "pc")
sys.path.insert(0, _PC)
sys.path.insert(0, _HERE)

import eyes  # noqa: E402
from brain import (Brain, PointerGuard, cursor_pos, focus_window,  # noqa: E402
                   list_windows)
from PIL import Image, ImageChops, ImageDraw  # noqa: E402

try:
    from board_lock import BoardLock  # noqa: E402
except Exception:  # noqa: BLE001  (mock 模式不需要锁)
    BoardLock = None

u32 = ctypes.windll.user32
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
u32.SetForegroundWindow.restype = ctypes.c_bool
u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
u32.SetWindowPos.restype = ctypes.c_bool
u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_RESTORE = 9

EV = os.path.join(_ROOT, "docs", "evidence")
SCRATCH = os.path.join(os.path.dirname(_ROOT), "_scratch_draw")

PAINT_MARKS = ("\u753b\u56fe", "Paint", "mspaint")
CHAT_MARKS = ("DeepSeek Harness", "dsh-ops")

_WHITE = 245


# --------------------------------------------------------------------------- #
# 窗口：检测 / 聚焦 / 最小移动（不改大小！）
# --------------------------------------------------------------------------- #
def find_paint() -> dict | None:
    best = None
    for w in list_windows():
        t = w["title"]
        if any(m.lower() in t.lower() for m in PAINT_MARKS):
            if w["rect"][2] - w["rect"][0] > 200 and w["rect"][3] - w["rect"][1] > 150:
                if best is None or (w["rect"][2] - w["rect"][0]) > (best["rect"][2] - best["rect"][0]):
                    best = w
    return best


def find_chat() -> dict | None:
    for w in list_windows():
        t = w["title"]
        if any(m.lower() in t.lower() for m in CHAT_MARKS):
            if w["rect"][2] - w["rect"][0] > 400:
                return w
    return None


def find_by_hwnd(hwnd: int) -> dict | None:
    for w in list_windows():
        if int(w["hwnd"]) == int(hwnd):
            return w
    return None


def rect_size(r) -> tuple[int, int]:
    return (r[2] - r[0], r[3] - r[1])


def overlap_area(a, b) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def foreground() -> int:
    return int(u32.GetForegroundWindow() or 0)


def focus_hwnd(hwnd: int) -> bool:
    if foreground() == int(hwnd):
        return True
    if u32.IsIconic(int(hwnd)):
        u32.ShowWindow(int(hwnd), SW_RESTORE)
        time.sleep(0.3)
    ok = u32.SetForegroundWindow(int(hwnd))
    if not ok:
        u32.keybd_event(0x12, 0, 0, 0)      # 解锁前台锁的标准做法
        u32.keybd_event(0x12, 0, 2, 0)
        ok = u32.SetForegroundWindow(int(hwnd))
    time.sleep(0.35)
    if foreground() == int(hwnd):
        return True
    # 还不行（用户正在别的窗口里打字，Windows 死锁前台）：AttachThreadInput 硬切
    try:
        k32 = ctypes.windll.kernel32
        fg = int(u32.GetForegroundWindow())
        tid_fg = u32.GetWindowThreadProcessId(fg, None)
        tid_me = k32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            u32.AttachThreadInput(tid_fg, tid_me, True)
        u32.BringWindowToTop(int(hwnd))
        u32.SetForegroundWindow(int(hwnd))
        u32.SetFocus(int(hwnd))
        if tid_fg and tid_fg != tid_me:
            u32.AttachThreadInput(tid_fg, tid_me, False)
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass
    return foreground() == int(hwnd)


def set_rect(hwnd: int, x: int, y: int, w: int, h: int, activate: bool = False) -> bool:
    """把窗口挪到 (x,y) 并改成 w x h。只在「画图是我们自己打开的时候」用。"""
    flags = SWP_NOZORDER | (0 if activate else SWP_NOACTIVATE)
    return bool(u32.SetWindowPos(int(hwnd), 0, int(x), int(y), int(w), int(h), flags))


def minimal_move_off_chat(paint: dict, chat: dict | None, max_ratio: float = 0.25,
                          dry_run: bool = False) -> dict:
    """画图窗口与聊天窗口重叠太多时，做**等尺寸最小移动**（不缩放！）。

    在屏幕四个角里挑"与聊天窗口重叠面积最小"的落点；只在能显著减小重叠时才动。
    """
    info = {"moved": False, "before_rect": list(paint["rect"]),
            "overlap_before": 0, "overlap_after": 0, "chat_rect": None}
    if chat is None:
        return info
    info["chat_rect"] = list(chat["rect"])
    sw, sh = int(u32.GetSystemMetrics(0)), int(u32.GetSystemMetrics(1))
    w, h = rect_size(paint["rect"])
    chat_r = chat["rect"]
    chat_area = max(1, rect_size(chat_r)[0] * rect_size(chat_r)[1])
    cur = paint["rect"]
    ov0 = overlap_area(cur, chat_r)
    info["overlap_before"] = ov0
    info["overlap_ratio_before"] = round(ov0 / chat_area, 3)
    if ov0 / chat_area <= max_ratio:
        return info
    corners = {
        "top-left": (0, 0),
        "top-right": (max(0, sw - w), 0),
        "bottom-left": (0, max(0, sh - h)),
        "bottom-right": (max(0, sw - w), max(0, sh - h)),
    }
    best = None
    for name, (x, y) in corners.items():
        r = (x, y, x + w, y + h)
        ov = overlap_area(r, chat_r)
        if best is None or ov < best[1]:
            best = (name, ov, r)
    name, ov, new_r = best
    info.update({"candidate": name, "new_rect": list(new_r),
                 "overlap_after": ov, "overlap_ratio_after": round(ov / chat_area, 3)})
    if ov >= ov0 * 0.9:          # 挪了也没明显更好 -> 不动
        info["note"] = "no meaningfully better position; window left untouched"
        return info
    if dry_run:
        info["note"] = "dry-run: would move"
        return info
    ok = u32.SetWindowPos(int(paint["hwnd"]), None,
                          int(new_r[0]), int(new_r[1]), 0, 0,
                          SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    info["moved"] = bool(ok)
    time.sleep(0.35)
    w2 = find_by_hwnd(int(paint["hwnd"]))
    if w2:
        info["after_rect"] = list(w2["rect"])
        info["overlap_after"] = overlap_area(w2["rect"], chat_r)
        info["overlap_ratio_after"] = round(info["overlap_after"] / chat_area, 3)
    return info


# --------------------------------------------------------------------------- #
# 画布：窗口内最大纯白连通块 + 已有内容 bbox
# --------------------------------------------------------------------------- #
def detect_canvas(img: Image.Image, win_rect, step: int = 8) -> dict:
    W, H = img.size
    x1, y1, x2, y2 = win_rect
    x1, y1 = max(0, x1 + 4), max(0, y1 + 4)
    x2, y2 = min(W, x2 - 4), min(H, y2 - 4)
    if x2 - x1 < 60 or y2 - y1 < 60:
        return {"ok": False, "reason": "window too small", "window": list(win_rect)}
    rgb = img.convert("RGB")
    px = rgb.load()
    cols, rows = (x2 - x1) // step, (y2 - y1) // step
    grid = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            x, y = x1 + c * step + step // 2, y1 + r * step + step // 2
            rr, gg, bb = px[x, y]
            grid[r][c] = rr >= _WHITE and gg >= _WHITE and bb >= _WHITE
    seen = [[False] * cols for _ in range(rows)]
    best, bn = None, 0
    for r in range(rows):
        for c in range(cols):
            if not grid[r][c] or seen[r][c]:
                continue
            stack = [(r, c)]
            seen[r][c] = True
            pts = []
            while stack:
                rr, cc = stack.pop()
                pts.append((rr, cc))
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = rr + dr, cc + dc
                    if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            if len(pts) > bn:
                bn, best = len(pts), pts
    if not best or bn * step * step < 200 * 200:
        return {"ok": False, "reason": "no large white region", "window": list(win_rect)}

    rs = [p[0] for p in best]
    cs = [p[1] for p in best]
    gx0 = x1 + min(cs) * step
    gx1 = x1 + (max(cs) + 1) * step
    gy0 = y1 + min(rs) * step
    gy1 = y1 + (max(rs) + 1) * step

    # 细化为像素级：在白块里找"白占比>=85%"的最长连续行/列
    def white_at(x, y):
        rr, gg, bb = px[x, y]
        return rr >= _WHITE and gg >= _WHITE and bb >= _WHITE

    row_ok = []
    for y in range(max(0, gy0 - step), min(H, gy1 + step)):
        cnt = sum(1 for x in range(gx0, gx1, 2) if white_at(x, y))
        tot = max(1, len(range(gx0, gx1, 2)))
        if cnt / tot >= 0.85:
            row_ok.append(y)
    col_ok = []
    for x in range(max(0, gx0 - step), min(W, gx1 + step)):
        cnt = sum(1 for y in range(gy0, gy1, 2) if white_at(x, y))
        tot = max(1, len(range(gy0, gy1, 2)))
        if cnt / tot >= 0.85:
            col_ok.append(x)
    box = [min(col_ok) if col_ok else gx0, min(row_ok) if row_ok else gy0,
           max(col_ok) + 1 if col_ok else gx1, max(row_ok) + 1 if row_ok else gy1]
    # 白色纯度
    tot = cnt_white = 0
    for y in range(box[1], box[3], 3):
        for x in range(box[0], box[2], 3):
            tot += 1
            if white_at(x, y):
                cnt_white += 1
    return {"ok": True, "box": box, "size": [box[2] - box[0], box[3] - box[1]],
            "white_ratio": round(cnt_white / max(1, tot), 4),
            "window": list(win_rect)}


def ink_stats(img: Image.Image, box, thresh: int = 160) -> dict:
    """box 内墨迹像素数与 bbox。"""
    crop = img.convert("L").crop((box[0], box[1], box[2], box[3]))
    w, h = crop.size
    px = crop.load()
    n = 0
    minx = miny = 10 ** 9
    maxx = maxy = -1
    for y in range(h):
        for x in range(w):
            if px[x, y] < thresh:
                n += 1
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return {"pixels": 0, "bbox": None}
    return {"pixels": n, "bbox": [box[0] + minx, box[1] + miny,
                                  box[0] + maxx, box[1] + maxy]}


def changed_pixels(a: str, b: str, box=None, thresh: int = 40) -> int:
    ia = Image.open(a).convert("L")
    ib = Image.open(b).convert("L")
    if box:
        ia = ia.crop((box[0], box[1], box[2], box[3]))
        ib = ib.crop((box[0], box[1], box[2], box[3]))
    if ia.size != ib.size:
        return -1
    d = ImageChops.difference(ia, ib)
    return sum(v for i, v in enumerate(d.histogram()) if i > thresh and v)


# --------------------------------------------------------------------------- #
# 暴走漫画脸（Y U NO 风格）—— 归一化设计，等比缩放到画布
# --------------------------------------------------------------------------- #
def ellipse(cx, cy, rx, ry, n=48, a0=0.0, a1=360.0):
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def design_rage_face() -> list[dict]:
    """返回笔画列表：{name, weight, closed, pts(归一化 0..1)}。

    造型：圆头 + 两只怒眼(带瞳孔) + 倒竖的粗眉 + 鼻子 + 大张的嘴(带牙齿线)。
    Y U NO 式的"愤怒质问"表情，全部由折线/弧线构成，没有任何字体依赖。
    """
    strokes = []

    def add(name, weight, pts, closed=False):
        strokes.append({"name": name, "weight": weight,
                        "pts": [(float(x), float(y)) for x, y in pts],
                        "closed": closed})

    # 1) 头：椭圆
    add("head", 3, ellipse(0.50, 0.50, 0.415, 0.435, 56), closed=True)
    # 2) 怒眉：内侧下压（左眉从外上到内下）
    add("brow_L", 3, [(0.245, 0.265), (0.285, 0.305), (0.335, 0.345),
                      (0.395, 0.375)], closed=False)
    add("brow_R", 3, [(0.755, 0.265), (0.715, 0.305), (0.665, 0.345),
                      (0.605, 0.375)], closed=False)
    # 3) 眼睛：椭圆 + 瞳孔
    add("eye_L", 3, ellipse(0.345, 0.455, 0.092, 0.108, 28), closed=True)
    add("eye_R", 3, ellipse(0.655, 0.455, 0.092, 0.108, 28), closed=True)
    add("pupil_L", 2, ellipse(0.345, 0.465, 0.040, 0.046, 14), closed=True)
    add("pupil_R", 2, ellipse(0.655, 0.465, 0.040, 0.046, 14), closed=True)
    # 4) 鼻子：一竖一钩
    add("nose", 2, [(0.500, 0.500), (0.492, 0.545), (0.478, 0.575),
                    (0.500, 0.592), (0.522, 0.585)], closed=False)
    # 5) 嘴：上唇直、下唇弧（张大的"Y U NO"嘴）
    mouth = [(0.300, 0.705)]
    for i in range(1, 17):
        mouth.append((0.300 + 0.400 * i / 16, 0.705))
    for i in range(1, 13):                       # 右侧往下
        mouth.append((0.700, 0.705 + 0.075 * i / 12))
    for i in range(1, 25):                       # 下唇弧线（中间更往下）
        t = i / 24
        x = 0.700 - 0.400 * t
        y = 0.780 + 0.030 * math.sin(math.pi * t)
        mouth.append((x, y))
    for i in range(1, 13):                       # 左侧往上
        mouth.append((0.300, 0.780 - 0.075 * i / 12))
    add("mouth", 3, mouth, closed=True)
    # 6) 牙齿：嘴里的三条竖线
    for i, x in enumerate((0.395, 0.500, 0.605)):
        add("tooth_%d" % (i + 1), 1,
            [(x, 0.712), (x, 0.748), (x, 0.772)], closed=False)
    return strokes


def map_strokes(strokes, box, margin_ratio: float = 0.05) -> list[dict]:
    """归一化 -> 屏幕坐标：在画布内等比缩放、留边距、居中。"""
    w = box[2] - box[0]
    h = box[3] - box[1]
    side = min(w, h) * (1 - 2 * margin_ratio)
    ox = box[0] + (w - side) / 2.0
    oy = box[1] + (h - side) / 2.0
    out = []
    for s in strokes:
        pts = [(int(round(ox + x * side)), int(round(oy + y * side)))
               for x, y in s["pts"]]
        out.append({**s, "screen": pts})
    return out


def stroke_offsets(stroke: dict, thickness: float, weight: int) -> list[tuple[float, float]]:
    """按目标线宽算平行偏移（多重拖拽把线加粗）。"""
    target = {1: 3.0, 2: 5.0, 3: 7.0}.get(weight, 5.0)
    passes = max(1, min(5, int(round(target / max(1.0, thickness)))))
    if passes == 1:
        return [(0.0, 0.0)]
    pts = stroke["screen"]
    dx = pts[-1][0] - pts[0][0]
    dy = pts[-1][1] - pts[0][1]
    vertical = abs(dx) >= abs(dy)
    step = thickness
    offs = []
    for i in range(passes):
        d = (i - (passes - 1) / 2.0) * step
        offs.append((0.0, d) if vertical else (d, 0.0))
    return offs


# --------------------------------------------------------------------------- #
# 绘制
# --------------------------------------------------------------------------- #
def draw_strokes(b: Brain, paint: dict, strokes: list[dict], canvas_box,
                 evidence_dir: str, scratch: str, guard: PointerGuard,
                 thickness: float = 3.0, dry_run: bool = False) -> dict:
    res = {"ok": True, "strokes": 0, "segments": 0, "interruptions": 0,
           "aborted": False, "events": [], "per_stroke": []}
    title = paint["title"]
    prev_shot = os.path.join(scratch, "step_prev.png")
    eyes.capture(prev_shot)
    for idx, s in enumerate(strokes, 1):
        cur = find_by_hwnd(int(paint["hwnd"]))
        if cur is None:
            res.update({"ok": False, "reason": "paint window disappeared"})
            return res
        if list(cur["rect"]) != list(paint["rect"]):
            res.update({"ok": False, "aborted": True,
                        "reason": "Paint window was moved/resized by user during drawing",
                        "rect_now": list(cur["rect"]),
                        "rect_expected": list(paint["rect"])})
            return res
        if foreground() != int(paint["hwnd"]):
            if not focus_hwnd(int(paint["hwnd"])):
                res.update({"ok": False, "aborted": True,
                            "reason": "foreground is not Paint (safety stop)",
                            "foreground_title": _fg_title()})
                return res
            time.sleep(0.2)
        offsets = stroke_offsets(s, thickness, s["weight"])
        added_total = 0
        detail = {"name": s["name"], "weight": s["weight"], "passes": len(offsets),
                  "points": len(s["screen"])}
        for ox, oy in offsets:
            pts = [(x + ox, y + oy) for x, y in s["screen"]]
            pts = [(int(round(x)), int(round(y))) for x, y in pts]
            if dry_run:
                continue
            r = b.drag_path(pts, guard=guard, tol=1, settle_ms=8)
            res["segments"] += 1
            res["interruptions"] += r.get("interruptions", 0)
            if r.get("events"):
                res["events"].extend(r["events"])
            if not r.get("ok"):
                detail["error"] = r.get("reason")
                if r.get("aborted"):
                    res.update({"ok": False, "aborted": True,
                                "reason": r.get("reason"),
                                "guard": guard.summary()})
                    return res
        if not dry_run:
            shot = os.path.join(scratch, "step_%02d.png" % idx)
            eyes.capture(shot)
            added = changed_pixels(prev_shot, shot, box=canvas_box)
            detail["added_pixels"] = added
            added_total += max(0, added)
            try:
                os.replace(shot, prev_shot)
            except OSError:
                pass
            if added_total == 0 and idx <= 2:
                detail["warning"] = "no ink detected"
        res["per_stroke"].append(detail)
        res["strokes"] += 1
        if guard.should_pause():
            res.update({"ok": False, "aborted": True,
                        "reason": "user is fighting for the pointer; paused",
                        "guard": guard.summary()})
            return res
    res["ok"] = True
    return res


def _fg_title() -> str:
    fg = foreground()
    for w in list_windows():
        if int(w["hwnd"]) == fg:
            return w["title"]
    return "(hwnd %d)" % fg


# --------------------------------------------------------------------------- #
# 验证（几何采样，不靠眼睛）
# --------------------------------------------------------------------------- #
def verify_face(img: Image.Image, canvas_box, thickness: float = 3.0) -> dict:
    """沿设计好的几何位置采样墨迹，给出每个特征的覆盖率。"""
    w = canvas_box[2] - canvas_box[0]
    h = canvas_box[3] - canvas_box[1]
    side = min(w, h) * 0.90
    ox = canvas_box[0] + (w - side) / 2.0
    oy = canvas_box[1] + (h - side) / 2.0

    crop = img.convert("L").crop((canvas_box[0], canvas_box[1],
                                  canvas_box[2], canvas_box[3]))
    cw, ch = crop.size
    px = crop.load()
    total_ink = sum(1 for y in range(ch) for x in range(cw) if px[x, y] < 160)
    tol = max(6, int(thickness * 2.5))

    def ink_near(nx, ny, rad=tol):
        x = int(round((ox - canvas_box[0]) + nx * side))
        y = int(round((oy - canvas_box[1]) + ny * side))
        for dy in range(-rad, rad + 1, 2):
            for dx in range(-rad, rad + 1, 2):
                xx, yy = x + dx, y + dy
                if 0 <= xx < cw and 0 <= yy < ch and px[xx, yy] < 160:
                    return True
        return False

    def coverage(points):
        hit = sum(1 for nx, ny in points if ink_near(nx, ny))
        return round(hit / max(1, len(points)), 3)

    head_pts = [(0.50 + 0.415 * math.cos(2 * math.pi * i / 72),
                 0.50 + 0.435 * math.sin(2 * math.pi * i / 72)) for i in range(72)]
    eye_pts = []
    for cx in (0.345, 0.655):
        for i in range(20):
            a = 2 * math.pi * i / 20
            eye_pts.append((cx + 0.092 * math.cos(a), 0.455 + 0.108 * math.sin(a)))
    mouth_pts = [(0.3 + 0.4 * i / 16, 0.705) for i in range(17)]
    mouth_pts += [(0.3 + 0.4 * i / 16, 0.780 + 0.030 * math.sin(math.pi * i / 16))
                  for i in range(17)]
    brow_pts = [(0.245, 0.265), (0.285, 0.305), (0.335, 0.345), (0.395, 0.375),
                (0.755, 0.265), (0.715, 0.305), (0.665, 0.345), (0.605, 0.375)]
    nose_pts = [(0.500, 0.500), (0.492, 0.545), (0.478, 0.575), (0.500, 0.592)]

    return {
        "ink_pixels": total_ink,
        "head_coverage": coverage(head_pts),
        "eyes_coverage": coverage(eye_pts),
        "brows_coverage": coverage(brow_pts),
        "nose_coverage": coverage(nose_pts),
        "mouth_coverage": coverage(mouth_pts),
        "geometry": {"side": round(side, 1), "origin": [round(ox, 1), round(oy, 1)]},
    }


def ink_ascii(img: Image.Image, canvas_box, cols: int = 48) -> str:
    """把画布降采样成墨迹字符图：每个格子取**最小值**（细线也不会被平均掉）。"""
    crop = img.convert("L").crop((canvas_box[0], canvas_box[1],
                                  canvas_box[2], canvas_box[3]))
    w, h = crop.size
    rows = max(1, int(cols * h / w / 2.1))
    px = crop.load()
    cw, ch = w / cols, h / rows
    lines = []
    for r in range(rows):
        y0, y1 = int(r * ch), max(int(r * ch) + 1, int((r + 1) * ch))
        line = []
        for c in range(cols):
            x0, x1 = int(c * cw), max(int(c * cw) + 1, int((c + 1) * cw))
            dark = False
            for y in range(y0, min(y1, h)):
                for x in range(x0, min(x1, w)):
                    if px[x, y] < 170:
                        dark = True
                        break
                if dark:
                    break
            line.append("#" if dark else ".")
        lines.append("".join(line))
    return "\n".join(lines)


def verdict(ver: dict) -> tuple[bool, list[str]]:
    checks = []
    ok = True

    def chk(name, cond, detail):
        nonlocal ok
        checks.append("%s %-18s %s" % ("PASS" if cond else "FAIL", name, detail))
        ok = ok and cond

    chk("ink", ver["ink_pixels"] > 1500, "墨迹像素=%d (>1500)" % ver["ink_pixels"])
    chk("head", ver["head_coverage"] >= 0.80, "头轮廓覆盖=%.2f (>=0.80)" % ver["head_coverage"])
    chk("eyes", ver["eyes_coverage"] >= 0.65, "双眼覆盖=%.2f (>=0.65)" % ver["eyes_coverage"])
    chk("brows", ver["brows_coverage"] >= 0.60, "眉毛覆盖=%.2f (>=0.60)" % ver["brows_coverage"])
    chk("nose", ver["nose_coverage"] >= 0.50, "鼻子覆盖=%.2f (>=0.50)" % ver["nose_coverage"])
    chk("mouth", ver["mouth_coverage"] >= 0.70, "嘴覆盖=%.2f (>=0.70)" % ver["mouth_coverage"])
    return ok, checks


# --------------------------------------------------------------------------- #
# mock：离屏渲染同一套几何（验证工具本身，不动屏幕/不占串口）
# --------------------------------------------------------------------------- #
def mock_render(strokes: list[dict], side: int = 528, thickness: int = 3,
                outdir: str = "") -> dict:
    img = Image.new("RGB", (side, side), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    box = [0, 0, side, side]
    mapped = map_strokes(strokes, box, margin_ratio=0.05)
    for s in mapped:
        pts = s["screen"] + ([s["screen"][0]] if s["closed"] else [])
        dr.line(pts, fill=(0, 0, 0), width=max(1, thickness), joint="curve")
    os.makedirs(outdir or SCRATCH, exist_ok=True)
    path = os.path.join(outdir or SCRATCH, "rage_mock.png")
    img.save(path)
    ver = verify_face(img, box, thickness)
    return {"path": path, "verify": ver, "ascii": ink_ascii(img, box)}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用 HID 鼠标在画图里画暴走漫画脸")
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    ap.add_argument("--transport", default=os.environ.get("HID_TRANSPORT", "uart"),
                    choices=["uart", "tcp"],
                    help="uart=直连 COM7（需板子锁）；tcp=连本地 hub 127.0.0.1:8766")
    ap.add_argument("--host", default=os.environ.get("HID_HUB_HOST", "127.0.0.1"))
    ap.add_argument("--tcp-port", type=int,
                    default=int(os.environ.get("HID_HUB_PORT", "8766")))
    ap.add_argument("--detect", action="store_true", help="只检测窗口/画布，不动笔")
    ap.add_argument("--mock", action="store_true", help="离屏渲染校验几何（无副作用）")
    ap.add_argument("--plan", action="store_true", help="打印笔画坐标，不动笔")
    ap.add_argument("--no-move", action="store_true", help="即使与聊天窗口重叠也不移动画图窗口")
    ap.add_argument("--overlap-max", type=float, default=0.25,
                    help="画图/聊天窗口重叠面积占聊天窗口比例超过它就做等尺寸最小移动")
    ap.add_argument("--weight", type=int, default=0, help="固定线宽挡位 1/2/3（0=按实测笔宽自动）")
    ap.add_argument("--no-lock", action="store_true", help="不取板子锁（mock 用）")
    ap.add_argument("--settle", type=float, default=0.5, help="画完等待截图的时间")
    args = ap.parse_args(argv)

    os.makedirs(EV, exist_ok=True)
    os.makedirs(SCRATCH, exist_ok=True)
    strokes = design_rage_face()

    if args.mock:
        r = mock_render(strokes, 528, 3, SCRATCH)
        ok, checks = verdict(r["verify"])
        print("== mock 离屏渲染（不碰屏幕/串口） ==")
        print(r["ascii"])
        print(json.dumps(r["verify"], ensure_ascii=False, indent=2))
        for c in checks:
            print("  " + c)
        print("mock 图:", r["path"])
        print("VERDICT:", "PASS mock 几何成立" if ok else "FAIL mock 几何不成立")
        return 0 if ok else 6

    # ---- 1) 找画图窗口 ----
    paint = find_paint()
    if paint is None:
        print("[1] 没找到画图窗口；尝试启动 mspaint.exe ...")
        subprocess.Popen(["mspaint.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.5)
            paint = find_paint()
            if paint:
                break
    if paint is None:
        print("FAIL: 没有画图窗口，拒绝盲画")
        return 1
    if paint["minimized"]:
        u32.ShowWindow(int(paint["hwnd"]), SW_RESTORE)
        time.sleep(0.4)
        paint = find_by_hwnd(int(paint["hwnd"])) or paint
    chat = find_chat()
    print("[1] 画图窗口: %r rect=%s  %dx%d" % (
        paint["title"], paint["rect"], *rect_size(paint["rect"])))
    window_check = {"paint_title": paint["title"], "paint_rect": list(paint["rect"]),
                    "chat_title": None, "chat_rect": None,
                    "overlap_px2": None, "overlap_ratio_of_chat": None,
                    "max_allowed_ratio": args.overlap_max, "moved": False}
    if chat:
        ov = overlap_area(paint["rect"], chat["rect"])
        chat_area = max(1, rect_size(chat["rect"])[0] * rect_size(chat["rect"])[1])
        window_check.update({"chat_title": chat["title"],
                             "chat_rect": list(chat["rect"]),
                             "overlap_px2": ov,
                             "overlap_ratio_of_chat": round(ov / chat_area, 4)})
        print("    聊天窗口: %r rect=%s 重叠=%d px² (%.1f%% of chat)" % (
            chat["title"], chat["rect"], ov, 100.0 * ov / chat_area))
    else:
        print("    聊天窗口: 未找到（不检查遮挡）")

    if args.plan:
        mapped = map_strokes(strokes, [paint["rect"][0] + 60, paint["rect"][1] + 120,
                                       paint["rect"][2] - 60, paint["rect"][3] - 40])
        for s in mapped:
            print("  %-10s weight=%d n=%d  %s..." % (s["name"], s["weight"],
                                                     len(s["screen"]), s["screen"][:3]))
        return 0

    # ---- 2) 聚焦 + 最小移动（绝不改大小） ----
    if not focus_hwnd(int(paint["hwnd"])):
        print("FAIL: 无法把画图窗口切到前台（前台=%r），拒绝盲画" % _fg_title())
        return 3
    if chat and not args.no_move:
        mv = minimal_move_off_chat(paint, chat, args.overlap_max)
        print("[2] 遮挡处理:", json.dumps(mv, ensure_ascii=False))
        window_check["move_decision"] = mv
        window_check["moved"] = bool(mv.get("moved"))
        if mv.get("after_rect"):
            paint = find_by_hwnd(int(paint["hwnd"])) or paint
        if not focus_hwnd(int(paint["hwnd"])):
            print("FAIL: 移动后无法聚焦画图窗口，拒绝盲画")
            return 3
    else:
        print("[2] 未移动窗口（--no-move 或没有聊天窗口）")
    window_check["final_rect"] = list(paint["rect"])
    print("    实际 rect:", paint["rect"])

    # ---- 3) 截屏 + 找画布 ----
    probe = os.path.join(SCRATCH, "rage_probe.png")
    eyes.capture(probe)
    img = Image.open(probe).convert("RGB")
    cv = detect_canvas(img, paint["rect"])
    if not cv.get("ok"):
        print("FAIL: 画布检测失败:", cv)
        return 2
    box = cv["box"]
    print("[3] 画布: box=%s size=%s 白色占比=%.3f" % (
        box, cv["size"], cv["white_ratio"]))
    if cv["white_ratio"] < 0.90:
        print("FAIL: 画布不是白的（可能被别的窗口盖住/几何算错），拒绝乱画")
        return 2
    before_ink = ink_stats(img, box)
    print("    画前已有墨迹: pixels=%d bbox=%s" % (before_ink["pixels"], before_ink["bbox"]))
    if args.detect:
        print("[detect] 只检测，不动笔。")
        return 0

    # ---- 4) 真机绘制 ----
    lock = None
    # hub 独占串口时走 TCP 透传，不需要（也不该）抢板子锁
    if not args.no_lock and BoardLock is not None and args.transport == "uart":
        lock = BoardLock("draw:rage-face", timeout=90.0)
        lock.__enter__()
        print("[4] 已取得板子锁 (draw:rage-face)")
    elif args.transport == "tcp":
        print("[4] TCP 传输：hub 独占串口，无需板子锁")
    b = None
    try:
        b = Brain(port=args.port, transport=args.transport, host=args.host,
                  tcp_port=args.tcp_port)
        info = b.info()
        print("    板子: mounted=%s host_mounted=%s fw=%s" % (
            info.get("status", {}).get("mounted"),
            info.get("status", {}).get("host_mounted"),
            info.get("status", {}).get("fw_version")))
        b.release()
        # 画前把光标挪到画布里，确认闭环可用
        warm = b.locate(box[0] + 20, box[1] + 20, tol=1)
        print("    闭环暖机: ok=%s iters=%s err=%s" % (
            warm["ok"], warm.get("iterations"), warm.get("error")))

        mapped = map_strokes(strokes, box, margin_ratio=0.05)
        guard = PointerGuard(tol_px=15, rel_tol=0.30, max_consecutive=3)

        # 探针：先画一笔测笔宽（画在头轮廓会经过的位置，随即 ctrl+z 撤掉）
        eyes.capture(os.path.join(SCRATCH, "probe_before.png"))
        probe_pts = [(box[0] + int(cv["size"][0] * 0.15), box[1] + 12),
                     (box[0] + int(cv["size"][0] * 0.35), box[1] + 12)]
        pr = b.drag_path(probe_pts, guard=guard, tol=1, settle_ms=8)
        time.sleep(0.25)
        eyes.capture(os.path.join(SCRATCH, "probe_after.png"))
        add_probe = changed_pixels(os.path.join(SCRATCH, "probe_before.png"),
                                   os.path.join(SCRATCH, "probe_after.png"), box=box)
        print("[5] 探针笔画: ok=%s 新增像素=%d（<5 说明默认工具不是画笔）" % (
            pr.get("ok"), add_probe))
        if add_probe < 5:
            print("FAIL: 拖拽没有留下墨迹，默认工具可能不是画笔，停止")
            return 4
        # 测笔宽：在探针线中点上下扫描暗像素
        pimg = Image.open(os.path.join(SCRATCH, "probe_after.png")).convert("L")
        ppx = pimg.load()
        mx = (probe_pts[0][0] + probe_pts[1][0]) // 2
        my = probe_pts[0][1]
        thick = 0
        for dy in range(-10, 11):
            if 0 <= my + dy < pimg.size[1] and ppx[mx, my + dy] < 160:
                thick += 1
        thick = max(1, thick)
        print("    实测笔宽 = %d px" % thick)
        b.key("ctrl+z")
        time.sleep(0.5)
        eyes.capture(os.path.join(SCRATCH, "probe_undo.png"))
        left = ink_stats(Image.open(os.path.join(SCRATCH, "probe_undo.png")).convert("RGB"), box)["pixels"]
        print("    ctrl+z 后画布墨迹 = %d（基线 %d）" % (left, before_ink["pixels"]))
        if left > before_ink["pixels"] + 50:
            print("    !! 探针没撤干净，继续画（残留在边角，不影响主体）")

        if args.weight:
            thick_eff = {1: 2.0, 2: 3.0, 3: 4.0}[args.weight]
        else:
            thick_eff = float(thick)

        # 正式画：每笔画完截屏差分自证
        print("[6] 开始作画（%d 笔，笔画内抗打断阈值 15px）" % len(mapped))
        t0 = time.time()
        dres = draw_strokes(b, paint, mapped, box, EV, SCRATCH, guard,
                            thickness=thick_eff)
        dt = time.time() - t0
        print("    用时 %.1fs  笔画=%d  拖拽=%d  用户介入=%d" % (
            dt, dres["strokes"], dres["segments"], dres["interruptions"]))
        for ps in dres["per_stroke"]:
            print("      %-10s passes=%d pts=%3d added=%s" % (
                ps["name"], ps["passes"], ps["points"], ps.get("added_pixels")))
        if dres.get("aborted"):
            print("    !! 中止：%s" % dres.get("reason"))
        b.release()
    finally:
        if b is not None:
            b.close()
        if lock is not None:
            lock.__exit__(None, None, None)
            print("    已释放板子锁")

    time.sleep(args.settle)
    after = os.path.join(EV, "rage-after.png")
    before_path = os.path.join(EV, "rage-before.png")
    try:
        os.replace(probe, before_path)
    except OSError:
        pass
    eyes.capture(after)
    aimg = Image.open(after).convert("RGB")

    # ---- 5) 证据与自证 ----
    before_img = Image.open(before_path).convert("RGB")
    added = changed_pixels(before_path, after, box=box)
    ink_after = ink_stats(aimg, box)
    ver = verify_face(aimg, box, thick_eff)
    ok_face, checks = verdict(ver)
    ascii_map = ink_ascii(aimg, box, 48)
    with open(os.path.join(EV, "rage-ascii.txt"), "w", encoding="utf-8") as f:
        f.write("canvas box=%s\n\n" % box + ascii_map + "\n")

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"title": paint["title"], "rect": paint["rect"], "hwnd": int(paint["hwnd"])},
        "chat_rect": chat["rect"] if chat else None,
        "window_check": window_check,
        "canvas": cv,
        "before_ink": before_ink,
        "after_ink": ink_after,
        "added_pixels": added,
        "draw": {k: v for k, v in dres.items() if k != "events"},
        "interrupt_events": dres.get("events", [])[-10:],
        "verify": ver,
        "verdict_checks": checks,
        "evidence": {"before": before_path, "after": after, "ascii": os.path.join(EV, "rage-ascii.txt")},
    }
    with open(os.path.join(EV, "rage-report.txt"), "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    print("\n== 像素级自证 ==")
    print("  画前墨迹: %d px  画后墨迹: %d px" % (before_ink["pixels"], ink_after["pixels"]))
    print("  新增像素(差分): %s" % added)
    print("  墨迹 bbox: %s" % ink_after["bbox"])
    print("  覆盖: 头=%s 眼=%s 眉=%s 鼻=%s 嘴=%s" % (
        ver["head_coverage"], ver["eyes_coverage"], ver["brows_coverage"],
        ver["nose_coverage"], ver["mouth_coverage"]))
    print("\n== 墨迹 ASCII（#=墨 / .=白）==")
    print(ascii_map)
    for c in checks:
        print("  " + c)
    print("\n证据: %s | %s | %s" % (before_path, after, os.path.join(EV, "rage-ascii.txt")))
    print("VERDICT:", "PASS 暴走漫画脸成立" if ok_face else "FAIL 形状不达标")

    if dres.get("aborted"):
        return 5
    return 0 if ok_face else 6


if __name__ == "__main__":
    sys.exit(main())
