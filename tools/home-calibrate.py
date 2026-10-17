#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/home-calibrate.py — CNC 式「四角归零」指针标定。

思路（和 CNC 开机回零一样）
--------------------------
屏幕四边就是**硬限位开关**：鼠标是相对位移设备，Windows 会把光标裁在虚拟桌面
边界上，所以「拼命往左上推」最后一定精确停在 (0,0)，不需要知道分辨率。

流程：
  1. 撞边回零 —— 依次把光标推到 左上 / 右上 / 右下 / 左下，记录四个角坐标；
  2. 量量具   —— 四角坐标直接给出该设备的虚拟桌面尺寸与原点（与 GetSystemMetrics
                 对比，可发现缩放/多屏/换设备）；
  3. 重标增益 —— 在该设备上重测「发 m 个 count 光标走多少像素」（加速曲线），
                 写进 profile，供 brain 的增益表使用；
  4. 验收     —— 四个角附近 + 屏幕中心，闭环 locate(tol=1) 落点误差；
  5. 存档     —— profile 按「分辨率+虚拟桌面+DPI 模式」做 key 写入
                 C:\\ProgramData\\dsh\\pointer-profile.json；换设备时 key 对不上
                 → 自动重新跑这一套（见 pc/pointer_profile.py 的 ensure()）；
  6. 收工     —— 把光标放回标定前的位置，不发任何点击。

安全：只移动光标，不点击、不按键；全程闭环读 GetCursorPos；任一步不收敛立即停。

用法::

    python tools/home-calibrate.py                       # 走 hub 透传（推荐，不用停 hub）
    python tools/home-calibrate.py --transport uart      # 直连 COM7（需板子锁）
    python tools/home-calibrate.py --no-gain             # 跳过增益重测（更快）
    python tools/home-calibrate.py --apply               # 标定后把增益表写进 profile
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pc"))
sys.path.insert(0, _HERE)

from brain import Brain, cursor_pos, screen_size  # noqa: E402

try:
    import eyes  # noqa: E402
except Exception:  # noqa: BLE001
    eyes = None

try:
    from board_lock import BoardLock
except Exception:  # noqa: BLE001
    BoardLock = None

EV = os.path.join(_ROOT, "docs", "evidence")
PROFILE_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "dsh")
PROFILE_PATH = os.path.join(PROFILE_DIR, "pointer-profile.json")

PUSH_COUNTS = 1500     # 每次撞边推的 count 数（固件自动拆成 <=127 的帧）
PUSH_ROUNDS = 8        # 最多推几轮（两轮位置不变 = 到边）
SETTLE_S = 0.10

GAIN_MAGS = [1, 2, 3, 4, 6, 8, 12, 16, 24, 40, 60, 120, 200]
GAIN_REPEAT = 2

CORNER_NAMES = ["左上", "右上", "右下", "左下"]


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


def push_until_edge(b: Brain, sx: int, sy: int) -> tuple[tuple[int, int], list]:
    """朝 (sx, sy) 方向狂推，直到光标不再移动（撞到虚拟桌面边界）。

    返回 (最终位置, 每轮轨迹)。
    """
    rounds = []
    last = cursor_pos()
    for i in range(PUSH_ROUNDS):
        b.bridge.mouse_move(sx * PUSH_COUNTS, sy * PUSH_COUNTS)
        time.sleep(SETTLE_S)
        cur = cursor_pos()
        rounds.append({"round": i + 1, "pos": [cur[0], cur[1]]})
        if cur == last:
            return cur, rounds
        last = cur
    return last, rounds


