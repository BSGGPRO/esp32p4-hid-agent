"""用「Keyboard Test Utility」逐键验证板子的 HID 键盘。

判定方式：按住某键时该软件会把那个键高亮 -> 与"全松开"基线做像素差分。
差分超阈值即证明 OS/软件真的收到了这个键。不依赖任何 VK 映射，也不依赖我看图。

用法: python tools/verify-all-keys.py [--port COM7] [--title Keyboard]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pc"))

import eyes  # noqa: E402
from brain import Brain, focus_window, list_windows  # noqa: E402

from PIL import Image, ImageChops  # noqa: E402

EV = os.path.join(_ROOT, "docs", "evidence")


def key_names() -> list[str]:
    """从固件 keymap.c 里抽出协议支持的全部键名。"""
    src = os.path.join(_ROOT, "firmware", "main", "keymap.c")
    txt = open(src, encoding="utf-8", errors="replace").read()
    names = re.findall(r'"([a-z][a-z0-9]*)"', txt)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def diff_count(p1: str, p2: str, thresh: int = 30) -> int:
    d = ImageChops.difference(Image.open(p1).convert("L"), Image.open(p2).convert("L"))
    return sum(v for i, v in enumerate(d.histogram()) if i > thresh and v)


def find(key: str) -> dict | None:
    for w in list_windows():
        if key.lower() in w["title"].lower() and w["rect"][2] - w["rect"][0] > 200:
            return w
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    ap.add_argument("--title", default="Keyboard")
    ap.add_argument("--thresh", type=int, default=30)
    args = ap.parse_args(argv)

    os.makedirs(EV, exist_ok=True)
    win = find(args.title)
    if win is None:
        print("找不到窗口（关键词 %r）。当前窗口：" % args.title)
        for w in list_windows()[:12]:
            print("   ", w["title"], w["rect"])
        return 1
    print("窗口:", win["title"], win["rect"])
    focus_window(win["title"][:14])
    time.sleep(0.8)

    names = key_names()
    SKIP = {"leftgui", "rightgui", "win", "cmd", "meta", "super",
            "printscreen", "power"}
    print("跳过: %s（Win 键弹开始菜单 / PrintScreen 弹截图工具，另行单测）" % sorted(SKIP & set(names)))
    names = [n for n in names if n not in SKIP]
    print("键名 %d 个: %s" % (len(names), " ".join(names)))

    base_p = os.path.join(EV, "keys_base.png")
    hold_p = os.path.join(EV, "keys_hold.png")
    results = []

    with Brain(port=args.port) as b:
        b.release()
        time.sleep(0.4)
        eyes.capture(base_p)

        for i, name in enumerate(names, 1):
            r = b.hold([name])
            if not r.get("ok", True):
                results.append((name, False, "hold 被拒"))
                continue
            time.sleep(0.13)
            eyes.capture(hold_p)
            n = diff_count(base_p, hold_p, args.thresh)
            b.release()
            time.sleep(0.10)
            ok = n > args.thresh
            results.append((name, ok, "%d px" % n))
            print("   [%02d/%d] %-12s %s  %d px" % (i, len(names), name,
                                                   "PASS" if ok else "FAIL", n))
        b.release()

    passed = [n for n, o, _ in results if o]
    failed = [(n, d) for n, o, d in results if not o]
    print("\n== 汇总 ==")
    print("PASS %d / %d" % (len(passed), len(results)))
    if failed:
        print("FAIL:")
        for n, d in failed:
            print("   %-12s %s" % (n, d))
    print("\n基线:", base_p)
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(main())
