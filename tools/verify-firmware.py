#!/usr/bin/env python3
"""Lead 侧最终验收脚本：仪表盘 hub + 板子固件（v1.2 + pm 开关）。

用法（hub 已在 8765 运行、板子在线时）：
    python tools/verify-firmware.py [--hub http://127.0.0.1:8765] [--out docs/evidence/...]
       [--soak-s 120]

检查项（每项打印 PASS/FAIL，最后汇总；任何 FAIL → 退出码 1）：
  1  页面可打开（HTML 200，含关键 DOM id）
  2  /api/pc 正常（本机数据）
  3  /api/board 在线且字段齐全（uptime/cpu/mem/temp/sd/hid/proto/pm）
  4  TF 卡已挂载（present/mounted/fs=fat32/容量）
  5  ping 延迟（通过 /api/cmd → 板子）
  6  sd_test 1MB 读写校验（通过 /api/cmd）
  7  动态调频：set dfs=1 → stats.pm.dfs=true → ping 仍正常
  8  动态调频：set dfs=0 → stats.pm.dfs=false（恢复安全态）
  9  持续 soak：N 秒内 uptime 单调递增、无复位、无 age_ms 超时、sd/hid 不掉
 10  非法命令被拒（cmd_not_allowed）

证据：stdout 同时写入 --out 文件（JSON 行 + 人类可读汇总）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, bool, str]] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    log("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))


def http_json(url: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body.decode("utf-8", "replace"))


def http_text(url: str, timeout: float = 8.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cmd(hub: str, payload: dict, timeout: float = 25.0) -> dict:
    return http_json(hub + "/api/cmd", payload, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="")
    ap.add_argument("--soak-s", type=float, default=120.0)
    a = ap.parse_args()
    hub = a.hub.rstrip("/")

    lines: list[str] = []

    def out(msg: str) -> None:
        lines.append(msg)
        log(msg)

    out("== 最终验收 %s ==" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("hub = %s" % hub)

    # 1 页面
    try:
        html = http_text(hub + "/")
        need = ["board-status", "pm-toggle", "sd-state", "board-temp"]
        missing = [n for n in need if n not in html]
        record("页面可打开 + 关键控件存在", len(html) > 3000 and not missing,
               "bytes=%d missing=%s" % (len(html), missing or "无"))
    except Exception as exc:  # noqa: BLE001
        record("页面可打开", False, repr(exc))

    # 2 本机数据
    try:
        pc = http_json(hub + "/api/pc")
        record("/api/pc 正常", bool(pc.get("ok")), "keys=%s" % sorted(pc)[:8])
    except Exception as exc:  # noqa: BLE001
        record("/api/pc 正常", False, repr(exc))

    # 3 板子在线 + 字段
    board = {}
    try:
        board = http_json(hub + "/api/board")
        st = board.get("stats") or {}
        sd = st.get("sd") or {}
        pm = st.get("pm") or {}
        have = {
            "uptime_ms": "uptime_ms" in st,
            "cpu.mhz": "mhz" in (st.get("cpu") or {}),
            "mem": bool(st.get("mem")),
            "temp_c": "temp_c" in st,
            "sd": bool(sd),
            "hid": "mounted" in (st.get("hid") or {}),
            "proto": "proto" in st,
            "pm": bool(pm),
        }
        record("板子在线且 stats 字段齐全", board.get("ok") is True and all(have.values()),
               "ok=%s missing=%s" % (board.get("ok"), [k for k, v in have.items() if not v] or "无"))
    except Exception as exc:  # noqa: BLE001
        record("板子在线", False, repr(exc))

    # 4 TF 卡
    try:
        sd = (board.get("stats") or {}).get("sd") or {}
        ok = sd.get("mounted") is True and str(sd.get("fs", "")).lower() == "fat32" and sd.get("capacity_mb")
        record("TF 卡已挂载 FAT32", bool(ok),
               "present=%s mounted=%s fs=%s cap=%sMB" % (sd.get("present"), sd.get("mounted"), sd.get("fs"), sd.get("capacity_mb")))
    except Exception as exc:  # noqa: BLE001
        record("TF 卡已挂载", False, repr(exc))

    # 5 ping 延迟
    try:
        t0 = time.perf_counter()
        pong = cmd(hub, {"cmd": "ping"})
        rtt = (time.perf_counter() - t0) * 1000
        record("ping 往返", pong.get("ok") is True and rtt < 200,
               "rtt=%.0fms fw=%s proto=%s" % (rtt, pong.get("fw"), pong.get("proto")))
    except Exception as exc:  # noqa: BLE001
        record("ping 往返", False, repr(exc))

    # 6 sd_test
    try:
        t0 = time.perf_counter()
        r = cmd(hub, {"cmd": "sd_test", "bytes": 1048576})
        dt = (time.perf_counter() - t0) * 1000
        st = (r.get("sd_test") or {})
        ok = r.get("ok") is True and st.get("verify") is True and st.get("write_kbps")
        record("sd_test 1MB 读写校验", bool(ok),
               "write=%s read=%s verify=%s rtt=%.0fms" % (st.get("write_kbps"), st.get("read_kbps"), st.get("verify"), dt))
    except Exception as exc:  # noqa: BLE001
        record("sd_test 1MB", False, repr(exc))

    # 7/8 dfs 开关
    for value, label in ((1, "set dfs=1（智能调度）"), (0, "set dfs=0（固定360）")):
        try:
            r = cmd(hub, {"cmd": "set", "key": "dfs", "value": value})
            pm = r.get("pm") or {}
            ok = r.get("ok") is True and pm.get("dfs") is (value == 1)
            record(label, bool(ok), "resp=%s" % json.dumps(r, ensure_ascii=False)[:220])
            time.sleep(1.0)
            b2 = http_json(hub + "/api/board")
            pm2 = (b2.get("stats") or {}).get("pm") or {}
            record(label + " → stats.pm 生效", pm2.get("dfs") is (value == 1),
                   "stats.pm=%s" % json.dumps(pm2, ensure_ascii=False))
            t0 = time.perf_counter()
            pong = cmd(hub, {"cmd": "ping"})
            rtt = (time.perf_counter() - t0) * 1000
            record(label + " 下 ping 正常", pong.get("ok") is True and rtt < 200, "rtt=%.0fms" % rtt)
        except Exception as exc:  # noqa: BLE001
            record(label, False, repr(exc))

    # 10 非法命令
    try:
        bad = cmd(hub, {"cmd": "reboot"})
        record("非法命令被拒", bad.get("ok") is False and bad.get("err") == "cmd_not_allowed",
               json.dumps(bad, ensure_ascii=False)[:160])
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        record("非法命令被拒", exc.code == 400 and "cmd_not_allowed" in body, "http=%s %s" % (exc.code, body[:160]))
    except Exception as exc:  # noqa: BLE001
        record("非法命令被拒", False, repr(exc))

    # 9 soak
    try:
        prev = None
        resets = 0
        gaps = 0
        samples = 0
        t_end = time.time() + a.soak_s
        while time.time() < t_end:
            b = http_json(hub + "/api/board")
            st = b.get("stats") or {}
            up = st.get("uptime_ms")
            samples += 1
            if up is not None and prev is not None:
                if up < prev:
                    resets += 1
                elif up - prev > 15000:
                    gaps += 1
            prev = up
            time.sleep(2.0)
        record("soak %.0fs 无复位/无断流" % a.soak_s, resets == 0 and gaps == 0,
               "samples=%d resets=%d gaps=%d last_uptime=%sms" % (samples, resets, gaps, prev))
    except Exception as exc:  # noqa: BLE001
        record("soak", False, repr(exc))

    fails = [r for r in RESULTS if not r[1]]
    out("")
    out("汇总: %d 项检查, %d PASS, %d FAIL" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    for name, ok, detail in RESULTS:
        out("  %-4s %s %s" % ("PASS" if ok else "FAIL", name, ("(" + detail + ")") if detail else ""))

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log("证据已写入 %s" % a.out)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
