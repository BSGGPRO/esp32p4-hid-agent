"""在 Windows 画图里用板子的 HID 鼠标画一个笑脸，并用像素分析自证。

为什么这么写
------------
* 大脑（模型）没有视觉输入，"画得像不像"不能靠看：本脚本读回截图，
  按几何模型检查墨迹（外圆覆盖度 / 两只眼睛 / 嘴部弧线），给出可复算的结论。
  另配 tools/verify-smiley.py 把墨迹降采样成 ASCII 图，文本模型也能核查形状。
* 所有位移都由板子发出：Brain.point() 用 GetCursorPos() 做反馈闭环，
  位移全部走 HID；这里只负责算几何、发动作、验结果。没有用 SetCursorPos 抄近路。
* 先用探针笔确认"默认工具能画"（画图默认工具未必是画笔），再画整幅。

用法
----
    python tools/demo-smiley.py --port COM7 --radius 150
    python tools/demo-smiley.py --port COM7 --fresh --skip-probe   # 清空画布后重画

退出码: 0 通过 / 1 找不到窗口 / 2 画不上（工具不对）/ 3 形状不成立
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PC = os.path.join(os.path.dirname(_HERE), "pc")
sys.path.insert(0, _PC)

import eyes  # noqa: E402
from brain import Brain, focus_window, list_windows  # noqa: E402

from PIL import Image, ImageChops  # noqa: E402


# --------------------------------------------------------------------------- #
# 观测
# --------------------------------------------------------------------------- #
def find_paint() -> dict | None:
    for w in list_windows():
        t = w["title"]
        if ("Paint" in t or "画图" in t) and w["rect"][2] - w["rect"][0] > 300:
            return w
    return None


def ink_mask(img: Image.Image, box, thresh: int = 128):
    """box 区域内的墨迹布尔矩阵（深色 = 墨）。"""
    crop = img.crop(box).convert("L")
    w, h = crop.size
    px = crop.load()
    return [[px[x, y] < thresh for x in range(w)] for y in range(h)], (w, h)


def count_ink(img: Image.Image, box) -> int:
    m, _ = ink_mask(img, box)
    return sum(1 for row in m for v in row if v)


def changed_pixels(p1: str, p2: str) -> int:
    d = ImageChops.difference(Image.open(p1).convert("L"), Image.open(p2).convert("L"))
    return sum(v for i, v in enumerate(d.histogram()) if i > 40 and v)


def has_ink_near(mask, size, x, y, tol=5) -> bool:
    w, h = size
    for dy in range(-tol, tol + 1):
        for dx in range(-tol, tol + 1):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and mask[ny][nx]:
                return True
    return False


def analyze(shot_path: str, cx: int, cy: int, radius: int) -> dict:
    """按几何模型核对笑脸：外圆 / 左眼 / 右眼 / 嘴弧。"""
    img = Image.open(shot_path)
    box = (cx - radius - 40, cy - radius - 40, cx + radius + 40, cy + radius + 40)
    mask, size = ink_mask(img, box)
    ox, oy = box[0], box[1]
    total = sum(1 for row in mask for v in row if v)

    # 1) 外圆：沿圆周 72 个角度，看半径 ±8 内有没有墨
    hit_circle = 0
    for i in range(72):
        a = 2 * math.pi * i / 72
        for r in range(radius - 8, radius + 9):
            x = int(cx + r * math.cos(a)) - ox
            y = int(cy + r * math.sin(a)) - oy
            if 0 <= x < size[0] and 0 <= y < size[1] and mask[y][x]:
                hit_circle += 1
                break

    # 2) 眼睛：两只眼睛中心 ±10 内要有墨
    eye_dx, eye_dy = int(radius * 0.36), int(radius * 0.32)
    eye_l = has_ink_near(mask, size, cx - eye_dx - ox, cy - eye_dy - oy, 10)
    eye_r = has_ink_near(mask, size, cx + eye_dx - ox, cy - eye_dy - oy, 10)

    # 3) 嘴：下半圆弧（30°~150°），半径 0.6R，采样 31 点
    mouth_r = int(radius * 0.6)
    hit_mouth = 0
    for i in range(31):
        t = math.radians(30 + (150 - 30) * i / 30)
        for r in range(mouth_r - 9, mouth_r + 10):
            x = int(cx + r * math.cos(t)) - ox
            y = int(cy + r * math.sin(t)) - oy
            if 0 <= x < size[0] and 0 <= y < size[1] and mask[y][x]:
                hit_mouth += 1
                break

    return {
        "ink_pixels": total,
        "circle_coverage": round(hit_circle / 72.0, 3),
        "eye_left": eye_l,
        "eye_right": eye_r,
        "mouth_coverage": round(hit_mouth / 31.0, 3),
        "box": box,
    }


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def stroke(b: Brain, pts) -> int:
    """沿一串屏幕坐标拖动，返回段数。每段都是一次真实的 HID 按下-拖动-抬起。"""
    for i in range(len(pts) - 1):
        b.drag(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
    return len(pts) - 1


def circle_pts(cx, cy, r, n=60, a0=0.0, a1=360.0):
    """圆周上 n+1 个整数屏幕坐标点。"""
    pts = []
    for i in range(n + 1):
        deg = a0 + (a1 - a0) * i / n
        rad = math.radians(deg)
        pts.append((int(round(cx + r * math.cos(rad))),
                    int(round(cy + r * math.sin(rad)))))
    return pts


def clear_canvas(b: Brain, cx: int, cy: int, radius: int, shot_tmp: str, title: str) -> int:
    """ctrl+n 清空画布；若弹保存框就按 不保存。返回剩余墨迹数。"""
    box = (cx - radius - 40, cy - radius - 40, cx + radius + 40, cy + radius + 40)
    left = -1
    for attempt in range(3):
        b.key("ctrl+n")
        time.sleep(0.9)
        eyes.capture(shot_tmp)
        left = count_ink(Image.open(shot_tmp), box)
        print("   ctrl+n 第 %d 次后画布墨迹 = %d" % (attempt + 1, left))
        if left < 100:
            return left
        b.tap("n")          # 保存对话框 -> 不保存
        time.sleep(0.9)
    focus_window(title)
    time.sleep(0.4)
    return left


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    ap.add_argument("--radius", type=int, default=150)
    ap.add_argument("--fresh", action="store_true", help="先清空画布再画")
    ap.add_argument("--skip-probe", action="store_true", help="跳过探针笔（已验证能画时用）")
    args = ap.parse_args(argv)

    out_dir = os.path.join(os.path.dirname(_HERE), "docs", "evidence")
    os.makedirs(out_dir, exist_ok=True)
    shot_before = os.path.join(out_dir, "smiley_before.png")
    shot_tmp = os.path.join(out_dir, "smiley_probe.png")
    shot_final = os.path.join(out_dir, "smiley.png")

    print("== 1) 启动画图 ==")
    subprocess.Popen(["mspaint.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3.0)
    win = find_paint()
    if win is None:
        print("FAIL: 没找到画图窗口")
        return 1
    title_key = "Paint" if "Paint" in win["title"] else "画图"
    print("   窗口:", win["title"], win["rect"])
    focus_window(title_key)
    time.sleep(0.6)
    win = find_paint() or win
    x1, y1, x2, y2 = win["rect"]
    cx = (x1 + x2) // 2
    cy = y1 + int((y2 - y1) * 0.52)       # 避开顶部工具栏和底部状态栏
    print("   画布中心估计:", (cx, cy))

    with Brain(port=args.port) as b:
        info = b.info()
        print("   板子:", {k: info.get(k) for k in ("mounted", "host_mounted", "fw_version")
                          if k in info})

        if args.fresh:
            print("== 2) 清空画布 ==")
            clear_canvas(b, cx, cy, args.radius, shot_tmp, title_key)

        eyes.capture(shot_before)

        if args.skip_probe:
            print("== 3) 跳过探针 ==")
        else:
            print("== 3) 探针：画一小笔确认默认工具能画 ==")
            px, py = cx - args.radius - 10, cy - args.radius - 10
            b.drag(px, py, px + 40, py)
            time.sleep(0.3)
            eyes.capture(shot_tmp)
            changed = changed_pixels(shot_before, shot_tmp)
            print("   探针改动像素:", changed)
            if changed < 20:
                print("FAIL: 拖动没有产生墨迹（默认工具可能不是画笔）")
                return 2

        print("== 4) 外圆（60 段）==")
        n1 = stroke(b, circle_pts(cx, cy, args.radius, 60))

        print("== 5) 两只眼睛 ==")
        er = max(6, args.radius // 12)
        n2 = 0
        for sx in (-1, 1):
            ex = cx + sx * int(args.radius * 0.36)
            ey = cy - int(args.radius * 0.32)
            n2 += stroke(b, circle_pts(ex, ey, er, 14))

        print("== 6) 嘴（下半圆弧 30°~150°）==")
        n3 = stroke(b, circle_pts(cx, cy, int(args.radius * 0.6), 30, 30, 150))
        print("   共拖动 %d 段，全部由板子发出" % (n1 + n2 + n3))

        print("== 7) 释放 + 截图 ==")
        b.release()
        time.sleep(0.5)
        eyes.capture(shot_final)

    w2 = find_paint()
    print("   窗口标题:", w2["title"] if w2 else "(没了?)")

    res = analyze(shot_final, cx, cy, args.radius)
    print("\n== 像素分析（不靠眼睛的自证）==")
    for k, v in res.items():
        print("   %-16s %s" % (k, v))

    ok = (res["ink_pixels"] > 800
          and res["circle_coverage"] >= 0.80
          and res["eye_left"] and res["eye_right"]
          and res["mouth_coverage"] >= 0.55)
    print("\n截图:", shot_final)
    print("VERDICT:", "PASS 笑脸成立" if ok else "FAIL 形状不成立")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
