"""Analyze a screenshot: locate window rect, white canvas region, and content bbox.

Specialized for MS Paint style windows: the canvas is a large near-white area.
Prints JSON so the Lead/agents can consume it without vision.
"""
import json
import sys
from PIL import Image

DEFAULT = r"<repo>\pc\shot.png"


def is_white(px, tol=8):
    return px[0] >= 255 - tol and px[1] >= 255 - tol and px[2] >= 255 - tol


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    img = Image.open(path).convert("RGB")
    W, H = img.size
    x0, y0, x1, y1 = 0, 0, W, H
    if len(sys.argv) > 2:
        x0, y0, x1, y1 = map(int, sys.argv[2:6])
    px = img.load()

    # 1) largest contiguous white-ish rectangle scan: find rows/cols mostly white
    step = 2
    white_rows = []
    for y in range(y0 + 1, y1 - 1, step):
        cnt = 0
        total = 0
        for x in range(x0 + 1, x1 - 1, step):
            total += 1
            if is_white(px[x, y], 12):
                cnt += 1
        if total and cnt / total > 0.55:
            white_rows.append(y)

    # group consecutive white rows into bands
    bands = []
    cur = None
    for y in white_rows:
        if cur is None or y - cur[1] > 4 * step:
            if cur:
                bands.append(cur)
            cur = [y, y]
        else:
            cur[1] = y
    if cur:
        bands.append(cur)
    bands = [b for b in bands if b[1] - b[0] > 120]

    out = {
        "image": path,
        "size": [W, H],
        "window": [x0, y0, x1, y1],
        "white_bands": bands,
    }

    if bands:
        band = max(bands, key=lambda b: b[1] - b[0])
        by0, by1 = band
        # columns white within the band
        white_cols = []
        for x in range(x0 + 1, x1 - 1, step):
            cnt = 0
            total = 0
            for y in range(by0, by1, step):
                total += 1
                if is_white(px[x, y], 12):
                    cnt += 1
            if total and cnt / total > 0.8:
                white_cols.append(x)
        bands2 = []
        cur = None
        for x in white_cols:
            if cur is None or x - cur[1] > 4 * step:
                if cur:
                    bands2.append(cur)
                cur = [x, x]
            else:
                cur[1] = x
        if cur:
            bands2.append(cur)
        bands2 = [b for b in bands2 if b[1] - b[0] > 200]
        if bands2:
            band2 = max(bands2, key=lambda b: b[1] - b[0])
            bx0, bx1 = band2
            out["canvas_rect"] = [bx0, by0, bx1, by1]
            # 2) content bbox (non-white pixels inside canvas, ignoring near-white)
            minx, miny, maxx, maxy = None, None, None, None
            n = 0
            for y in range(by0, by1):
                for x in range(bx0, bx1):
                    p = px[x, y]
                    if not is_white(p, 20):
                        n += 1
                        if minx is None or x < minx:
                            minx = x
                        if maxx is None or x > maxx:
                            maxx = x
                        if miny is None or y < miny:
                            miny = y
                        if maxy is None or y > maxy:
                            maxy = y
            out["content_pixels"] = n
            if minx is not None:
                out["content_bbox"] = [minx, miny, maxx, maxy]
                out["canvas_size"] = [bx1 - bx0, by1 - by0]
            crop = img.crop((bx0, by0, bx1, by1))
            crop.save(path.replace(".png", "_canvas.png"))
            out["canvas_crop"] = path.replace(".png", "_canvas.png")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
