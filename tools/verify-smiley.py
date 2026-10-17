"""不靠眼睛复核笑脸：把截图里的墨迹降采样成 ASCII 图，文本模型也能"看"。

用法: python tools/verify-smiley.py <before.png> <after.png> <cx> <cy> <radius>
"""

import sys

from PIL import Image


def ink(img, box, thresh=128):
    crop = img.crop(box).convert("L")
    w, h = crop.size
    px = crop.load()
    return [[px[x, y] < thresh for x in range(w)] for y in range(h)], w, h


def ascii_art(mask, w, h, cols=76, rows=38):
    out = []
    for ry in range(rows):
        y0, y1 = ry * h // rows, (ry + 1) * h // rows
        line = []
        for rx in range(cols):
            x0, x1 = rx * w // cols, (rx + 1) * w // cols
            cnt = 0
            tot = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    tot += 1
                    if mask[y][x]:
                        cnt += 1
            line.append("#" if cnt * 4 >= max(tot, 1) else ("+" if cnt else "."))
        out.append("".join(line))
    return out


def main(argv):
    before_p, after_p, cx, cy, r = argv[1], argv[2], int(argv[3]), int(argv[4]), int(argv[5])
    box = (cx - r - 40, cy - r - 40, cx + r + 40, cy + r + 40)
    b_img = Image.open(before_p)
    a_img = Image.open(after_p)

    mb, w, h = ink(b_img, box)
    ma, _, _ = ink(a_img, box)

    n_before = sum(1 for row in mb for v in row if v)
    n_after = sum(1 for row in ma for v in row if v)
    print("box      :", box, "size", (w, h))
    print("墨迹像素 : before=%d  after=%d  (画上去的=%d)" % (n_before, n_after, n_after - n_before))

    xs = [x for y in range(h) for x in range(w) if ma[y][x]]
    ys = [y for y in range(h) for x in range(w) if ma[y][x]]
    if xs:
        print("墨迹范围 : x %d..%d (宽 %d)   y %d..%d (高 %d)"
              % (min(xs), max(xs), max(xs) - min(xs) + 1, min(ys), max(ys), max(ys) - min(ys) + 1))
        print("期望外圆 : 宽高约 %d px" % (2 * r))

    print("\n--- 墨迹 ASCII 图（# 浓 / + 淡 / . 空）---")
    for line in ascii_art(ma, w, h):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
