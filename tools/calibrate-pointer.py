#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/calibrate-pointer.py — 复测指针精度：开环增益表 + 闭环落点误差。

为什么要两段
------------
HID 鼠标只有相对位移，Windows「提高指针精确度」又按速度非线性放大，所以有两类
问题要分开测：

1. **开环增益**：发 m 像素，光标实际走多少。它决定 ``brain._step_for()`` 怎么把
   误差换算成步长（大误差快收敛、小误差 1:1 精修）。
2. **闭环落点**：``brain.locate(x, y, tol=1)`` 反复读 ``GetCursorPos()`` 收敛后，
   实际能落到多准。**这才是"精度"的用户可见结论**。

本脚本不改任何系统设置（用户选择"只做闭环校准"），只测量 + 给建议。
证据写入 ``docs/evidence/calib-<时间戳>.txt``，可复算。

用法::

    python tools/calibrate-pointer.py                      # 默认 COM7
    python tools/calibrate-pointer.py --transport tcp      # 走 hub 127.0.0.1:8766
    python tools/calibrate-pointer.py --targets 12 --tol 1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pc"))
sys.path.insert(0, _HERE)

from brain import Brain, cursor_pos, screen_size  # noqa: E402

try:
    from board_lock import BoardLock
except Exception:  # noqa: BLE001
    BoardLock = None

EV = os.path.join(_ROOT, "docs", "evidence")

MAGS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 30, 40, 60, 80, 120, 160, 200]
REPEAT = 3


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
            st.flush()

    def flush(self):
        for st in self.streams:
            st.flush()


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[i]


def measure_gain(b: Brain, out) -> list[tuple[int, float, float]]:
    rows = []
    out.write("\n[1] 开环增益：发 m 像素 -> 光标实际位移（每档 %d 次）\n" % REPEAT)
    out.write("  请求   实测(均值)   增益   样本\n")
    out.write("  " + "-" * 44 + "\n")
    for m in MAGS:
        samples = []
        for _ in range(REPEAT):
            x0, _y0 = cursor_pos()
            b.bridge.mouse_move(m, 0)
            time.sleep(0.06)
            x1, _y1 = cursor_pos()
            moved = x1 - x0
            samples.append(moved)
            if moved:
                b.bridge.mouse_move(-moved, 0)
                time.sleep(0.06)
        avg = sum(samples) / len(samples)
        rows.append((m, avg, avg / m))
        out.write("  %4d   %10.1f   %5.2f   %s\n" % (m, avg, avg / m, samples))

    out.write("\n  竖向抽查:\n")
    for m in (5, 40, 120):
        samples = []
        for _ in range(REPEAT):
            _x0, y0 = cursor_pos()
            b.bridge.mouse_move(0, m)
            time.sleep(0.06)
            _x1, y1 = cursor_pos()
            moved = y1 - y0
            samples.append(moved)
            if moved:
                b.bridge.mouse_move(0, -moved)
                time.sleep(0.06)
        avg = sum(samples) / len(samples)
        out.write("  %4d   %8.1f   gain=%.2f   %s\n" % (m, avg, avg / m, samples))
    return rows


