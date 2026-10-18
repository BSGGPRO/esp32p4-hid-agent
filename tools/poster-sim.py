"""Rasterise a poster plan exactly like a round brush would, so parameters can be tuned
without drawing on the board.

usage: python tools/poster-sim.py --plan x-plan.json --out x-sim.png
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
from PIL import Image, ImageDraw

PAL = {}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    cw, ch = plan["canvas"]
    img = Image.new("RGB", (cw, ch), (255, 255, 255))
    d = ImageDraw.Draw(img)
    n = 0
    for g in plan["groups"]:
        col = tuple(int(v) for v in g["rgb"])
        w = max(1, int(g["brush"]))
        for s in g["strokes"]:
            pts = [(float(p[0]), float(p[1])) for p in s]
            if len(pts) == 1:
                d.ellipse([pts[0][0] - w / 2, pts[0][1] - w / 2,
                           pts[0][0] + w / 2, pts[0][1] + w / 2], fill=col)
            else:
                d.line(pts, fill=col, width=w, joint="curve")
                for p in (pts[0], pts[-1]):
                    d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=col)
            n += 1
    out = a.out or (a.plan.replace("-plan.json", "") + "-sim.png")
    img.save(out, "PNG")
    arr = np.asarray(img, dtype=np.float32)
    g = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    print(json.dumps({"ok": True, "sim": out, "strokes": n,
                      "ink": round(float((g < 240).mean()), 4)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
