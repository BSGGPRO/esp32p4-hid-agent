# -*- coding: utf-8 -*-
"""pc/eyes.py — 全屏截屏（Pillow ImageGrab）-> PNG。

用途: e2e-demo / mouse 效果验证 / 给 lead 留证据。
依赖: 仅 Pillow + 标准库（本机 Pillow 12.3.0）。

关键实现点:
    * **DPI 感知**: 进程不是 DPI-aware 时，Windows 会把截图缩放到逻辑分辨率
      （2048x1152 屏可能拿到 1365x768 之类），所以抓屏前先 SetProcessDpiAwareness
      (PER_MONITOR_AWARE)，失败再退 SetProcessDPIAware。
    * ``all_screens=True`` -> 整个虚拟桌面（多显示器全部包含），
      ``--primary`` -> 只抓主屏。
    * 返回值 ``Shot(width, height, path, size)``：尺寸、路径、**文件字节数**，
      可直接解包 ``w, h, path, nbytes = capture("a.png")``。

CLI::

    python eyes.py out.png                 # 全屏（虚拟桌面）
    python eyes.py out.png --primary       # 只主屏
    python eyes.py out.png --region 0,0,800,600
    python eyes.py --info                  # 只打印屏幕指标，不截屏
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from collections import namedtuple

from PIL import Image, ImageGrab

#: (width, height, path, size_bytes) —— 顺序固定，便于解包
Shot = namedtuple("Shot", "width height path size")

_default_path_counter = {"n": 0}


# --------------------------------------------------------------------------
# DPI / 屏幕指标
# --------------------------------------------------------------------------
_dpi_state: dict = {"mode": "unset"}


def enable_dpi_awareness(force: bool = False) -> str:
    """让进程 DPI-aware，保证截图尺寸 == 物理屏幕尺寸。

    返回生效的模式字符串；已在别处设置过时返回 ``"already-set"``。
    """
    if _dpi_state["mode"] != "unset" and not force:
        return _dpi_state["mode"]
    if sys.platform != "win32":
        _dpi_state["mode"] = "non-windows"
        return _dpi_state["mode"]

    mode = "none"
    # Win8.1+: 2 = PROCESS_PER_MONITOR_DPI_AWARE
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        hr = shcore.SetProcessDpiAwareness(2)
        if hr == 0:
            mode = "per-monitor-aware"
        elif hr in (0x80070005, -2147024891):  # E_ACCESSDENIED: 已经设置过
            mode = "already-set"
    except Exception:
        mode = "none"
    if mode == "none":
        try:
            if ctypes.windll.user32.SetProcessDPIAware():
                mode = "system-aware"
            else:
                mode = "already-set"
        except Exception:
            mode = "none"
    _dpi_state["mode"] = mode
    return mode


def screen_metrics() -> dict:
    """主屏 / 虚拟桌面像素尺寸（GetSystemMetrics）。"""
    if sys.platform != "win32":
        return {"platform": sys.platform}
    u32 = ctypes.windll.user32
    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return {
        "platform": "win32",
        "primary": (u32.GetSystemMetrics(SM_CXSCREEN), u32.GetSystemMetrics(SM_CYSCREEN)),
        "virtual": (u32.GetSystemMetrics(SM_CXVIRTUALSCREEN), u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        "virtual_origin": (u32.GetSystemMetrics(SM_XVIRTUALSCREEN), u32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        "dpi_mode": _dpi_state["mode"],
    }


def parse_region(spec: str) -> tuple[int, int, int, int]:
    """``"x,y,w,h"`` -> ``(x, y, w, h)``。也接受 ``"x,y,x2,y2"`` 之外的写法吗？不，固定 w,h。"""
    parts = [p.strip() for p in str(spec).replace(";", ",").split(",") if p.strip() != ""]
    if len(parts) != 4:
        raise ValueError(f"region 需要 4 个整数 x,y,w,h，收到: {spec!r}")
    try:
        x, y, w, h = (int(float(p)) for p in parts)
    except ValueError as exc:
        raise ValueError(f"region 必须是整数 x,y,w,h，收到: {spec!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"region 的 w/h 必须为正，收到: {spec!r}")
    return x, y, w, h


def _grab(all_screens: bool) -> Image.Image:
    try:
        return ImageGrab.grab(all_screens=all_screens)
    except TypeError:  # 老 Pillow 不支持 all_screens
        return ImageGrab.grab()


def capture_report(path: str, *, all_screens: bool = True, region=None,
                   dpi_aware: bool = True) -> tuple[Shot, dict]:
    """截屏 -> PNG；返回 ``(Shot, 诊断报告)``。"""
    if dpi_aware:
        enable_dpi_awareness()
    metrics = screen_metrics()
    img = _grab(all_screens)
    grabbed_size = img.size

    region_box = None
    if region is not None:
        if isinstance(region, str):
            region = parse_region(region)
        x, y, w, h = region
        region_box = (x, y, x + w, y + h)
        img = img.crop(region_box)

    out_path = os.path.abspath(path)
    parent = os.path.dirname(out_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")
    img.save(out_path, "PNG")
    size = os.path.getsize(out_path)

    shot = Shot(img.size[0], img.size[1], out_path, size)
    report = {
        "mode": "fullscreen" if region_box is None else "region",
        "all_screens": bool(all_screens),
        "grabbed_size": grabbed_size,
        "region": list(region) if region is not None else None,
        "primary": metrics.get("primary"),
        "virtual": metrics.get("virtual"),
        "dpi_mode": metrics.get("dpi_mode"),
        "matches_primary": tuple(img.size) == tuple(metrics.get("primary") or ()),
        "matches_virtual": tuple(img.size) == tuple(metrics.get("virtual") or ()),
        "size_bytes": size,
        "path": out_path,
    }
    return shot, report


def capture(path: str, *, all_screens: bool = True, region=None,
            dpi_aware: bool = True) -> Shot:
    """截屏存 PNG，返回 ``Shot(width, height, path, size_bytes)``。"""
    shot, _ = capture_report(path, all_screens=all_screens, region=region, dpi_aware=dpi_aware)
    return shot


def capture_default_path(outdir: str = ".") -> str:
    """生成不覆盖的默认文件名（e2e/screenshot 未给路径时用）。"""
    _default_path_counter["n"] += 1
    base = os.path.join(outdir, f"shot_{_default_path_counter['n']:03d}.png")
    return base


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1048576:.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eyes.py",
        description="全屏截图存 PNG / full-screen screenshot to PNG (Pillow ImageGrab).",
        epilog=(
            "示例 / examples:\n"
            "  python eyes.py out.png\n"
            "  python eyes.py out.png --primary\n"
            "  python eyes.py out.png --region 0,0,800,600\n"
            "  python eyes.py --info\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("out", nargs="?", help="输出 PNG 路径 / output PNG path")
    p.add_argument("--primary", action="store_true",
                   help="只抓主屏（默认抓整个虚拟桌面）/ capture primary screen only")
    p.add_argument("--region", metavar="x,y,w,h",
                   help="裁剪区域 / crop region before saving")
    p.add_argument("--no-dpi-fix", action="store_true",
                   help="不要设置 DPI 感知 / do not set DPI awareness")
    p.add_argument("--info", action="store_true", help="只打印屏幕指标 / print metrics only")
    p.add_argument("--json", action="store_true", help="JSON 输出 / JSON output")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    import json

    if args.info:
        if not args.no_dpi_fix:
            enable_dpi_awareness()
        info = screen_metrics()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"主屏/primary      : {info.get('primary')}")
            print(f"虚拟桌面/virtual  : {info.get('virtual')} @ {info.get('virtual_origin')}")
            print(f"DPI 模式          : {info.get('dpi_mode')}")
        return 0

    if not args.out:
        build_parser().error("需要输出路径，例如: python eyes.py out.png  (或用 --info)")
    try:
        shot, report = capture_report(
            args.out,
            all_screens=not args.primary,
            region=args.region,
            dpi_aware=not args.no_dpi_fix,
        )
    except Exception as exc:  # 截屏失败要给清晰信息而不是 traceback
        print(f"[EYES-ERROR] 截图失败 / screenshot failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"width": shot.width, "height": shot.height, "path": shot.path,
                          "size": shot.size, **report}, ensure_ascii=False, indent=2))
    else:
        print(f"截图已保存 / screenshot saved: {shot.path}")
        print(f"  尺寸/size  : {shot.width} x {shot.height}  (grabbed {report['grabbed_size'][0]}x{report['grabbed_size'][1]})")
        print(f"  文件/bytes : {shot.size} B ({human_size(shot.size)})")
        print(f"  DPI 模式   : {report['dpi_mode']}")
        if report["mode"] == "fullscreen":
            match = "主屏" if report["matches_primary"] else ("虚拟桌面" if report["matches_virtual"] else "未匹配系统指标")
            print(f"  尺寸校验   : {match} / matches {('primary' if report['matches_primary'] else ('virtual' if report['matches_virtual'] else 'neither'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())