"""专项回归：限流风暴中的 release_all 必须成功（安全命令免限流）。

复现原缺陷：连发 200 帧后立即 release_all -> 旧固件回 rate_limited，按键卡死。
"""

import sys
import time

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
PC = __import__("os").path.join(__import__("os").path.dirname(_HERE), "pc")
sys.path.insert(0, PC)

from agent_bridge import AgentBridge, ProtocolError   # noqa: E402
from transport_uart import UartTransport              # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"

tr = UartTransport(port=PORT, baud=115200)
b = AgentBridge(tr, timeout=1.5, release_on_close=False)
b.open()
b.ping()

fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# 1) 先按住一个键（制造"真的卡住"的前提）
b.key_down(["leftctrl"])
time.sleep(0.2)

# 2) 用 no-wait 连发 250 帧，把 200 条/秒的限流窗口打满
for _ in range(250):
    b.mouse_move(1, 0, wait=False)
print("  已连发 250 帧（限流窗口应已打满）")

# 3) 立刻验证「普通命令仍然会被限流」——证明限流没被整体关掉（信息性，不判失败）
limited_count = 0
for _ in range(5):
    try:
        b.request("mouse_move", dx=1, dy=0)
    except ProtocolError as exc:
        if getattr(exc, "err", None) == "rate_limited":
            limited_count += 1
print(f"  紧随其后的 5 条普通命令中，被限流 {limited_count} 条")
check("normal command still rate limited", limited_count > 0,
      "0 条被限流（可能窗口已滑动，非缺陷）" if limited_count == 0 else "")

# 4) 紧接着发 release_all —— 必须 ok:true
try:
    resp = b.release_all()
    ok = bool(resp and resp.get("ok") is True)
    print(f"  release_all -> {resp}")
    check("release_all survives rate-limit storm", ok)
except ProtocolError as exc:
    print(f"  release_all -> ProtocolError {exc}")
    check("release_all survives rate-limit storm", False, str(exc))

# 5) 退避 1.1s 后应恢复正常
time.sleep(1.2)
try:
    b.ping()
    recovered = True
except Exception:  # noqa: BLE001
    recovered = False
check("recovers after backoff", recovered)

b.release_all()
b.close(release=False)

print()
print("RESULT:", "PASS" if not fails else "FAIL " + ",".join(fails))
sys.exit(0 if not fails else 1)
