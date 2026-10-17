"""实测 Windows 指针传递函数：发 N 像素，光标实际走多少。

为什么要测
----------
HID 鼠标只能发相对位移，而 Windows 的「提高指针精确度」（指针加速）会按速度
非线性放大：发 40 可能走 107，发 120 可能走 233。不知道这条曲线就没法精确落点。

本脚本在 1~200 像素区间逐档测量，输出增益表，供 brain.point() 的控制器使用。
测量用 GetCursorPos() 读真值，位移全部由板子发出。

用法: python tools/calibrate-pointer.py [--port COM7]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "pc"))

from brain import Brain, cursor_pos, screen_size  # noqa: E402

MAGS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 30, 40, 60, 80, 120, 160, 200]
REPEAT = 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    args = ap.parse_args(argv)

    sw, sh = screen_size()
    print("屏幕:", sw, sh)

    with Brain(port=args.port) as b:
        b.release()
        # 回到屏幕中央附近，保证两个方向都有余量
        b.point(sw // 2, sh // 2)
        time.sleep(0.3)

        print("\n请求  实测(均值)  增益   样本")
        print("-" * 44)
        rows = []
        for m in MAGS:
            samples = []
            for _ in range(REPEAT):
                x0, y0 = cursor_pos()
                b.bridge.mouse_move(m, 0)
                time.sleep(0.06)
                x1, _y1 = cursor_pos()
                samples.append(x1 - x0)
                # 往回走，避免撞到屏幕右缘累积偏移
                b.bridge.mouse_move(-(x1 - x0), 0)
                time.sleep(0.06)
            avg = sum(samples) / len(samples)
            gain = avg / m
            rows.append((m, avg, gain))
            print("%4d  %10.1f  %5.2f  %s" % (m, avg, gain, samples))

        # 竖向也抽两档确认增益一致
        print("\n竖向抽查:")
        for m in (5, 40, 120):
            samples = []
            for _ in range(REPEAT):
                _x0, y0 = cursor_pos()
                b.bridge.mouse_move(0, m)
                time.sleep(0.06)
                _x1, y1 = cursor_pos()
                samples.append(y1 - y0)
                b.bridge.mouse_move(0, -(y1 - y0))
                time.sleep(0.06)
            avg = sum(samples) / len(samples)
            print("  %4d  %8.1f  gain=%.2f  %s" % (m, avg, avg / m, samples))

        b.release()

    print("\n建议的控制器档位（step -> 实际位移）:")
    for m, avg, gain in rows:
        print("   发 %3d -> 走 %6.1f" % (m, avg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
