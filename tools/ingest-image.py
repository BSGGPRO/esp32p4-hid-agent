#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/ingest-image.py — 把「任意来源 / 任意格式」的图片统一成我能看的 PNG。

为什么需要它：模型侧看图只吃 PNG/JPEG/WebP/GIF 的文件，用户手上的图可能是
剪贴板里的截图、手机拍的 HEIC、设计稿 SVG、扫描的 TIFF……本工具负责把任何
入口统一成一张规规矩矩的 PNG（转正、透明底垫白、超大图缩到可读尺寸）。

用法：
    python tools/ingest-image.py --clipboard            # 剪贴板里的图（截图 / 复制网页图 / 复制的图片文件）
    python tools/ingest-image.py "D:\\pic\\a.heic"       # 任意格式（png/jpg/webp/bmp/gif/tiff/ico/svg/...）
    python tools/ingest-image.py --latest               # 取 inbox 里最新的一张
    python tools/ingest-image.py PATH --thumb 1400      # 额外输出缩略图（≤1400 px，便于看图）

输出：统一存到 <workspace>\\inbox\\，并打印一行 JSON（path/size/format/thumb）。
退出码：0 成功；2 文件不存在；3 无法解码（会说明怎么办）。
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

try:
    from PIL import Image, ImageOps, ImageGrab
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"ok": False, "err": "Pillow 不可用: %r" % (exc,)}, ensure_ascii=False))
    sys.exit(3)

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# Pillow 直接能开的扩展名；SVG/PDF/HEIC 需要额外处理
PIL_EXT = {".png", ".jpg", ".jpeg", ".jpe", ".webp", ".bmp", ".gif", ".tif", ".tiff",
           ".ico", ".ppm", ".pgm", ".pbm", ".tga", ".pcx", ".dds", ".jp2", ".j2k",
           ".eps", ".psd"}  # eps/psd 不一定成功，失败会给出提示


def ensure_inbox() -> str:
    os.makedirs(INBOX, exist_ok=True)
    return INBOX


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _normalize(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _save_png(im: Image.Image, tag: str) -> str:
    ensure_inbox()
    base = "%s-%s" % (tag, _stamp())
    out = os.path.join(INBOX, base + ".png")
    n = 1
    while os.path.exists(out):          # 同一秒内多张图不要互相覆盖
        n += 1
        out = os.path.join(INBOX, "%s-%d.png" % (base, n))
    im.save(out, "PNG")
    return out


def _thumb(im: Image.Image, path: str, max_side: int) -> str | None:
    w, h = im.size
    if max(w, h) <= max_side:
        return None
    r = max_side / float(max(w, h))
    t = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)
    out = path[:-4] + "-thumb.png"
    t.save(out, "PNG")
    return out


def rasterize_svg(src: str) -> str | None:
    """用 Edge headless 把 SVG 渲染成 PNG（宽高交给页面自适应）。"""
    if not os.path.exists(EDGE):
        return None
    ensure_inbox()
    out = os.path.join(INBOX, "svg-%s.png" % _stamp())
    url = "file:///" + os.path.abspath(src).replace("\\", "/")
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--window-size=1600,1600", "--screenshot=" + out, url]
    try:
        subprocess.run(cmd, timeout=45, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return None
    return out if os.path.exists(out) and os.path.getsize(out) > 2000 else None


def load_any(src: str) -> tuple[Image.Image, str]:
    """打开任意图片文件，返回 (PIL Image, 说明)。"""
    ext = os.path.splitext(src)[1].lower()
    if ext == ".svg":
        png = rasterize_svg(src)
        if not png:
            raise RuntimeError("SVG 渲染失败（缺 Edge？）。可以先用浏览器打开另存为 PNG。")
        return _normalize(Image.open(png)), "svg->png(Edge)"
    try:
        return _normalize(Image.open(src)), "PIL:%s" % ext
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        hint = ""
        if ext in (".heic", ".heif"):
            hint = "（iPhone 的 HEIC：手机相册里导出/分享成 JPG，或微信发给自己再存到电脑）"
        elif ext == ".pdf":
            hint = "（PDF：先截图那一页，或把页面导出成 PNG；本机没有 PDF 渲染器）"
        raise RuntimeError("无法解码 %s：%s%s" % (os.path.basename(src), err, hint))


def clipboard_image() -> Image.Image | None:
    """读剪贴板：可能是位图，也可能是「复制的图片文件」。"""
    try:
        data = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, Image.Image):
        return _normalize(data)
    if isinstance(data, list) and data:
        for p in data:
            try:
                return load_any(str(p))[0]
            except Exception:  # noqa: BLE001
                continue
    return None


def latest_in_dir(d: str) -> str | None:
    best, best_t = None, -1.0
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            t = os.path.getmtime(p)
            if t > best_t:
                best, best_t = p, t
    return best


def emit(im: Image.Image, path: str, tag: str, fmt: str, thumb_side: int | None) -> dict:
    th = _thumb(im, path, thumb_side) if thumb_side else None
    return {"ok": True, "path": path, "thumb": th, "size": [im.width, im.height],
            "format": fmt, "mode": im.mode, "bytes": os.path.getsize(path)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--clipboard", action="store_true", help="读剪贴板里的图")
    ap.add_argument("--latest", action="store_true", help="取 inbox 里最新一张")
    ap.add_argument("--thumb", type=int, default=1400, help="缩略图最长边（0=不出）")
    ap.add_argument("--tag", default="img")
    a = ap.parse_args(argv)

    try:
        if a.clipboard:
            im = clipboard_image()
            if im is None:
                print(json.dumps({"ok": False, "err": "剪贴板里没有图片（复制一张截图/图片再试）"},
                                 ensure_ascii=False))
                return 3
            p = _save_png(im, "clip")
            print(json.dumps(emit(im, p, "clip", "clipboard", a.thumb), ensure_ascii=False))
            return 0
        if a.latest:
            ensure_inbox()
            p0 = latest_in_dir(INBOX)
            if not p0:
                print(json.dumps({"ok": False, "err": "inbox 是空的"}, ensure_ascii=False))
                return 2
            im, fmt = load_any(p0)
            p = _save_png(im, a.tag)
            print(json.dumps(emit(im, p, a.tag, fmt, a.thumb), ensure_ascii=False))
            return 0
        if not a.path:
            ap.print_help()
            return 2
        if not os.path.exists(a.path):
            print(json.dumps({"ok": False, "err": "文件不存在: %s" % a.path}, ensure_ascii=False))
            return 2
        im, fmt = load_any(a.path)
        p = _save_png(im, a.tag)
        print(json.dumps(emit(im, p, a.tag, fmt, a.thumb), ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "err": str(exc)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
