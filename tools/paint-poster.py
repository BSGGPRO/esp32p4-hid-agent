#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/paint-poster.py — 按 poster-plan.py 的计划，用 ESP32-P4 的 HID 鼠标在画图里"刷"出彩色色块画。

为什么这么做（而不是油漆桶）：
* 油漆桶要先把所有色块描边封闭，1px 的缝就漏一整片，风险太大；
* 大画笔蛇形填涂不需要封闭边界，颜色分块之间只是互相压一点边，像真拿笔画。

颜色只能用画图自带调色板（自定义 RGB 的「编辑颜色」是 WinUI 模态窗，UIA 一碰就卡死，
实测把整个画图卡住），所以计划里的颜色都是调色板 20 色之一；黑色是画图启动时的默认
前景色，所以黑色组必须**在换任何颜色之前**先画。

用法：
    python tools/paint-poster.py --plan inbox/xxx-poster-plan.json --transport tcp
    python tools/paint-poster.py --plan ... --dry-run      # 只打印分组和耗时估计
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pc"))

_spec = importlib.util.spec_from_file_location("draw_image", os.path.join(HERE, "draw-image.py"))
di = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(di)


def ps1(*args, timeout=180):
    r = subprocess.run(["pwsh", "-NoProfile", "-File", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return (r.stdout or "").strip()


def set_color(row: int, col: int) -> dict:
    out = ps1(os.path.join(HERE, "paint-color.ps1"), "-Row", str(row), "-Col", str(col))
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return {"ok": False, "raw": out}


def set_brush(size: int, kind: str = "Brush") -> dict:
    out = ps1(os.path.join(HERE, "paint-brush.ps1"), "-Size", str(size), "-Brush", kind)
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return {"ok": False, "raw": out}


def start_fresh_paint(cw: int, ch: int) -> int:
    """起一个全新画图（前景色=黑），画布尺寸 = 计划尺寸，窗口放到能看全的高度。"""
    from PIL import Image  # 延迟导入，dry-run 不需要

    blank = os.path.join(HERE, "..", "inbox", "poster-canvas-%dx%d.png" % (cw, ch))
    blank = os.path.abspath(blank)
    os.makedirs(os.path.dirname(blank), exist_ok=True)
    Image.new("RGB", (cw, ch), (255, 255, 255)).save(blank)
    subprocess.run(["taskkill", "/F", "/IM", "mspaint.exe"], capture_output=True)
    time.sleep(1.2)
    subprocess.Popen(["mspaint.exe", blank], close_fds=True)
    time.sleep(4.0)
    dr = di.dr
    paint = dr.find_paint()
    hwnd = int(paint["hwnd"])
    win_w = min(cw + 70, 900)
    win_h = min(ch + 160, 1390)
    dr.set_rect(hwnd, 1490, 0, win_w, win_h, activate=True)
    time.sleep(0.6)
    dr.focus_hwnd(hwnd)
    return hwnd


def ensure_focus(dr, hwnd: int, tries: int = 8) -> bool:
    """切前台并确认（切刚改完颜色/笔径时，前台常被 PowerShell 抢一下）。"""
    for _ in range(tries):
        dr.focus_hwnd(hwnd)
        if int(dr.foreground() or 0) == int(hwnd):
            return True
        time.sleep(0.25)
    return False


def win_size(dr) -> tuple[int, int]:
    w = dr.find_paint() or {}
    r = w.get("rect") or (0, 0, 900, 800)
    return int(r[2] - r[0]), int(r[3] - r[1])


def draw_chunked(b, dr, hwnd, strokes_win, args, table, log, chunk: int = 40) -> tuple[int, str]:
    """分段画：每段前重切前台。前台被别的窗口抢了就重试，而不是整张作废。"""
    done = 0
    fails = 0
    while done < len(strokes_win):
        if not ensure_focus(dr, hwnd, tries=6):
            fails += 1
            if fails >= 3:
                return done, "切不回画图前台"
            time.sleep(0.5)
            continue
        part = strokes_win[done:done + chunk]
        res = di.run_strokes(b, part, args, table, hwnd, log)
        moved = int(res.get("strokes_done", 0))
        done += moved
        if res.get("aborted"):
            if "ESC" in (res.get("reason") or ""):
                return done, (res.get("reason") or "用户按了 ESC")
            fails += 1
            if moved == 0:
                if fails >= 3:
                    return done, (res.get("reason") or "画不进去")
                time.sleep(0.4)
            else:
                fails = 0
    return done, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按计划在画图里刷色块画")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--transport", default="tcp")
    ap.add_argument("--brush-kind", default="Brush")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--progress", default=None)
    a = ap.parse_args(argv)

    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    cw, ch = plan["canvas"]
    groups = plan["groups"]
    strokes_total = sum(len(g["strokes"]) for g in groups)
    verts_total = sum(len(s) for g in groups for s in g["strokes"])

    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "canvas": [cw, ch], "groups": len(groups),
                          "strokes": strokes_total, "vertices": verts_total,
                          "est_seconds": round(verts_total * 0.013 + strokes_total * 0.06, 1),
                          "plan": [(g["color"], g["brush"], len(g["strokes"])) for g in groups]},
                         ensure_ascii=False))
        return 0

    if di.dr is None:
        print(json.dumps({'ok': False, 'err': 'draw-rage.py 加载失败'}, ensure_ascii=False))
        return 2
    from brain import Brain

    dr = di.dr
    hwnd = start_fresh_paint(cw, ch)
    ps1(os.path.join(HERE, "paint-tool.ps1"), "-Zoom", "100")
    dr.focus_hwnd(hwnd)
    di.clear_canvas()
    time.sleep(0.4)
    # 清空会把工具切到「选择」，画笔必须在清空之后设
    box, hwnd, _ = di.detect_canvas_auto(SimpleNamespace(
        no_launch_paint=True, paint_canvas="", paint_size="", clear_canvas=False))
    if not box:
        print(json.dumps({"ok": False, "err": "找不到画布"}, ensure_ascii=False))
        return 3
    # 画布没露全（窗口太矮）就长高窗口再检测，坐标必须 1:1 对得上
    for _ in range(4):
        vh, vw = box[3] - box[1], box[2] - box[0]
        if vh >= ch - 2 and vw >= cw - 2:
            break
        ww, wh = win_size(dr)
        dr.set_rect(hwnd, 1490, 0, min(ww + (cw - vw) + 20, 900),
                    min(wh + (ch - vh) + 20, 1390), activate=True)
        time.sleep(0.6)
        ensure_focus(dr, hwnd)
        box, hwnd, _ = di.detect_canvas_auto(SimpleNamespace(
            no_launch_paint=True, paint_canvas="", paint_size="", clear_canvas=False))
        if not box:
            break

    # 黑色组必须最先画（画图启动时前景色就是黑）
    blacks = [g for g in groups if g["color"] == "black"]
    others = [g for g in groups if g["color"] != "black"]
    ordered = blacks + others

    args = SimpleNamespace(stop_key="esc", anchor_every=8, interfere_px=60, progress=a.progress)
    table = di._gain_table()
    log: list = []
    summary = {"ok": True, "canvas_box": box, "groups": [], "started": time.strftime("%H:%M:%S")}
    done_strokes = 0
    t0 = time.time()
    with Brain(transport=a.transport) as b:
        b.release()
        cur_color = "black"          # 启动默认
        cur_brush = None
        for g in ordered:
            if g["color"] != cur_color:
                r = set_color(g["row"], g["col"])
                summary.setdefault("colors", []).append({"want": g["color"], "resp": r})
                cur_color = g["color"]
                if not ensure_focus(dr, hwnd):
                    summary["aborted"] = "切不回画图前台（颜色切换后）"
                    break
                time.sleep(0.2)
            if g["brush"] != cur_brush:
                summary.setdefault("brushes", []).append(set_brush(int(g["brush"]), a.brush_kind))
                cur_brush = g["brush"]
                if not ensure_focus(dr, hwnd):
                    summary["aborted"] = "切不回画图前台（笔径切换后）"
                    break
                time.sleep(0.2)
            strokes_win = [[[p[0] + box[0], p[1] + box[1]] for p in s] for s in g["strokes"]]
            done, why = draw_chunked(b, dr, hwnd, strokes_win, args, table, log)
            done_strokes += done
            summary["groups"].append({"color": g["color"], "brush": g["brush"],
                                      "strokes": len(g["strokes"]), "done": done})
            if why:
                summary["aborted"] = why
                break
        b.release()
    summary["strokes_done"] = done_strokes
    summary["seconds"] = round(time.time() - t0, 1)
    summary["events"] = log[:20]

    try:
        import eyes
        summary["after_shot"] = eyes.capture(
            os.path.join(di.INBOX, "poster-after-%s.png" % time.strftime("%Y%m%d-%H%M%S"))).path
    except Exception as e:  # noqa: BLE001
        summary["shot_err"] = str(e)

    out = os.path.splitext(a.plan)[0] + "-draw.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    summary["summary_path"] = out
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not summary.get("aborted") else 5


if __name__ == "__main__":
    sys.exit(main())
