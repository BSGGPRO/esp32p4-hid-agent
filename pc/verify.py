# -*- coding: utf-8 -*-
"""pc/verify.py — 两张 PNG 的像素差分（鼠标/HID 效果验证）。

输出: 变化像素数、总像素数、变化比例、是否判定「有变化」、变化包围盒。
支持 ``--region x,y,w,h`` 只比较局部区域（例如只看某个窗口）。

实现: Pillow 的 ImageChops.difference 做逐通道差，再对每个通道按阈值二值化
（>threshold -> 255），用 ImageChops.lighter 合并成「任一通道有差异」的掩码，
最后 mask.histogram()[255] 数变化像素 —— 全部在 C 层完成，
2048x1152（236 万像素）也在几十毫秒量级，不需要 numpy。

判定规则:
    has_change = (changed_pixels >= min_pixels and ratio >= min_ratio) or size_mismatch

CLI::

    python verify.py a.png b.png
    python verify.py a.png b.png --region 100,100,600,400 --json
    python verify.py a.png b.png --expect change      # 期望有变化，否则 exit 1
    python verify.py a.png b.png --expect nochange    # 期望无变化，否则 exit 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import namedtuple

from PIL import Image, ImageChops

#: 差分结果
DiffResult = namedtuple(
    "DiffResult",
    "changed_pixels total_pixels ratio has_change bbox size_a size_b "
    "size_mismatch threshold min_ratio min_pixels region",
)


class VerifyError(RuntimeError):
    """差分无法进行（文件缺失/不是图片）。"""


def _parse_region(spec) -> tuple[int, int, int, int] | None:
    """``"x,y,w,h"`` 或 4-元组 -> ``(x, y, w, h)``。"""
    if spec is None:
        return None
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.replace(";", ",").split(",") if p.strip() != ""]
        if len(parts) != 4:
            raise VerifyError(f"--region 需要 4 个整数 x,y,w,h，收到: {spec!r}")
        x, y, w, h = (int(float(p)) for p in parts)
    else:
        x, y, w, h = (int(v) for v in spec)
    if w <= 0 or h <= 0:
        raise VerifyError(f"--region 的 w/h 必须为正，收到: {spec!r}")
    return x, y, w, h


def _open_rgb(path: str) -> Image.Image:
    if not os.path.isfile(path):
        raise VerifyError(f"图片不存在 / image not found: {path}")
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:  # Pillow 的各种解码错误统一成 VerifyError
        raise VerifyError(f"无法读取图片 / cannot read image {path}: {exc}") from exc
    return img.convert("RGB")


def diff_images(path_a: str, path_b: str, *, region=None, threshold: int = 0,
                min_ratio: float = 0.0, min_pixels: int = 1) -> DiffResult:
    """比较两张图，返回 :class:`DiffResult`。

    ``threshold``: 每通道差异 > threshold 才算变化（0 = 任何差异都算）。
    ``min_ratio``: 变化比例下限（0.0 = 只要有一个像素变化就算有变化）。
    ``min_pixels``: 变化像素数下限。
    """
    a = _open_rgb(path_a)
    b = _open_rgb(path_b)
    size_a, size_b = a.size, b.size

    box = _parse_region(region)
    if box is not None:
        x, y, w, h = box
        a = a.crop((x, y, x + w, y + h))
        b = b.crop((x, y, x + w, y + h))

    size_mismatch = a.size != b.size
    if size_mismatch:
        # 取公共区域继续比较，但仍然把 size_mismatch 记入结论
        w = min(a.size[0], b.size[0])
        h = min(a.size[1], b.size[1])
        a = a.crop((0, 0, w, h))
        b = b.crop((0, 0, w, h))

    total = a.size[0] * a.size[1]
    if total == 0:
        raise VerifyError("比较区域为空 / empty comparison area")

    diff = ImageChops.difference(a, b)
    mask = None
    for ch in diff.split():
        m = ch.point(lambda v: 255 if v > threshold else 0, mode="L")
        mask = m if mask is None else ImageChops.lighter(mask, m)
    # mask 的取值只有 0 / 255 -> hist[255] 即变化像素数（C 层统计）
    changed = int(mask.histogram()[255])
    bbox = mask.getbbox()
    ratio = changed / total if total else 0.0
    has_change = bool(
        (changed >= min_pixels and ratio >= min_ratio) or size_mismatch
    )
    return DiffResult(
        changed_pixels=changed,
        total_pixels=total,
        ratio=ratio,
        has_change=has_change,
        bbox=bbox,
        size_a=size_a,
        size_b=size_b,
        size_mismatch=size_mismatch,
        threshold=threshold,
        min_ratio=min_ratio,
        min_pixels=min_pixels,
        region=box,
    )


def diff_summary(res: DiffResult, path_a: str = "", path_b: str = "") -> dict:
    return {
        "a": path_a,
        "b": path_b,
        "size_a": list(res.size_a),
        "size_b": list(res.size_b),
        "size_mismatch": res.size_mismatch,
        "region": list(res.region) if res.region else None,
        "threshold": res.threshold,
        "min_ratio": res.min_ratio,
        "min_pixels": res.min_pixels,
        "changed_pixels": res.changed_pixels,
        "total_pixels": res.total_pixels,
        "ratio": res.ratio,
        "ratio_percent": res.ratio * 100.0,
        "has_change": res.has_change,
        "bbox": list(res.bbox) if res.bbox else None,
    }


def format_report(res: DiffResult, path_a: str = "", path_b: str = "") -> str:
    lines = []
    if path_a:
        lines.append(f"[A] {path_a}  {res.size_a[0]}x{res.size_a[1]}")
    if path_b:
        lines.append(f"[B] {path_b}  {res.size_b[0]}x{res.size_b[1]}")
    if res.size_mismatch:
        lines.append("尺寸不一致/size mismatch: 已按公共区域比较 (compared on common area)")
    if res.region:
        x, y, w, h = res.region
        lines.append(f"比较区域/region : x={x} y={y} w={w} h={h}")
    else:
        lines.append("比较区域/region : 全图 (full image)")
    lines.append(f"变化像素/changed: {res.changed_pixels} / {res.total_pixels}")
    lines.append(f"变化比例/ratio  : {res.ratio * 100:.4f}%")
    lines.append(f"差异阈值/thresh : >{res.threshold} (每通道)")
    lines.append(f"包围盒/bbox     : {res.bbox if res.bbox else '无 (none)'}")
    verdict = "有变化 / CHANGE DETECTED" if res.has_change else "无变化 / NO CHANGE"
    lines.append(f"判定/verdict    : {verdict}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify.py",
        description="比较两张 PNG 的像素差异 / pixel-diff two PNG screenshots.",
        epilog=(
            "示例 / examples:\n"
            "  python verify.py before.png after.png\n"
            "  python verify.py before.png after.png --region 0,0,800,600 --json\n"
            "  python verify.py before.png after.png --expect change\n"
            "退出码/exit: 0=比较成功, 1=--expect 未满足, 2=参数/文件错误\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("a", help="图片 A（前）/ first image")
    p.add_argument("b", help="图片 B（后）/ second image")
    p.add_argument("--region", metavar="x,y,w,h", help="只比较该区域 / compare region only")
    p.add_argument("--threshold", type=int, default=0,
                   help="每通道差异阈值，>threshold 才算变化（默认 0）/ per-channel threshold")
    p.add_argument("--min-ratio", type=float, default=0.0,
                   help="变化比例下限，如 0.001 = 0.1%%（默认 0.0）/ minimum changed ratio")
    p.add_argument("--min-pixels", type=int, default=1,
                   help="变化像素数下限（默认 1）/ minimum changed pixel count")
    p.add_argument("--expect", choices=["change", "nochange"], default=None,
                   help="断言期望：未满足时 exit 1 / assert expectation")
    p.add_argument("--json", action="store_true", help="JSON 输出 / JSON output")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        res = diff_images(args.a, args.b, region=args.region, threshold=args.threshold,
                          min_ratio=args.min_ratio, min_pixels=args.min_pixels)
    except VerifyError as exc:
        print(f"[VERIFY-ERROR] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        print(f"[VERIFY-ERROR] 差分失败 / diff failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(diff_summary(res, args.a, args.b), ensure_ascii=False, indent=2))
    else:
        print(format_report(res, args.a, args.b))

    if args.expect == "change" and not res.has_change:
        print("[VERIFY-ERROR] 期望有变化但未检测到 / expected change, got none", file=sys.stderr)
        return 1
    if args.expect == "nochange" and res.has_change:
        print("[VERIFY-ERROR] 期望无变化但检测到变化 / expected no change, got change", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())