def measure_gain(b: Brain, out: Tee, sw: int, sh: int) -> list[list[float]]:
    """重测该设备上的 count->px 增益（加速曲线）。

    注意：必须在屏幕中间量，否则在四角撞边后的边界上某一方向被裁掉会量出 0。
    小位移有量化噪声（1 count 可能 <1 px），所以 <=8 时连发 10 次取总量平均。
    """
    b.locate(sw // 2, sh // 2, tol=3)
    time.sleep(0.08)
    out.write("\n[3] 增益重测（屏幕中心量，单位为 px/count）\n")
    out.write("    请求   实测均值   增益   样本\n")
    rows = []
    for m in GAIN_MAGS:
        if m <= 8:
            k = 10
            x0 = cursor_pos()[0]
            for _ in range(k):
                b.bridge.mouse_move(m, 0)
                time.sleep(0.02)
            time.sleep(0.06)
            moved = cursor_pos()[0] - x0
            avg = moved / k
            samples = [round(avg, 2)]
            b.locate(sw // 2, sh // 2, tol=3)
        else:
            samples = []
            for _ in range(GAIN_REPEAT):
                b.locate(sw // 2, sh // 2, tol=3)
                x0 = cursor_pos()[0]
                b.bridge.mouse_move(m, 0)
                time.sleep(0.06)
                moved = cursor_pos()[0] - x0
                samples.append(moved)
                b.locate(sw // 2, sh // 2, tol=3)
            avg = sum(samples) / len(samples)
        rows.append([float(m), round(avg / m, 3)])
        out.write("    %4d   %8.1f   %5.2f   %s\n" % (m, avg, avg / m, samples))

    out.write("\n    竖向抽查（中心，两个方向取绝对值均值）:\n")
    for m in (3, 40, 120):
        moved = []
        for sign in (+1, -1):
            b.locate(sw // 2, sh // 2, tol=3)
            y0 = cursor_pos()[1]
            b.bridge.mouse_move(0, sign * m)
            time.sleep(0.06)
            moved.append(abs(cursor_pos()[1] - y0))
        avg = sum(moved) / len(moved)
        out.write("    %4d   %8.1f   gain=%.2f   %s\n" % (m, avg, avg / m, moved))

    # 单调性（只查 >=6 的有效区，容忍量化噪声 0.2）
    real = [(m, g) for m, g in rows if m >= 6]
    bad = [(real[i], real[i + 1]) for i in range(len(real) - 1)
           if real[i + 1][1] + 0.20 < real[i][1]]
    out.write("    单调性(>=6): %s\n" % ("OK" if not bad else "异常 %s" % bad))
    if bad:
        out.write("    ! 增益表不单调，profile 仍会保存，但建议人工确认指针加速设置\n")
    return rows


def corner_checks(b: Brain, corners: dict, out: Tee, inset: int = 24) -> list[dict]:
    """四角附近 + 中心闭环定位验收。"""
    out.write("\n[4] 闭环落点验收（tol=1）\n")
    tl, tr, br, bl = corners["TL"], corners["TR"], corners["BR"], corners["BL"]
    targets = [
        ("左上角内缩", (tl[0] + inset, tl[1] + inset)),
        ("右上角内缩", (tr[0] - inset, tr[1] + inset)),
        ("右下角内缩", (br[0] - inset, br[1] - inset)),
        ("左下角内缩", (bl[0] + inset, bl[1] - inset)),
        ("屏幕中心", ((tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2)),
    ]
    results = []
    for name, (tx, ty) in targets:
        r = b.locate(tx, ty, tol=1)
        x, y = cursor_pos()
        err = max(abs(tx - x), abs(ty - y))
        results.append({"name": name, "target": [tx, ty], "final": [x, y],
                        "err_px": err, "iterations": r.get("iterations"),
                        "ok": bool(r.get("ok"))})
        out.write("    %-12s 目标(%4d,%4d) -> (%4d,%4d)  |err|=%d  iter=%s  %s\n"
                  % (name, tx, ty, x, y, err, r.get("iterations"),
                     "OK" if err <= 1 else "MISS"))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default=os.environ.get("HID_TRANSPORT", "tcp"),
                    choices=["uart", "tcp"])
    ap.add_argument("--host", default=os.environ.get("HID_HUB_HOST", "127.0.0.1"))
    ap.add_argument("--tcp-port", type=int, default=int(os.environ.get("HID_HUB_PORT", "8766")))
    ap.add_argument("--no-gain", action="store_true", help="跳过增益重测（更快）")
    ap.add_argument("--apply", action="store_true", help="把增益表写入 profile（供 brain 使用）")
    ap.add_argument("--no-lock", action="store_true")
    a = ap.parse_args(argv)

    sw, sh = screen_size()
    sig_info = {}
    if eyes is not None:
        fn = getattr(eyes, "screen_metrics", None) or getattr(eyes, "screen_info", None)
        if fn is not None:
            try:
                sig_info = fn()
            except Exception:  # noqa: BLE001
                sig_info = {}

    os.makedirs(EV, exist_ok=True)
    ev_path = os.path.join(EV, "home-calib-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    lock = None
    if not a.no_lock and BoardLock is not None and a.transport == "uart":
        lock = BoardLock("draw:home-calib", timeout=120.0)
        lock.__enter__()

    f = open(ev_path, "w", encoding="utf-8")
    out = Tee(sys.stdout, f)
    started = time.time()
    orig = cursor_pos()
    summary = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "screen_primary": [sw, sh],
               "screen_info": sig_info, "transport": a.transport,
               "start_cursor": list(orig)}
    ok = True
    try:
        out.write("== CNC 式四角归零标定 / four-corner homing calibration ==\n")
        out.write("时间: %s\n" % summary["ts"])
        out.write("主屏(GetSystemMetrics): %dx%d   DPI 模式: %s\n"
                  % (sw, sh, sig_info.get("dpi_mode")))
        if sig_info.get("virtual"):
            out.write("虚拟桌面: %sx%s @ %s\n" %
                      (sig_info["virtual"][0], sig_info["virtual"][1],
                       sig_info.get("virtual_origin")))
        out.write("传输: %s（tcp = 走 hub 8766 透传，不用停网页）\n" % a.transport)

        with Brain(port="COM7", transport=a.transport, host=a.host,
                   tcp_port=a.tcp_port) as b:
            b.release()
            out.write("板子: %s\n" % json.dumps(
                {k: b.info().get(k) for k in ("transport", "transport_note")},
                ensure_ascii=False))

            # ---- 1) 四角撞边 ----
            out.write("\n[1] 四角撞边回零（每轮推 %d counts）\n" % PUSH_COUNTS)
            plan = [("TL", -1, -1), ("TR", +1, -1), ("BR", +1, +1), ("BL", -1, +1)]
            corners = {}
            for idx, (name, sx, sy) in enumerate(plan):
                pos, rounds = push_until_edge(b, sx, sy)
                corners[name] = [pos[0], pos[1]]
                out.write("    %s(%s) 方向(%+d,%+d) -> 停在 (%d,%d)，用了 %d 轮: %s\n"
                          % (name, CORNER_NAMES[idx], sx, sy, pos[0], pos[1],
                             len(rounds), [r["pos"] for r in rounds]))
            summary["corners"] = corners

            # ---- 2) 量具：四角给出的桌面尺寸 ----
            (vx0, vy0), (vx1, vy1) = corners["TL"], corners["BR"]
            width, height = (vx1 - vx0 + 1), (vy1 - vy0 + 1)
            out.write("\n[2] 四角实测虚拟桌面: 原点(%d,%d)  尺寸 %dx%d\n"
                      % (vx0, vy0, width, height))
            if sig_info.get("virtual"):
                vw, vh = sig_info["virtual"]
                ox, oy = sig_info.get("virtual_origin", (0, 0))
                match = (width == vw and height == vh and vx0 == ox and vy0 == oy)
                out.write("    GetSystemMetrics 对比: 原点(%d,%d) 尺寸 %dx%d -> %s\n"
                          % (ox, oy, vw, vh, "一致" if match else "不一致（缩放/多屏以实测为准）"))
            else:
                match = None
            corners_ok = (abs(vx1 - (vx0 + width - 1)) <= 1) and width > 100 and height > 100
            out.write("    四角自洽: %s\n" % ("OK" if corners_ok else "FAIL"))
            ok = ok and corners_ok

            summary["desktop"] = {"origin": [vx0, vy0], "size": [width, height],
                                  "match_systemmetrics": match}

            # ---- 3) 增益重测 ----
            if not a.no_gain:
                gain_rows = measure_gain(b, out, sw, sh)
                summary["gain_table"] = gain_rows
                out.write("    增益表: %s\n" % gain_rows)
                real = [(m, g) for m, g in gain_rows if m >= 6]
                bad = [(real[i], real[i + 1]) for i in range(len(real) - 1)
                       if real[i + 1][1] + 0.20 < real[i][1]]
                ok = ok and not bad

            # ---- 4) 验收 ----
            checks = corner_checks(b, corners, out)
            summary["checks"] = checks
            worst = max(c["err_px"] for c in checks)
            out.write("    最差误差: %d px，%d/%d 命中 <=1px\n"
                      % (worst, sum(1 for c in checks if c["err_px"] <= 1), len(checks)))
            ok = ok and worst <= 1

            # ---- 6) 光标放回去 ----
            try:
                b.locate(orig[0], orig[1], tol=2)
                out.write("\n[6] 光标已放回标定前位置 %s\n" % (list(orig),))
            except Exception as exc:  # noqa: BLE001
                out.write("\n[6] 光标未能放回: %r\n" % (exc,))
            b.release()

        # ---- 5) 存 profile ----
        signature = "%dx%d|virtual=%sx%s|dpi=%s" % (
            sw, sh,
            (sig_info.get("virtual") or (sw, sh))[0],
            (sig_info.get("virtual") or (sw, sh))[1],
            sig_info.get("dpi_mode", "unknown"))
        prof = {"signature": signature, "ts": summary["ts"],
                "desktop": summary.get("desktop"),
                "gain_table": summary.get("gain_table"),
                "checks": summary.get("checks"),
                "corners": summary.get("corners"),
                "screen_info": sig_info}
        os.makedirs(PROFILE_DIR, exist_ok=True)
        try:
            data = {}
            if os.path.exists(PROFILE_PATH):
                with open(PROFILE_PATH, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
            data[signature] = prof
            data["_last"] = signature
            with open(PROFILE_PATH, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            out.write("\n[5] profile 已写: %s\n    key: %s\n" % (PROFILE_PATH, signature))
        except Exception as exc:  # noqa: BLE001
            out.write("\n[5] profile 写入失败: %r\n" % (exc,))

        if a.apply and summary.get("gain_table"):
            try:
                import brain as brain_mod
                brain_mod.set_gain_table([(m, g) for m, g in summary["gain_table"]])
                out.write("    已应用到本次进程的 brain 增益表\n")
            except Exception as exc:  # noqa: BLE001
                out.write("    应用增益表失败: %r\n" % (exc,))

        elapsed = time.time() - started
        out.write("\n[7] 结论\n")
        out.write("  四角归零: %s\n" % ("PASS" if corners_ok else "FAIL"))
        out.write("  落点精度: 最差 %d px（tol=1）\n"
                  % max((c["err_px"] for c in summary.get("checks") or [{"err_px": 99}])))
        out.write("  耗时: %.1f s\n" % elapsed)
        out.write("  证据文件: %s\n" % ev_path)
        out.write("  换设备时: profile key 对不上会自动重跑本流程（pc/pointer_profile.py ensure）\n")
        out.write("\n== %s ==\n" % ("PASS" if ok else "FAIL"))
        print("\nJSON:", json.dumps({"ok": ok, "signature": signature,
                                     "desktop": summary.get("desktop"),
                                     "worst_err_px": max((c["err_px"] for c in
                                                          summary.get("checks") or []) or [0]),
                                     "evidence": ev_path}, ensure_ascii=False))
        return 0 if ok else 1
    finally:
        try:
            f.close()
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
