"""鼠标诊断：屏幕尺寸、当前坐标、多次移动的实际 delta。"""

import ctypes
import statistics
import sys
import time

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
PC = __import__("os").path.join(__import__("os").path.dirname(_HERE), "pc")
sys.path.insert(0, PC)

from agent_bridge import AgentBridge          # noqa: E402
from transport_uart import UartTransport      # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


u32 = ctypes.windll.user32
u32.SetProcessDPIAware()


def cursor():
    pt = POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


print(f"屏幕: {u32.GetSystemMetrics(0)} x {u32.GetSystemMetrics(1)}  (虚拟桌面: "
      f"{u32.GetSystemMetrics(78)} x {u32.GetSystemMetrics(79)})")
print(f"虚拟屏幕原点: ({u32.GetSystemMetrics(76)}, {u32.GetSystemMetrics(77)})")
print(f"当前光标: {cursor()}")
print()

tr = UartTransport(port=PORT, baud=115200)
b = AgentBridge(tr, timeout=1.5, release_on_close=False)
b.open()
b.ping()

for label, dx, dy in [("+120,+60", 120, 60), ("-120,-60", -120, -60), ("+40,0", 40, 0), ("0,+40", 0, 40)]:
    x0, y0 = cursor()
    b.mouse_move(dx, dy)
    time.sleep(0.25)
    x1, y1 = cursor()
    print(f"{label:>10}: ({x0},{y0}) -> ({x1},{y1})   delta=({x1-x0},{y1-y0})   期望≈({dx},{dy})")

# 连发 5 次 +20，看总位移（累计）
x0, y0 = cursor()
for _ in range(5):
    b.mouse_move(20, 0)
    time.sleep(0.05)
time.sleep(0.2)
x1, y1 = cursor()
print(f"{'5x(+20,0)':>10}: ({x0},{y0}) -> ({x1},{y1})   delta=({x1-x0},{y1-y0})   期望≈(100,0)")

b.release_all()
b.close(release=False)