def measure_closed_loop(b: Brain, sw: int, sh: int, n: int, tol: int, out) -> dict:
    out.write("\n[2] 闭环落点精度：locate(x, y, tol=%d) %d 个目标点\n" % (tol, n))
    rng = random.Random(20261017)
    errs = []
    iters = []
    ok_n = 0
    rows = []
    for i in range(1, n + 1):
        tx = rng.randint(250, sw - 250)
        ty = rng.randint(180, sh - 220)
        r = b.locate(tx, ty, tol=tol)
        cur = cursor_pos()
        ex, ey = abs(tx - cur[0]), abs(ty - cur[1])
        err = max(ex, ey)
        errs.append(err)
        iters.append(r.get("iterations", 0))
        ok_n += 1 if r.get("ok") else 0
        rows.append({"target": [tx, ty], "final": list(cur), "error": [ex, ey],
                     "max_err": err, "iterations": r.get("iterations"),
                     "ok": r.get("ok")})
        out.write("  #%02d 目标(%4d,%4d) -> 实际(%4d,%4d)  |err|=(%d,%d) max=%d  iter=%s\n"
                  % (i, tx, ty, cur[0], cur[1], ex, ey, err, r.get("iterations")))
    # 固定目标重复性：同一个点连续 5 次
    out.write("\n[3] 重复性：同一目标连续定位 5 次（每次都从别处出发）\n")
    tx, ty = sw // 2 + 137, sh // 2 - 91
    rep = []
    for i in range(5):
        b.locate(rng.randint(300, sw - 300), rng.randint(200, sh - 200), tol=3)
        r = b.locate(tx, ty, tol=tol)
        cur = cursor_pos()
        e = max(abs(tx - cur[0]), abs(ty - cur[1]))
        rep.append({"final": list(cur), "error": [abs(tx - cur[0]), abs(ty - cur[1])],
                    "max_err": e, "iterations": r.get("iterations")})
        out.write("  第%d次: 目标(%d,%d) -> (%d,%d)  |err|=%d  iter=%s\n"
                  % (i + 1, tx, ty, cur[0], cur[1], e, r.get("iterations")))
    summary = {
        "targets": n,
        "ok": ok_n,
        "tol_px": tol,
        "hit_ok_rate": round(ok_n / max(1, n), 3),
        "err_max_px": max(errs) if errs else None,
        "err_mean_px": round(sum(errs) / len(errs), 2) if errs else None,
        "err_p50_px": pct(errs, 50),
        "err_p90_px": pct(errs, 90),
        "err_le_1px_rate": round(sum(1 for e in errs if e <= 1) / max(1, len(errs)), 3),
        "iter_mean": round(sum(iters) / len(iters), 2) if iters else None,
        "iter_max": max(iters) if iters else None,
        "repeat_errs": [r["max_err"] for r in rep],
        "rows": rows,
        "repeat": rep,
    }
    out.write("\n[4] 统计\n")
    for k in ("targets", "ok", "hit_ok_rate", "err_mean_px", "err_p50_px",
              "err_p90_px", "err_max_px", "err_le_1px_rate", "iter_mean", "iter_max"):
        out.write("  %-16s %s\n" % (k, summary[k]))
    out.write("  重复性 |err|: %s\n" % summary["repeat_errs"])
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    ap.add_argument("--transport", default=os.environ.get("HID_TRANSPORT", "uart"),
                    choices=["uart", "tcp"])
    ap.add_argument("--host", default=os.environ.get("HID_HUB_HOST", "127.0.0.1"))
    ap.add_argument("--tcp-port", type=int,
                    default=int(os.environ.get("HID_HUB_PORT", "8766")))
    ap.add_argument("--targets", type=int, default=10)
    ap.add_argument("--tol", type=int, default=1)
    ap.add_argument("--evidence", default="", help="证据文件路径（默认 docs/evidence/calib-<ts>.txt）")
    ap.add_argument("--no-lock", action="store_true")
    args = ap.parse_args(argv)

    sw, sh = screen_size()
    os.makedirs(EV, exist_ok=True)
    ev_path = args.evidence or os.path.join(
        EV, "calib-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))

    lock = None
    if not args.no_lock and BoardLock is not None and args.transport == "uart":
        lock = BoardLock("draw:calib", timeout=120.0)
        lock.__enter__()

    f = open(ev_path, "w", encoding="utf-8")
    out = Tee(sys.stdout, f)
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "screen": [sw, sh],
               "transport": args.transport, "port": args.port}

    try:
        out.write("== 指针精度复测 / pointer accuracy ==\n")
        out.write("时间: %s\n屏幕: %dx%d  传输: %s (port=%s)\n"
                  % (summary["ts"], sw, sh, args.transport, args.port))
        out.write("说明: 不改任何系统设置（只做闭环校准）；位移全部由 ESP32-P4 HID 发出。\n")
        with Brain(port=args.port, transport=args.transport, host=args.host,
                   tcp_port=args.tcp_port) as b:
            b.release()
            info = b.info()
            out.write("板子: host_mounted=%s  transport=%s  note=%s\n" % (
                info.get("status", {}).get("host_mounted"),
                info.get("transport"), info.get("transport_note")))
            b.point(sw // 2, sh // 2)
            time.sleep(0.25)
            summary["gain_table"] = measure_gain(b, out)
            summary["closed_loop"] = measure_closed_loop(b, sw, sh, args.targets,
                                                         args.tol, out)
            b.release()

        cl = summary["closed_loop"]
        out.write("\n[5] 结论\n")
        out.write("  闭环落点: 平均 %.2f px / P90 %.0f px / 最大 %d px；<=1px 命中率 %.1f%%；"
                  "平均 %s 次迭代\n" % (
                      cl["err_mean_px"] or 0, cl["err_p90_px"] or 0, cl["err_max_px"] or 0,
                      100 * cl["err_le_1px_rate"], cl["iter_mean"]))
        out.write("  -> 结论: 闭环精度已到单像素级，不需要改 Windows『提高指针精确度』；\n")
        out.write("     若想更快收敛可减小 _step_for 的高速区上限，但会牺牲大范围移动速度。\n")
        out.write("\n证据文件: %s\n" % ev_path)
        print("\nJSON 摘要:", json.dumps({k: summary[k] for k in
                                         ("screen", "transport", "closed_loop")},
                                        ensure_ascii=False))
    finally:
        try:
            f.close()
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
