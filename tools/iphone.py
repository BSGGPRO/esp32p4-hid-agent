#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/iphone.py — 用这块 P4 板子操作手机（iPhone / 安卓）的命令行。

板子对手机是**真 USB 键鼠**（原生 USB 口 → 手机），PC 通过 CH340 串口指挥它。
移植自 ESP32 iPhone Agent by Henry Yuan（MIT，见 NOTICE）；这里换成
``docs/protocol.md`` 的 JSON-lines 协议，固件不改。

接线::

    iPhone ←─ USB-C（板子原生 USB 口）    PC ←─ CH340（板子串口）

手机侧先打开「辅助触控 / AssistiveTouch」，屏幕上才有指针。

常用::

    python tools/iphone.py --json status
    python tools/iphone.py --json calibrate            # 自动两点标定（要先能看到屏幕）
    python tools/iphone.py --json tap 0.5 0.85         # 点屏幕中间靠下
    python tools/iphone.py --json swipe 0.5 0.9 0.5 0.3
    python tools/iphone.py --json type "hello"
    python tools/iphone.py --json home

坐标是屏幕比例 0..1（原点左上），与镜像画面大小无关。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PC = os.path.join(ROOT, "pc")
for p in (PC, os.path.join(ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from phone import (  # noqa: E402
    MirrorScreen,
    Phone,
    PhoneError,
    ScreenError,
    default_calibration_path,
    find_mirror_window,
    load_video_box,
    save_video_box,
)


def _bridge(transport: str):
    from brain import Brain

    brain = Brain(transport=transport)
    return brain, brain.bridge


def _screen(args) -> MirrorScreen | None:
    rect = None
    if args.rect:
        parts = [int(float(v)) for v in str(args.rect).replace(";", ",").split(",") if v.strip()]
        if len(parts) != 4:
            raise SystemExit("--rect 要写 x1,y1,x2,y2（物理像素）")
        rect = parts
    return MirrorScreen(rect=rect, latency=getattr(args, "latency", 0.35),
                        trim=getattr(args, "trim", False))


def _out(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def cmd_status(args) -> dict:
    res: dict = {"ok": True}
    win = find_mirror_window()
    res["mirror_window"] = win
    if args.rect:
        res["mirror_window"] = {"title": "(from --rect)", "rect": [int(float(v)) for v in args.rect.split(",")]}
    brain, bridge = _bridge(args.transport)
    try:
        res["board"] = bridge.request("ping", wait=True)
        ph = Phone(bridge, screen=None, calib_file=args.calib)
        res["calibrated"] = ph.calibrated
        res["span"] = ph.span
        res["calibration_file"] = ph._file
    finally:
        brain.release()
        brain.__exit__(None, None, None)
    res["ok"] = bool(res.get("board", {}).get("ok"))
    return res


def cmd_calibrate(args) -> dict:
    screen = _screen(args)
    brain, bridge = _bridge(args.transport)
    try:
        ph = Phone(bridge, screen=screen, calib_file=args.calib)
        if args.finish:
            x, y = (float(v) for v in args.finish.split(","))
            ph.finish_calibration(x, y)
            return {"ok": True, "mode": "manual", "span": ph.span, "calibration_file": ph._file}
        ok = ph.start_calibration()
        return {"ok": ok, "mode": "auto", "span": ph.span, "calibration_file": ph._file,
                "hint": None if ok else "没在屏幕上找到指针：确认辅助触控已开、镜像在跑，"
                                        "或者手工标定：calibrate --finish x,y"}
    finally:
        brain.release()
        brain.__exit__(None, None, None)


def cmd_see(args) -> dict:
    screen = _screen(args)
    img = screen.frame()
    out_dir = os.path.join(ROOT, "inbox")
    os.makedirs(out_dir, exist_ok=True)
    path = args.out or os.path.join(out_dir, "phone-%s.png" % time.strftime("%m%d-%H%M%S"))
    img.save(path, "PNG")
    return {"ok": True, "path": os.path.abspath(path), "size": list(img.size),
            "rect": list(screen.rect()), "video_box": list(screen.last_box or ()),
            "window": screen.info or None, "letterbox_trimmed": bool(screen.trimmed)}


def cmd_gesture(args) -> dict:
    screen = _screen(args) if args.op in ("tap", "swipe", "drag") and args.aim else None
    brain, bridge = _bridge(args.transport)
    try:
        ph = Phone(bridge, screen=screen, calib_file=args.calib)
        op = args.op
        if op == "tap":
            ph.tap(args.x, args.y, aim=args.aim)
            return {"ok": True, "op": "tap", "at": [args.x, args.y], "aim": bool(args.aim)}
        if op == "swipe":
            ph.swipe(args.x1, args.y1, args.x2, args.y2, hold=not args.fling)
            return {"ok": True, "op": "swipe", "from": [args.x1, args.y1], "to": [args.x2, args.y2]}
        if op == "drag":
            pts = []
            for chunk in str(args.points).split(";"):
                if not chunk.strip():
                    continue
                x, y = (float(v) for v in chunk.split(","))
                pts.append((x, y))
            ph.drag(pts, hold=not args.fling)
            return {"ok": True, "op": "drag", "points": len(pts)}
        if op == "scroll":
            ph.scroll(args.lines)
            return {"ok": True, "op": "scroll", "lines": args.lines}
        if op == "type":
            ph.type(args.text)
            return {"ok": True, "op": "type", "chars": len(args.text)}
        if op == "key":
            ph.key(args.combo)
            return {"ok": True, "op": "key", "combo": args.combo}
        if op == "home":
            ph.go_home()
            return {"ok": True, "op": "home"}
        if op == "back":
            ph.go_back()
            return {"ok": True, "op": "back"}
        if op == "reset":
            ph.reset()
            return {"ok": True, "op": "reset"}
        raise SystemExit("未知手势 %r" % op)
    finally:
        brain.release()
        brain.__exit__(None, None, None)


def cmd_run(args) -> dict:
    """按计划跑多步（一次连接），步骤与 iphone.py 子命令同名。

    计划 JSON 示例::

        [{"op":"tap","x":0.5,"y":0.85},
         {"op":"type","text":"hello"},
         {"op":"swipe","x1":0.5,"y1":0.9,"x2":0.5,"y2":0.3},
         {"op":"wait","ms":600}]
    """
    plan = json.load(open(args.script, encoding="utf-8"))
    steps = plan.get("steps") if isinstance(plan, dict) else plan
    screen = _screen(args) if args.aim else None
    brain, bridge = _bridge(args.transport)
    done = []
    try:
        ph = Phone(bridge, screen=screen, calib_file=args.calib)
        for st in steps:
            op = st.get("op")
            if op == "tap":
                ph.tap(float(st["x"]), float(st["y"]), aim=bool(st.get("aim", args.aim)))
            elif op == "swipe":
                ph.swipe(float(st["x1"]), float(st["y1"]), float(st["x2"]), float(st["y2"]),
                         hold=not st.get("fling", False))
            elif op == "drag":
                ph.drag([(float(p[0]), float(p[1])) for p in st["points"]], hold=not st.get("fling", False))
            elif op == "scroll":
                ph.scroll(int(st.get("lines", -3)))
            elif op == "type":
                ph.type(str(st["text"]))
            elif op == "key":
                ph.key(st["combo"])
            elif op == "home":
                ph.go_home()
            elif op == "back":
                ph.go_back()
            elif op == "reset":
                ph.reset()
            elif op == "see":
                img = (_screen(args) or MirrorScreen()).frame()
                path = st.get("path") or os.path.join(ROOT, "inbox", "phone-%s.png" % time.strftime("%H%M%S"))
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                img.save(path, "PNG")
                st["_saved"] = os.path.abspath(path)
            elif op == "wait":
                time.sleep(int(st.get("ms", 300)) / 1000.0)
            else:
                raise SystemExit("未知步骤 %r" % op)
            done.append({"op": op, "ok": True, **({"saved": st["_saved"]} if st.get("_saved") else {})})
    finally:
        brain.release()
        brain.__exit__(None, None, None)
    return {"ok": True, "steps": done}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="iphone.py", description="用 P4 板子操作手机（真 USB 键鼠）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（默认就输出 JSON）")
    ap.add_argument("--transport", default="auto", choices=("auto", "tcp", "serial"))
    ap.add_argument("--calib", default=None, help="标定文件路径（默认 %%LOCALAPPDATA%%\\esp32p4-phone\\calibration.json）")
    ap.add_argument("--rect", default=None, help="手机镜像区域 x1,y1,x2,y2（物理像素）；不给就按窗口标题找")
    ap.add_argument("--latency", type=float, default=0.35, help="镜像延迟秒数（默认 0.35）")
    ap.add_argument("--trim", action="store_true",
                    help="自动裁掉留边（会随画面内容漂移，坐标不稳定；只在临时看图时用）")
    ap.add_argument("--aim", action="store_true", help="点之前看屏纠偏（慢，但准）")
    sub = ap.add_subparsers(dest="op", required=True)

    sub.add_parser("status")
    p = sub.add_parser("calibrate")
    p.add_argument("--finish", default=None, help="手工单点标定：指针当前所在的屏幕比例 x,y")
    p = sub.add_parser("see")
    p.add_argument("--out", default=None, help="PNG 存到哪（默认 inbox/phone-<时间>.png）")

    p = sub.add_parser("tap")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p = sub.add_parser("swipe")
    for k in ("x1", "y1", "x2", "y2"):
        p.add_argument(k, type=float)
    p.add_argument("--fling", action="store_true", help="末尾不停（系统手势用）")
    p = sub.add_parser("drag")
    p.add_argument("points", help='"x1,y1;x2,y2;..."')
    p.add_argument("--fling", action="store_true")
    p = sub.add_parser("scroll")
    p.add_argument("lines", type=int, help="正=上滚（HID 语义）")
    p = sub.add_parser("type")
    p.add_argument("text")
    p = sub.add_parser("key")
    p.add_argument("combo")
    sub.add_parser("home")
    sub.add_parser("back")
    sub.add_parser("reset")
    p = sub.add_parser("box")
    p.add_argument("--set", dest="box_set", default=None,
                   help='视频区在窗口客户区里的偏移 "dx1,dy1,dx2,dy2"（量一次存下来）')
    p = sub.add_parser("run")
    p.add_argument("script")
    return ap


def cmd_box(args) -> dict:
    """看/设「视频区在窗口客户区里的偏移」——量一次，之后每帧只取这一块。"""
    if not getattr(args, "box_set", None):
        box = load_video_box(getattr(args, "calib", None))
        return {"ok": True, "video_box": list(box) if box else None,
                "file": getattr(args, "calib", None) or default_calibration_path()}
    try:
        box = tuple(int(float(v)) for v in str(args.box_set).replace(";", ",").split(","))
    except ValueError:
        return {"ok": False, "err": "bad_arg", "detail": "要四个整数：dx1,dy1,dx2,dy2"}
    if len(box) != 4 or box[2] - box[0] < 50 or box[3] - box[1] < 50:
        return {"ok": False, "err": "bad_arg", "detail": "区域太小：%s" % (box,)}
    path = save_video_box(box, getattr(args, "calib", None))
    return {"ok": True, "video_box": list(box), "file": path}


HANDLERS = {
    "status": cmd_status, "calibrate": cmd_calibrate, "see": cmd_see, "box": cmd_box,
    "tap": cmd_gesture, "swipe": cmd_gesture, "drag": cmd_gesture, "scroll": cmd_gesture,
    "type": cmd_gesture, "key": cmd_gesture, "home": cmd_gesture, "back": cmd_gesture,
    "reset": cmd_gesture, "run": cmd_run,
}

#: 给 ``run()``（MCP 等进程内调用）用的默认参数，缺哪个补哪个
_DEFAULTS = dict(json=True, transport="auto", calib=None, rect=None, latency=0.35, aim=False, trim=False,
                 fling=False, finish=None, points=None, script=None, lines=0, text="", combo="",
                 out=None, box_set=None, x=0.0, y=0.0, x1=0.0, y1=0.0, x2=0.0, y2=0.0)


def run(op: str, **params) -> dict:
    """进程内跑一个操作（MCP 工具用），返回结果 dict；参数名与命令行一致。"""
    if op not in HANDLERS:
        raise SystemExit("未知操作 %r，可用：%s" % (op, ", ".join(HANDLERS)))
    args = argparse.Namespace(**{**_DEFAULTS, **params, "op": op})
    return HANDLERS[op](args)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        res = HANDLERS[args.op](args)
    except (PhoneError, ScreenError) as e:
        _out({"ok": False, "err": "phone", "detail": str(e)})
        return 2
    except Exception as e:  # noqa: BLE001 — 工具面永远回 JSON
        _out({"ok": False, "err": type(e).__name__, "detail": str(e)})
        return 1
    _out(res)
    return 0 if res.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
