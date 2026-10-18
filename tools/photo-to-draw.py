#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/photo-to-draw.py — 照片一键变画：选照片 -> 规划 -> （可选）真的用板子画出来。

写给「我把照片导入进去，你就能给我画出来」用。默认只规划+出预览（不动鼠标），
加 --draw 才真的用板子鼠标在画图里画。

诚实说明：鼠标+1px 铅笔笔画不可能「无损」还原照片（那是打印机/喷绘的事）。
本工具做的是**铅笔素描/铜版画**级别的还原：多层等值线保留明暗层次，
标准质量下与原图的灰度相关约 0.65、墨迹重合约 0.70；时间换质量。

用法：
    python tools\\photo-to-draw.py 照片.jpg                      # 只预览（安全）
    python tools\\photo-to-draw.py 照片.jpg --quality good        # 好一点（更慢）
    python tools\\photo-to-draw.py 照片.jpg --quality hd --draw   # 真画（会动鼠标）
    python tools\\photo-to-draw.py 照片.jpg --quality quick --draw --canvas 自动

质量档位（相对画布大小；画布越大越慢）：
    quick  ~10-20 秒/万像素：3 层等值线，笔画少，适合小图标/快速看效果
    good   默认：5 层等值线，明暗层次明显
    hd     最慢：7 层，细节最多（可能好几分钟，先看 est_seconds）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_WORKSPACE = os.path.dirname(_ROOT)
INBOX = os.path.join(_WORKSPACE, "inbox")
DRAW = os.path.join(_HERE, "draw-image.py")

PRESETS = {
    "quick": {"levels": "95,145,200", "simplify": 2.4, "min_len": 10},
    "good": {"levels": "90,125,160,200", "simplify": 2.2, "min_len": 8},
    "hd": {"levels": "70,95,120,145,170,195,220", "simplify": 1.6, "min_len": 7},
}


def _utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文/符号输出会 UnicodeEncodeError；统一改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="照片 -> 画（默认只预览，--draw 才落笔）")
    ap.add_argument("photo", help="照片路径（png/jpg/webp/bmp/tif…）")
    ap.add_argument("--quality", default="good", choices=sorted(PRESETS), help="质量档位")
    ap.add_argument("--draw", action="store_true", help="真的用板子画（会动鼠标）")
    ap.add_argument("--canvas", default=None, help="'x1,y1,x2,y2' 或 '宽x高'；不给就在 --draw 时自动检测画图")
    ap.add_argument("--out", default=None, help="输出前缀")
    ap.add_argument("--transport", default="tcp", choices=["tcp", "uart"])
    ap.add_argument("--progress", type=int, default=0, help="每 N 笔打印进度")
    ap.add_argument("--max-strokes", type=int, default=0, help="最多多少笔（0=不限）")
    ap.add_argument("--paint-canvas", default="1040x760",
                    help="自动打开画图时用的白画布尺寸（决定能画多大、多像）")
    ap.add_argument("--paint-size", default="",
                    help="自动打开画图时的窗口大小（默认按画布自动算）")
    ap.add_argument("--keep-canvas", action="store_true", help="不要清空画图里已有的内容")
    ap.add_argument("--no-launch-paint", action="store_true", help="不要自动打开画图")
    ap.add_argument("--no-invert", action="store_true",
                    help="深底照片别反色（默认深底图会反色成负片效果）")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.photo):
        print(json.dumps({"ok": False, "err": "照片不存在: %s" % a.photo}, ensure_ascii=False))
        return 1
    os.makedirs(INBOX, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = a.out or os.path.join(INBOX, "photo-%s" % ts)
    pres = PRESETS[a.quality]

    cmd = [sys.executable, DRAW, a.photo, "--mode", "engrave",
           "--levels", pres["levels"],
           "--simplify", str(pres["simplify"]), "--min-len", str(pres["min_len"]),
           "--preview-width", "4", "--out", base, "--transport", a.transport]
    if a.canvas:
        cmd += ["--canvas", a.canvas]
    if a.no_invert:
        cmd += ["--no-invert"]
    if a.max_strokes:
        cmd += ["--max-strokes", str(a.max_strokes)]
    if a.draw:
        cmd += ["--draw", "--paint-canvas", a.paint_canvas]
        if a.paint_size:
            cmd += ["--paint-size", a.paint_size]
        if not a.keep_canvas:
            cmd += ["--clear-canvas"]
        if a.no_launch_paint:
            cmd += ["--no-launch-paint"]
        if a.progress:
            cmd += ["--progress", str(a.progress)]

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    out = (proc.stdout or "").strip().splitlines()
    res = {}
    for line in out:
        line = line.strip()
        if line.startswith("{"):
            try:
                res = json.loads(line)
            except json.JSONDecodeError:
                pass
    if not res:
        print(json.dumps({"ok": False, "err": "规划失败", "stderr": (proc.stderr or "")[-800:],
                          "stdout": (proc.stdout or "")[-800:]}, ensure_ascii=False))
        return 2
    res["quality"] = a.quality
    res["photo"] = a.photo
    res["seconds"] = round(time.time() - t0, 1)
    res["cmd"] = " ".join(cmd)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
