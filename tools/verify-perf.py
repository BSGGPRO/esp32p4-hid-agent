"""真板性能实测：ping 往返 / mouse_move 延迟 / 连发吞吐。

协议目标（docs/protocol.md §7）：
  ping 往返        < 30 ms
  mouse_move 首报  < 15 ms
  连发吞吐         >= 100 条/秒
"""

import statistics
import sys
import time

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
PC = __import__("os").path.join(__import__("os").path.dirname(_HERE), "pc")
sys.path.insert(0, PC)

from agent_bridge import AgentBridge          # noqa: E402
from transport_uart import UartTransport      # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


def pct(xs, p):
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def stats(name, xs, unit="ms"):
    print(
        f"{name:<28} n={len(xs):<3} "
        f"min={min(xs):7.2f}  mean={statistics.mean(xs):7.2f}  "
        f"p50={pct(xs, 50):7.2f}  p95={pct(xs, 95):7.2f}  max={max(xs):7.2f} {unit}"
    )
    return statistics.mean(xs)


def main():
    tr = UartTransport(port=PORT, baud=115200)
    b = AgentBridge(tr, timeout=1.5, release_on_close=False)
    b.open()

    st = b.status()
    print(f"board: fw={st.get('fw')} proto={st.get('proto')} host_mounted={st.get('host_mounted')}")
    print()

    # 预热 3 次（丢弃首帧竞态）
    for _ in range(3):
        b.ping()

    rtt = []
    for _ in range(40):
        t0 = time.perf_counter()
        b.ping()
        rtt.append((time.perf_counter() - t0) * 1000.0)
    mean_ping = stats("ping RTT (ack)", rtt)

    lat = []
    for _ in range(30):
        t0 = time.perf_counter()
        b.mouse_move(5, 0)
        lat.append((time.perf_counter() - t0) * 1000.0)
    mean_move = stats("mouse_move ack", lat)

    # 大位移拆帧：dx=1000 需要 8 帧，看总耗时
    big = []
    for _ in range(10):
        t0 = time.perf_counter()
        b.mouse_move(1000, 0)
        big.append((time.perf_counter() - t0) * 1000.0)
    stats("mouse_move dx=1000 (split)", big)

    # 连发（不等 ack）
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        b.mouse_move(1, 0, wait=False)
    dt = time.perf_counter() - t0
    rate = n / dt
    print(f"{'burst (no-wait)':<28} {n} 帧 / {dt*1000:.1f} ms  ->  {rate:.0f} 条/秒")

    # 键盘
    kt = []
    for _ in range(10):
        t0 = time.perf_counter()
        b.request("key_tap", keys=["a"])
        kt.append((time.perf_counter() - t0) * 1000.0)
    stats("key_tap 'a' ack", kt)

    b.release_all()
    b.close(release=False)

    print()
    verdicts = [
        ("ping RTT < 30ms", mean_ping < 30.0, f"{mean_ping:.2f} ms"),
        ("mouse_move ack < 15ms", mean_move < 15.0, f"{mean_move:.2f} ms"),
        ("burst >= 100/s", rate >= 100, f"{rate:.0f}/s"),
    ]
    ok = True
    for name, good, val in verdicts:
        print(f"[{'PASS' if good else 'FAIL'}] {name:<28} {val}")
        ok = ok and good
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
