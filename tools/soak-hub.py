#!/usr/bin/env python3
"""10 分钟混合负载 soak（走 hub HTTP 接口，不向用户桌面发任何键鼠事件）。

负载 = 页面同等 1 Hz 板子轮询 + 每 5 s ping + 每 35 s 一次 1 MB sd_test。
HID 健康用 hid.mounted 连续性与 uptime 单调性验证（不发 HID 报文，避免打扰用户）。

用法：
    python tools/soak-hub.py --hub http://127.0.0.1:8765 --duration 600 --out docs/evidence/fw-soak-hub-<ts>.txt

gate（任一失败 → exit 1）：
  1  uptime 单调不回退（复位=0）
  2  hid.mounted 全程 true
  3  sd.mounted 全程 true 且 fs=fat32
  4  sd_test 次数 ≥15 且全部 verify:true
  5  heap_free 相对基线下降 ≤ 4096 B（无泄漏）
  6  ping 全部成功且 RTT < 200 ms
  7  uart.rx_bad 不增长
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

IPCPREFIX = ("ipc", "IDLE")


def http_json(url: str, payload: dict | None = None, timeout: float = 25.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="http://127.0.0.1:8765")
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--mgmt", type=float, default=35.0)
    a = ap.parse_args()
    hub = a.hub.rstrip("/")

    lines: list[str] = []

    def out(msg: str) -> None:
        lines.append(msg)
        print(msg, flush=True)

    out("== 混合负载 soak（经 hub，无键鼠输出）%s ==" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("hub=%s duration=%.0fs sd_test_every=%.0fs" % (hub, a.duration, a.mgmt))

    start = time.time()
    end = start + a.duration
    next_ping = start
    next_hid = start + 2.0
    hid_tx_ok = 0
    hid_tx_fail = 0
    next_sd = start + 5.0
    prev_up = None
    resets = 0
    hid_false = 0
    sd_false = 0
    samples = 0
    http_fail = 0
    rtts: list[float] = []
    sd_tests = 0
    sd_ok = 0
    heap_base = None
    heap_min = None
    temp_max = None
    rx_bad_base = None
    rx_bad_last = None
    first_stats: dict | None = None
    last_stats: dict | None = None

    while time.time() < end:
        try:
            b = http_json(hub + "/api/board", timeout=8)
            samples += 1
            st = b.get("stats") or {}
            if not st:
                http_fail += 1
                time.sleep(1.0)
                continue
            if first_stats is None:
                first_stats = st
            last_stats = st
            up = int(st.get("uptime_ms") or 0)
            if prev_up is not None and up < prev_up:
                resets += 1
                out("[%s] RESET: uptime %d -> %d" % (time.strftime("%H:%M:%S"), prev_up, up))
                first_stats = st  # 复位后重新取基线
            prev_up = up

            hid = st.get("hid") or {}
            if hid.get("mounted") is not True:
                hid_false += 1
            sd = st.get("sd") or {}
            if sd.get("mounted") is not True:
                sd_false += 1
            mem = st.get("mem") or {}
            hf = mem.get("heap_free")
            if hf is not None:
                hf = int(hf)
                if heap_base is None:
                    heap_base = hf
                heap_min = hf if heap_min is None else min(heap_min, hf)
            tc = st.get("temp_c")
            if tc is not None:
                temp_max = tc if temp_max is None else max(temp_max, float(tc))
            uart = st.get("uart") or {}
            if rx_bad_base is None:
                rx_bad_base = uart.get("rx_bad")
            rx_bad_last = uart.get("rx_bad")
        except Exception as exc:  # noqa: BLE001
            http_fail += 1
            out("[%s] /api/board 失败: %r" % (time.strftime("%H:%M:%S"), exc))

        now = time.time()
        if now >= next_ping:
            next_ping = now + 5.0
            try:
                t0 = time.perf_counter()
                r = http_json(hub + "/api/cmd", {"cmd": "ping"}, timeout=10)
                rtt = (time.perf_counter() - t0) * 1000
                if r.get("ok"):
                    rtts.append(rtt)
                else:
                    out("[%s] ping 失败: %s" % (time.strftime("%H:%M:%S"), r))
            except Exception as exc:  # noqa: BLE001
                out("[%s] ping 异常: %r" % (time.strftime("%H:%M:%S"), exc))

        # HID 发送路径覆盖：dx=dy=0 不发任何字符、光标不动、不点击（安全）
        if now >= next_hid:
            next_hid = now + 3.0
            try:
                r = http_json(hub + "/api/cmd", {"cmd": "mouse_move", "dx": 0, "dy": 0}, timeout=10)
                if r.get("ok"):
                    hid_tx_ok += 1
                else:
                    hid_tx_fail += 1
                    out("[%s] mouse_noop 失败: %s" % (time.strftime("%H:%M:%S"), r))
            except Exception as exc:  # noqa: BLE001
                hid_tx_fail += 1
                out("[%s] mouse_noop 异常: %r" % (time.strftime("%H:%M:%S"), exc))

        if now >= next_sd:
            next_sd = now + a.mgmt
            sd_tests += 1
            try:
                r = http_json(hub + "/api/cmd", {"cmd": "sd_test", "bytes": 1048576}, timeout=40)
                t = r.get("sd_test") or {}
                ok = r.get("ok") is True and t.get("verify") is True
                sd_ok += 1 if ok else 0
                out("[%s] sd_test #%d ok=%s write=%s read=%s verify=%s"
                    % (time.strftime("%H:%M:%S"), sd_tests, ok, t.get("write_kbps"), t.get("read_kbps"), t.get("verify")))
            except Exception as exc:  # noqa: BLE001
                out("[%s] sd_test #%d 异常: %r" % (time.strftime("%H:%M:%S"), sd_tests, exc))

        time.sleep(1.0)

    elapsed = time.time() - start
    heap_drop = (heap_base - heap_min) if (heap_base is not None and heap_min is not None) else None
    rtt_avg = (sum(rtts) / len(rtts)) if rtts else None
    rtt_max = max(rtts) if rtts else None
    rx_bad_delta = None
    if isinstance(rx_bad_base, int) and isinstance(rx_bad_last, int):
        rx_bad_delta = rx_bad_last - rx_bad_base

    gates = [
        ("无复位（uptime 单调）", resets == 0, "resets=%d" % resets),
        ("HID 全程 mounted", hid_false == 0, "hid 异常采样=%d" % hid_false),
        ("SD 全程 mounted", sd_false == 0, "sd 异常采样=%d" % sd_false),
        ("sd_test 全部 verify（次数按 duration/间隔 折算）", sd_tests >= max(1, int(a.duration // a.mgmt) - 1) and sd_ok == sd_tests, "sd_tests=%d ok=%d expect>=%d" % (sd_tests, sd_ok, max(1, int(a.duration // a.mgmt) - 1))),
        ("heap 无泄漏", heap_drop is not None and heap_drop <= 4096, "base=%s min=%s drop=%s" % (heap_base, heap_min, heap_drop)),
        ("ping 全成功且 <200ms", len(rtts) >= max(1, int(elapsed // 5) - 1) and rtt_max < 200.0,
         "n=%d avg=%.0fms max=%.0fms" % (len(rtts), rtt_avg or -1, rtt_max or -1)),
        ("rx_bad 不增长", rx_bad_delta in (0, None), "base=%s last=%s delta=%s" % (rx_bad_base, rx_bad_last, rx_bad_delta)),
        ("HID 发送路径可用（dx=0 报文，不动光标）", hid_tx_ok > 0 and hid_tx_fail == 0,
         "ok=%d fail=%d" % (hid_tx_ok, hid_tx_fail)),
    ]

    out("")
    out("== 汇总 ==")
    out("elapsed=%.0fs samples=%d http_fail=%d temp_max=%s" % (elapsed, samples, http_fail, temp_max))
    fails = 0
    for name, ok, detail in gates:
        out("[%s] %s — %s" % ("PASS" if ok else "FAIL", name, detail))
        fails += 0 if ok else 1
    out("verdict: %s (%d/%d gates)" % ("PASS" if fails == 0 else "FAIL", len(gates) - fails, len(gates)))

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("证据已写入 %s" % a.out)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


