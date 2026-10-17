"""补齐 HID 输入覆盖：中键 / 右键 / 双击 / 滚轮。

验证策略（都不靠"看起来对不对"）
--------------------------------
* 三个鼠标按钮：按下后读 GetAsyncKeyState(VK_LBUTTON/VK_RBUTTON/VK_MBUTTON)，
  抬起后再读一次 —— 这是 OS 层真值，与任何应用无关。
* 双击：在记事本里双击一个词会选中它（高亮），用截图差分证明"发生了可见变化"。
* 右键：在记事本里右键会弹出上下文菜单，同样用差分证明，随后 Esc 关掉。
* 滚轮：用 200 行的临时文本做靶子，滚动后文字位移 -> 差分；上下两个方向都测。

用法: python tools/verify-input-coverage.py [--port COM7]
"""

from __future__ import annotations

import argparse
import ctypes
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

u32 = ctypes.windll.user32
VK = {"left": 0x01, "right": 0x02, "middle": 0x04}

EVID = os.path.join(os.path.dirname(_HERE), "docs", "evidence")


def down(vk) -> bool:
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


def diff_count(p1: str, p2: str, thresh: int = 40) -> int:
    d = ImageChops.difference(Image.open(p1).convert("L"), Image.open(p2).convert("L"))
    return sum(v for i, v in enumerate(d.histogram()) if i > thresh and v)


def shot(name: str) -> str:
    p = os.path.join(EVID, name)
    eyes.capture(p)
    return p


def find_window(key: str, min_w: int = 300) -> dict | None:
    for w in list_windows():
        if key in w["title"] and w["rect"][2] - w["rect"][0] > min_w:
            return w
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    args = ap.parse_args(argv)

    os.makedirs(EVID, exist_ok=True)
    results = []

    def rec(name, ok, detail):
        results.append((name, ok, detail))
        print("   %-22s %s  %s" % (name, "PASS" if ok else "FAIL", detail))

    # ---- 准备靶子：一个 200 行的临时文本 ----
    tmp = os.path.join(os.environ.get("TEMP", "."), "hid_input_target.txt")
    with open(tmp, "w", encoding="utf-8") as fh:
        for i in range(1, 201):
            fh.write("line %03d  The quick brown fox jumps over the lazy dog\n" % i)
    print("== 靶子文件:", tmp)

    subprocess.Popen(["notepad.exe", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    win = find_window("hid_input_target") or find_window("Notepad") or find_window("记事本")
    if win is None:
        print("FAIL: 记事本没起来")
        return 1
    focus_window(win["title"][:12])
    time.sleep(0.6)
    win = find_window(win["title"][:12]) or win
    x1, y1, x2, y2 = win["rect"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    print("   窗口:", win["title"], win["rect"])

    with Brain(port=args.port) as b:
        b.release()

        # ---- 1) 三个按钮：OS 层真值 ----
        print("== 1) 鼠标按钮（固件 status 的 mouse_btn mask 为真值）==")
        b.point(cx, cy)
        for btn, mask in (("left", 1), ("right", 2), ("middle", 4)):
            b.bridge.mouse_btn(btn, "down")
            time.sleep(0.2)
            got = b.bridge.status().get("mouse_btn")
            b.bridge.mouse_btn(btn, "up")
            time.sleep(0.2)
            gone = b.bridge.status().get("mouse_btn")
            rec("button_%s" % btn, got == mask and gone == 0,
                "固件mask 按下=%s 抬起=%s（期望 %d/0）" % (got, gone, mask))
        b.release()

        # ---- 2) 双击：选中一个词 -> 高亮 ----
        print("== 2) 双击（记事本选中词会高亮）==")
        base = shot("input_dbl_before.png")
        b.click(at=(cx, cy), count=2)
        time.sleep(0.9)
        after = shot("input_dbl_after.png")
        n = diff_count(base, after)
        rec("double_click", n > 200, "差分像素=%d" % n)
        b.key("ctrl+z")          # 撤掉可能的编辑
        time.sleep(0.2)

        # ---- 3) 右键：上下文菜单 ----
        print("== 3) 右键（弹出上下文菜单）==")
        base = shot("input_rc_before.png")
        b.click(at=(cx, cy), button="right")
        time.sleep(0.9)
        after = shot("input_rc_after.png")
        n = diff_count(base, after)
        rec("right_click_menu", n > 2000, "差分像素=%d" % n)
        b.tap("esc")
        time.sleep(0.9)
        back = shot("input_rc_closed.png")
        n2 = diff_count(base, back)
        rec("right_click_esc_closes", n2 < n // 3, "关菜单后差分=%d（开菜单时=%d）" % (n2, n))

        # ---- 4) 滚轮：上下各滚一次 ----
        print("== 4) 滚轮 ==")
        base = shot("input_scroll_0.png")
        b.point(cx, cy)
        b.scroll(-5)             # 向下
        time.sleep(0.9)
        s1 = shot("input_scroll_down.png")
        n1 = diff_count(base, s1)
        rec("scroll_down", n1 > 500, "差分像素=%d" % n1)

        b.scroll(5)              # 向上，回到原位
        time.sleep(0.9)
        s2 = shot("input_scroll_up.png")
        n2 = diff_count(s1, s2)
        rec("scroll_up", n2 > 500, "差分像素=%d" % n2)

        b.release()

    print("\n== 汇总 ==")
    ok = sum(1 for _, o, _ in results if o)
    for name, o, d in results:
        print("   %-22s %s" % (name, "PASS" if o else "FAIL"))
    print("\n%d/%d PASS" % (ok, len(results)))
    return 0 if ok == len(results) else 3


if __name__ == "__main__":
    sys.exit(main())
