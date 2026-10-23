#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""手机手势引擎的离线自检（不需要板子、不需要手机）。

跑法：``python tests/phone_pointer.py``

验证三件容易错的事：
  1. 指针识别：合成两帧（指针平移了已知距离），``pointer()`` 要还原出原来位置；
  2. 变化判定：没动 -> False，有变化 -> True；
  3. 相对移动记账：``Phone`` 发出去的 ``mouse_move`` 增量之和 == 目标位置，
     归零（home）后从 0 重新算，``_fix`` 的线性修正能把固定偏移吃掉。

输出只有 ASCII，避免 Windows 控制台编码问题。
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "pc"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from PIL import Image, ImageDraw  # noqa: E402

import phone as P  # noqa: E402


class FakeBridge:
    """只记账，不连板子。"""

    def __init__(self):
        self.moves = []
        self.buttons = []
        self.texts = []
        self.released = 0

    def mouse_move(self, dx, dy):
        self.moves.append((dx, dy))
        return {"ok": True}

    def mouse_btn(self, btn="left", action="click"):
        self.buttons.append((btn, action))
        return {"ok": True}

    def mouse_scroll(self, dy):
        return {"ok": True}

    def text(self, value, cps=40):
        self.texts.append(value)
        return [{"ok": True}]

    def key_combo(self, keys, hold_ms=30):
        return {"ok": True}

    def release_all(self):
        self.released += 1
        return {"ok": True}


def make_frame(w=400, h=800, pointer=None, bg=40):
    """灰底 + 可选的白点（模拟指针）。"""
    img = Image.new("L", (w, h), bg)
    if pointer is not None:
        d = ImageDraw.Draw(img)
        px, py = int(pointer[0] * w), int(pointer[1] * h)
        d.ellipse((px - 3, py - 3, px + 3, py + 3), fill=235)
    return img.convert("RGB")


def test_pointer_recovery():
    w, h = 400, 800
    before_at = (0.40, 0.60)
    shift_px = 16                     # pointer moved right by 16 px between frames
    shift = shift_px / w
    before = make_frame(w, h, before_at)
    after = make_frame(w, h, (before_at[0] + shift, before_at[1]))
    got = P.pointer(before, after, before_at, shift, reach=(0.2, 0.2))
    assert got is not None, "pointer() returned None on a clean synthetic pair"
    assert abs(got[0] - before_at[0]) < 0.01, (got, before_at)
    assert abs(got[1] - before_at[1]) < 0.01, (got, before_at)
    return got


def test_pointer_rejects_unchanged():
    w, h = 400, 800
    a = make_frame(w, h, (0.5, 0.5))
    b = make_frame(w, h, (0.5, 0.5))
    assert P.pointer(a, b, (0.5, 0.5), 0.02) is None, "unchanged frames must not yield a pointer"
    assert P.changed(a, b) is False, "identical frames must not read as changed"
    assert P.changed(a, make_frame(w, h, (0.5, 0.5), bg=40)) is False
    # a real content change (big region) must read as changed
    c = make_frame(w, h, (0.5, 0.5))
    ImageDraw.Draw(c).rectangle((20, 40, 380, 400), fill=200)
    assert P.changed(a, c) is True, "a large content change must read as changed"


def test_move_accounting():
    fb = FakeBridge()
    tmp = os.path.join(tempfile.mkdtemp(), "calib.json")
    ph = P.Phone(fb, screen=None, calib_file=tmp)
    ph.span = {"w": 1000, "h": 2000}
    ph._pos = (0, 0)
    ph.point(0.25, 0.5)                       # -> (250, 1000)
    dx = sum(m[0] for m in fb.moves)
    dy = sum(m[1] for m in fb.moves)
    assert (dx, dy) == (250, 1000), (dx, dy)
    # home resets the bookkeeping: a new point is measured from the corner again
    fb.moves.clear()
    ph.home()
    assert sum(m[0] for m in fb.moves) == -2 * P.HOME_UNITS, fb.moves
    assert ph._pos == (0, 0), ph._pos
    fb.moves.clear()
    ph.point(0.5, 0.25)                       # -> (500, 500) from the corner
    assert sum(m[0] for m in fb.moves) == 500, fb.moves
    assert sum(m[1] for m in fb.moves) == 500, fb.moves


def test_fix_removes_constant_offset():
    fb = FakeBridge()
    ph = P.Phone(fb, screen=None, calib_file=os.path.join(tempfile.mkdtemp(), "c.json"))
    ph.span = {"w": 1000, "h": 1000}
    # observed: aiming at 0.5 landed at 0.52 (fixed +0.02 offset), aiming at 0.8 landed at 0.82
    ph._seen["x"].append((0.5, 0.52))
    ph._seen["x"].append((0.8, 0.82))
    assert abs(ph._fix(0.5, "x") - 0.48) < 0.005, ph._fix(0.5, "x")
    # clamp keeps targets on screen
    ph._pos = (0, 0)
    ph.point(1.5, -0.5)
    dx = sum(m[0] for m in fb.moves)
    dy = sum(m[1] for m in fb.moves)
    assert dx <= 1000 and dy >= 0, (dx, dy)


def test_drag_holds_and_releases():
    fb = FakeBridge()
    ph = P.Phone(fb, screen=None, calib_file=os.path.join(tempfile.mkdtemp(), "c.json"))
    ph.span = {"w": 1000, "h": 1000}
    ph._pos = (0, 0)
    ph.drag([(0.2, 0.8), (0.5, 0.5), (0.8, 0.2)])
    actions = [a for _, a in fb.buttons]
    assert actions[0] == "down", actions
    assert actions[-1] == "up", actions
    assert "press" not in actions


def test_letterbox_trim():
    """竖屏手机塞进横窗口：两侧黑边要被裁掉，偏移要报对。"""
    canvas = Image.new("L", (400, 200), 0)          # black window
    ImageDraw.Draw(canvas).rectangle((100, 0, 299, 199), fill=120)   # 200x200 video
    img, (dx1, dy1, dx2, dy2) = P.trim_letterbox(canvas.convert("RGB"))
    assert img.size == (200, 200), img.size
    assert (dx1, dx2) == (100, 300), (dx1, dx2)
    # 全黑图不能把自己裁成 0x0
    black = Image.new("RGB", (100, 100), 0)
    img2, box2 = P.trim_letterbox(black)
    assert img2.size == (100, 100) and box2 == (0, 0, 100, 100), (img2.size, box2)


def main() -> int:
    checks = [
        ("pointer recovery", test_pointer_recovery),
        ("pointer rejects unchanged", test_pointer_rejects_unchanged),
        ("move accounting + home", test_move_accounting),
        ("fix removes constant offset", test_fix_removes_constant_offset),
        ("drag down/up", test_drag_holds_and_releases),
        ("letterbox trim", test_letterbox_trim),
    ]
    failed = 0
    for name, fn in checks:
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as e:
            failed += 1
            print("FAIL  %s: %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("ERROR %s: %r" % (name, e))
    print("%d/%d passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
