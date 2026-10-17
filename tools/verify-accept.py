"""真机端到端验收：用板子当鼠标/键盘，从操作系统侧独立验证效果。

判定方式全部取自 Windows 自身状态（GetCursorPos / GetAsyncKeyState），
不依赖板子自报，避免"自己证明自己"。
"""

import ctypes
import os
import subprocess
import sys
import time

PY = sys.executable
_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
PC = __import__("os").path.join(__import__("os").path.dirname(_HERE), "pc")
PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


def run(*args, timeout=30):
    p = subprocess.run(
        [PY, "agent_bridge.py", *args, "--port", PORT],
        cwd=PC, capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


u32 = ctypes.windll.user32
VK_SHIFT = 0x10
VK_LWIN = 0x5B


def cursor():
    pt = POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def key_down(vk):
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


fails = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("=" * 70)
print("1) status —— host_mounted 必须 True")
rc, out = run("status")
print(out)
check("status.host_mounted", "host_mounted=True" in out, "")

print("=" * 70)
print("2) 鼠标：相对移动 (120, 60)，用 GetCursorPos 独立验证")
# 注：HID 相对移动会被 Windows「提高指针精确度」加速放大，实测 120 -> 233。
# 所以只断言"方向正确且确实动了"，不断言绝对幅度。
best = None
for attempt in range(1, 4):
    x0, y0 = cursor()
    rc, out = run("mouse-move", "120", "60")
    time.sleep(0.35)
    x1, y1 = cursor()
    dx, dy = x1 - x0, y1 - y0
    print(f"   尝试{attempt}: ({x0},{y0}) -> ({x1},{y1})   delta=({dx},{dy})   cli_rc={rc}")
    if best is None or (abs(dx) + abs(dy)) > (abs(best[0]) + abs(best[1])):
        best = (dx, dy)
    if dx > 0 and dy > 0:
        break
    time.sleep(0.2)
dx, dy = best
check("mouse moved right", dx > 0, f"dx={dx}")
check("mouse moved down", dy > 0, f"dy={dy}")
check("delta magnitude plausible", 0 < abs(dx) <= 800 and 0 < abs(dy) <= 600, f"({dx},{dy})")

print("=" * 70)
print("3) 键盘：按住 leftshift，用 GetAsyncKeyState 独立验证")
before = key_down(VK_SHIFT)
rc, out = run("key-down", "leftshift", "--no-release-on-exit")
time.sleep(0.25)
during = key_down(VK_SHIFT)
rc2, out2 = run("release-all")
time.sleep(0.25)
after = key_down(VK_SHIFT)
print(f"   SHIFT down: before={before} during={during} after_release={after}")
check("shift pressed by board", during is True, "")
check("shift released by board", after is False, "")

print("=" * 70)
print("4) 键盘：组合键 leftgui(win) 按住状态验证")
rc, out = run("key-down", "leftgui", "--no-release-on-exit")
time.sleep(0.25)
win_during = key_down(VK_LWIN)
rc2, out2 = run("release-all")
time.sleep(0.25)
win_after = key_down(VK_LWIN)
print(f"   LWIN down: during={win_during} after_release={win_after}")
check("leftgui pressed by board", win_during is True, "")
check("leftgui released by board", win_after is False, "")

print("=" * 70)
print("5) 安全释放：按下键后不发任何报文，1500ms 后应自动全抬")
rc, out = run("key-down", "leftctrl", "--no-release-on-exit")
time.sleep(0.2)
held = key_down(0x11)  # VK_CONTROL
print(f"   ctrl held right after command: {held}")
time.sleep(2.2)        # 超过 safety_timeout_ms=1500，且期间不发任何报文
released = not key_down(0x11)
print(f"   ctrl still held after 2.2s silence: {key_down(0x11)}")
check("safety_release fired", held is True and released is True, "")
rc, out = run("release-all")

print("=" * 70)
print("6) 端到端闭环：截屏 -> 移鼠标 -> 再截屏 -> 差分")
rc, out = run("e2e-demo", "--dx", "200", timeout=90)
print(out)
check("e2e-demo exit 0", rc == 0, f"rc={rc}")

print("=" * 70)
if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
