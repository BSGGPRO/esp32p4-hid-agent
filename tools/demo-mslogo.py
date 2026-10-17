"""在 Windows 画图里用板子画微软图标（四色方块），并做颜色级自证。

两个硬问题与解法
----------------
1) 不能"看"工具栏 -> 用像素分析自动定位调色板：扫描窗口顶部区域的**高饱和像素**，
   聚成色块，取中心坐标。左键点色块即设为主色。
2) 画笔只能拖直线 -> 方块用「4 条边 + 横向排线填充」画，排线间距按**实测笔宽**定，
   保证不留白缝。

验证：把画面按颜色分类降采样成 R/G/B/Y 字符图（模型无视觉输入也能核查），
      再逐块统计各色覆盖率与几何尺寸。

用法:
    python tools/demo-mslogo.py --detect            # 只探测调色板，不画
    python tools/demo-mslogo.py --size 100 --gap 14 # 画
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pc"))

import eyes  # noqa: E402
from brain import Brain, focus_window, list_windows  # noqa: E402

from PIL import Image  # noqa: E402

EV = os.path.join(_ROOT, "docs", "evidence")

# 微软 logo 的标准四色
TARGETS = {
    "red": (242, 80, 34),
    "green": (127, 186, 0),
    "blue": (0, 164, 239),
    "yellow": (255, 185, 0),
}
# 四块在 logo 里的位置：左上红 / 右上绿 / 左下蓝 / 右下黄
LAYOUT = [("red", 0, 0), ("green", 1, 0), ("blue", 0, 1), ("yellow", 1, 1)]


# --------------------------------------------------------------------------- #
# 调色板探测
# --------------------------------------------------------------------------- #
def find_paint() -> dict | None:
    for w in list_windows():
        t = w["title"]
        if ("Paint" in t or "画图" in t) and w["rect"][2] - w["rect"][0] > 300:
            return w
    return None


def find_canvas(img: Image.Image, win: dict) -> tuple[int, int, int, int]:
    """找最大的一块白色连通区域 = 真正的画布。

    不能靠"窗口中心大致就是画布中心"猜：窗口尺寸/缩放一变就全错，会画到
    工具栏上去。这里用 16px 网格 + 4 邻域连通块，取最大的那块白色。
    """
    W, H = img.size
    x1, y1, x2, y2 = win["rect"]
    x1c, y1c = max(0, x1 + 8), max(0, y1 + 8)
    x2c, y2c = min(W, x2 - 8), min(H, y2 - 8)
    step = 16
    cols, rows = (x2c - x1c) // step, (y2c - y1c) // step
    if cols < 4 or rows < 4:
        return (x1c, y1c, x2c, y2c)

    px = img.convert("L").load()
    grid = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            sx, sy = x1c + c * step, y1c + r * step
            ok = 0
            for ddx in (2, step - 3):
                for ddy in (2, step - 3):
                    if px[min(W - 1, sx + ddx), min(H - 1, sy + ddy)] > 235:
                        ok += 1
            grid[r][c] = ok >= 3

    seen = [[False] * cols for _ in range(rows)]
    best, bestn = None, 0
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
            if len(pts) > bestn:
                bestn, best = len(pts), pts
    if not best:
        return (x1c, y1c, x2c, y2c)
    rs = [p[0] for p in best]
    cs = [p[1] for p in best]
    return (x1c + min(cs) * step, y1c + min(rs) * step,
            x1c + (max(cs) + 1) * step, y1c + (max(rs) + 1) * step)


def detect_swatches(img: Image.Image, win: dict, y_bottom: int | None = None) -> list[tuple[int, int, tuple[int, int, int]]]:
    """在 Paint 窗口上部找色块：高饱和度像素 -> 连通块 -> 中心点。"""
    x1, y1, x2, y2 = win["rect"]
    top = max(0, y1)
    bottom = min(img.size[1], y1 + int((y2 - y1) * 0.16)) if y_bottom is None else y_bottom
    left, right = max(0, x1), min(img.size[0], x2)
    if bottom - top < 8:
        return []

    pix = img.load()
    seen = set()
    blobs = []
    for y in range(top, bottom):
        for x in range(left, right):
            if (x, y) in seen:
                continue
            r, g, b = pix[x, y][:3]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn < 70 or mx < 90:      # 高饱和、不太暗
                continue
            stack = [(x, y)]
            pts = []
            seen.add((x, y))
            while stack and len(pts) < 2000:
                cx, cy = stack.pop()
                pts.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if not (left <= nx < right and top <= ny < bottom) or (nx, ny) in seen:
                        continue
                    rr, gg, bb = pix[nx, ny][:3]
                    if max(rr, gg, bb) - min(rr, gg, bb) >= 70 and max(rr, gg, bb) >= 90:
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            if not (80 <= len(pts) <= 1200):
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            if not (8 <= bw <= 34 and 8 <= bh <= 34):
                continue
            if not (0.6 <= bw / bh <= 1.7):
                continue
            mr = sum(pix[p[0], p[1]][0] for p in pts) // len(pts)
            mg = sum(pix[p[0], p[1]][1] for p in pts) // len(pts)
            mb = sum(pix[p[0], p[1]][2] for p in pts) // len(pts)
            blobs.append(((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2, (mr, mg, mb)))

    # 去重：位置太近的只留一个
    out = []
    for b in blobs:
        if all(abs(b[0] - o[0]) > 12 or abs(b[1] - o[1]) > 12 for o in out):
            out.append(b)
    return out


def nearest(swatches, rgb):
    if not swatches:
        return None
    best, bd = None, 1e9
    for s in swatches:
        d = sum((a - b) ** 2 for a, b in zip(s[2], rgb))
        if d < bd:
            best, bd = s, d
    return best


# --------------------------------------------------------------------------- #
# 画
# --------------------------------------------------------------------------- #
def hline(b: Brain, x1, x2, y):
    b.drag(x1, y, x2, y)


def square(b: Brain, x0, y0, s, st):
    """画一个实心方块：四条边 + 横向排线。"""
    hline(b, x0, x0 + s, y0)
    hline(b, x0, x0 + s, y0 + s)
    for i in range(1, 5):
        b.drag(x0, y0 + i, x0, y0 + s - i)          # 左边
        b.drag(x0 + s, y0 + i, x0 + s, y0 + s - i)  # 右边
    y = y0 + 2
    n = 0
    while y <= y0 + s - 2:
        hline(b, x0 + 1, x0 + s - 1, y)
        y += st
        n += 1
    return n + 8


def measure_brush(b: Brain, cx, cy, shot) -> int:
    """画一条短横线，测出笔迹竖向厚度，返回建议排线间距。"""
    b.drag(cx - 60, cy - 200, cx + 60, cy - 200)
    time.sleep(0.3)
    eyes.capture(shot)
    img = Image.open(shot).convert("L")
    px = img.load()
    thick = 0
    for dy in range(-12, 13):
        if px[cx, cy - 200 + dy] < 128:
            thick += 1
    b.key("ctrl+z")          # 撤掉探针线
    time.sleep(0.3)
    return max(1, thick - 1)


def classify(rgb):
    r, g, b = rgb
    best, bd = ".", 1e9
    for name, t in TARGETS.items():
        d = sum((a - c) ** 2 for a, c in zip((r, g, b), t))
        if d < bd:
            best, bd = name[0].upper(), d
    return best if bd < 12000 else "?"


def report(shot, cx, cy, s, gap):
    img = Image.open(shot)
    px = img.load()
    total = 2 * s + gap
    x0 = cx - total // 2
    y0 = cy - total // 2

    print("\n== 各块颜色覆盖率 ==")
    ok_all = True
    for name, gx, gy in LAYOUT:
        bx = x0 + gx * (s + gap) + s // 2
        by = y0 + gy * (s + gap) + s // 2
        hit = 0
        cnt = 0
        for dy in range(-s // 3, s // 3):
            for dx in range(-s // 3, s // 3):
                cnt += 1
                if classify(px[bx + dx, by + dy]) == name[0].upper():
                    hit += 1
        ratio = hit / max(cnt, 1)
        print("   %-7s 中心(%4d,%4d) 覆盖率 %.1f%%" % (name, bx, by, ratio * 100))
        ok_all = ok_all and ratio > 0.70

    print("\n== 颜色分类字符图（R红 G绿 B蓝 Y黄 / ?其它 / .空白）==")
    cols, rows = 56, 28
    for ry in range(rows):
        line = []
        for rx in range(cols):
            sx = x0 - 20 + rx * (total + 40) // cols
            sy = y0 - 20 + ry * (total + 40) // rows
            line.append(classify(px[sx, sy]))
        print("   " + "".join(line))
    return ok_all


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--gap", type=int, default=14)
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--swatches", default="", help="手动指定 颜色=屏幕x,屏幕y，逗号分隔")
    args = ap.parse_args(argv)

    os.makedirs(EV, exist_ok=True)
    print("== 1) 启动/找到画图 ==")
    if find_paint() is None:
        subprocess.Popen(["mspaint.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3.0)
    win = find_paint()
    if win is None:
        print("FAIL: 没有画图窗口")
        return 1
    print("   ", win["title"], win["rect"])
    focus_window("Paint" if "Paint" in win["title"] else "画图")
    time.sleep(0.7)
    # 窗口可能被缩得很小（画布就装不下 logo），先最大化
    with Brain(port=args.port) as _mb:
        _mb.key("win+up")
    time.sleep(1.2)
    win = find_paint() or win
    print("   最大化后:", win["rect"])
    probe = os.path.join(EV, "logo_probe.png")
    eyes.capture(probe)
    _img0 = Image.open(probe)
    cbox = find_canvas(_img0, win)
    print("   检测到画布:", cbox, "尺寸 %dx%d" % (cbox[2] - cbox[0], cbox[3] - cbox[1]))
    cx = (cbox[0] + cbox[2]) // 2
    cy = (cbox[1] + cbox[3]) // 2
    total_need = 2 * args.size + args.gap + 40
    if (cbox[2] - cbox[0]) < total_need or (cbox[3] - cbox[1]) < total_need:
        print("FAIL: 画布装不下（需要 %d）" % total_need)
        return 5
    sw = detect_swatches(_img0, win, y_bottom=max(0, cbox[1] - 6))
    print("\n== 2) 调色板探测：找到 %d 个色块 ==" % len(sw))
    for i, (sx, sy, rgb) in enumerate(sorted(sw, key=lambda v: (v[1], v[0]))):
        print("   #%02d  (%4d,%4d)  rgb%s" % (i, sx, sy, rgb))
    if args.detect:
        return 0
    if not sw:
        print("FAIL: 没找到色块，无法上色")
        return 2

    picks = {}
    for name, rgb in TARGETS.items():
        hit = nearest(sw, rgb)
        picks[name] = hit
        print("   %-7s 目标%s -> 选(%d,%d) rgb%s 距离²=%d"
              % (name, rgb, hit[0], hit[1], hit[2],
                 sum((a - b) ** 2 for a, b in zip(hit[2], rgb))))

    with Brain(port=args.port) as b:
        b.release()
        print("\n== 3) 清空画布 ==")
        for attempt in range(3):
            b.key("ctrl+n")
            time.sleep(0.9)
            eyes.capture(probe)
            m = Image.open(probe).convert("L")
            _h2 = m.crop((cx - 200, cy - 200, cx + 200, cy + 200)).histogram()
            dark = sum(_h2[:128])
            print("   ctrl+n 后画布暗像素 = %d" % dark)
            if dark < 200:
                break
            b.tap("n")
            time.sleep(0.9)
        focus_window("Paint" if "Paint" in win["title"] else "画图")
        time.sleep(0.5)

        # 守卫：画布区域必须基本是白的，否则说明几何算错，别乱画
        eyes.capture(probe)
        _im = Image.open(probe).convert("L")
        _h = _im.crop((cx - 200, cy - 200, cx + 200, cy + 200)).histogram()
        white_ratio = sum(_h[200:]) / max(sum(_h), 1)
        print("   画布白色占比 = %.1f%%" % (white_ratio * 100))
        if white_ratio < 0.60:
            print("FAIL: 画布区域不是白的，几何定位错了，停止以免乱画")
            return 4

        print("\n== 4) 实测笔宽 ==")
        st = measure_brush(b, cx, cy, probe)
        print("   排线间距 st =", st)

        total = 2 * args.size + args.gap
        x0 = cx - total // 2
        y0 = cy - total // 2
        print("\n== 5) 画四色方块 (size=%d gap=%d) ==" % (args.size, args.gap))
        nd = 0
        for name, gx, gy in LAYOUT:
            sx, sy, _rgb = picks[name]
            b.click(at=(sx, sy))          # 选颜色
            time.sleep(0.35)
            bx = x0 + gx * (args.size + args.gap)
            by = y0 + gy * (args.size + args.gap)
            nd += square(b, bx, by, args.size, st)
            print("   %-7s 画在 (%d,%d) 色块(%d,%d) 累计 %d 次拖拽"
                  % (name, bx, by, sx, sy, nd))

        b.release()
        time.sleep(0.6)
        out = os.path.join(EV, "mslogo.png")
        eyes.capture(out)

    ok = report(out, cx, cy, args.size, args.gap)
    print("\n截图:", out)
    print("VERDICT:", "PASS 四色方块成立" if ok else "FAIL 颜色/几何不达标")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
