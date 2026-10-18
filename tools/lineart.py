#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/lineart.py — 照片 -> 干净的线稿（HED 深度学习边缘，走 GPU）。

为什么要有它：等值线（engrave）画照片天生是一团乱麻，人眼认不出来。
HED 输出的是「人眼认得的轮廓线」，线条少、连贯，适合用画笔一笔笔描。

模型：lllyasviel/Annotators 的 ControlNetHED.pth（Apache-2.0，约 29 MB）。
权重默认放在 C:\\Users\\<你>\\.dsh\\models\\lineart\\ControlNetHED.pth。

用法：
    python tools\\lineart.py 照片.jpg                       # 出线稿 PNG
    python tools\\lineart.py 照片.jpg --thresh 0.12 --blur 1.0
    python tools\\lineart.py 照片.jpg --scale 1200 --out 线稿.png

参数：
    --thresh  NMS 阈值（0.02 细碎，0.10 适中，0.20 只留主轮廓）
    --blur    先高斯平滑，越大线条越少越粗（0.5-2.0）
    --scale   长边像素（默认 1200）
    --invert  输出反过来（黑底白线）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

DEFAULT_CKPT = os.path.join(os.path.expanduser("~"), ".dsh", "models", "lineart",
                            "ControlNetHED.pth")
MODEL_DIR = os.path.dirname(DEFAULT_CKPT)

# Informative Drawings（CVPR 2022，专训「照片->看得懂的线稿」）的 ONNX 版本
ID_MODELS = {
    "anime": "informative-drawings_anime_768x768.onnx",
    "anime768": "informative-drawings_anime_768x768.onnx",
    "contour": "informative-drawings_contour_768x768.onnx",
    "contour768": "informative-drawings_contour_768x768.onnx",
    "contour1024": "informative-drawings_contour_1024x1024.onnx",
}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _build_model():
    """ControlNetHED_Apache2（和 lllyasviel/ControlNet 里的实现等价）。"""
    import torch

    class DoubleConvBlock(torch.nn.Module):
        def __init__(self, input_channel, output_channel, layer_number):
            super().__init__()
            self.convs = torch.nn.Sequential()
            self.convs.append(torch.nn.Conv2d(input_channel, output_channel, 3, 1, 1))
            for _ in range(1, layer_number):
                self.convs.append(torch.nn.Conv2d(output_channel, output_channel, 3, 1, 1))
            self.projection = torch.nn.Conv2d(output_channel, 1, 1, 1, 0)

        def forward(self, x, down_sampling=False):
            h = x
            if down_sampling:
                h = torch.nn.functional.max_pool2d(h, kernel_size=2, stride=2)
            for conv in self.convs:
                h = torch.nn.functional.relu(conv(h))
            return h, self.projection(h)

    class ControlNetHED(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.norm = torch.nn.Parameter(torch.zeros(size=(1, 3, 1, 1)))
            self.block1 = DoubleConvBlock(3, 64, 2)
            self.block2 = DoubleConvBlock(64, 128, 2)
            self.block3 = DoubleConvBlock(128, 256, 3)
            self.block4 = DoubleConvBlock(256, 512, 3)
            self.block5 = DoubleConvBlock(512, 512, 3)

        def forward(self, x):
            h = x - self.norm
            h, p1 = self.block1(h)
            h, p2 = self.block2(h, down_sampling=True)
            h, p3 = self.block3(h, down_sampling=True)
            h, p4 = self.block4(h, down_sampling=True)
            h, p5 = self.block5(h, down_sampling=True)
            return p1, p2, p3, p4, p5

    return ControlNetHED


def hed_edges(rgb: np.ndarray, ckpt: str, device: str = "auto") -> np.ndarray:
    """rgb: HxWx3 uint8 -> 0..255 边缘强度图（越大越像轮廓）。"""
    import torch

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    net = _build_model()().to(device).float().eval()
    state = torch.load(ckpt, map_location=device)
    net.load_state_dict(state)

    h, w = rgb.shape[:2]
    ph, pw = (-h) % 16, (-w) % 16
    if ph or pw:
        rgb = np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="edge")
    x = torch.from_numpy(rgb.astype(np.float32)).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        outs = net(x)
        probs = []
        for o in outs:
            e = torch.nn.functional.interpolate(o, size=x.shape[-2:], mode="bilinear",
                                                align_corners=False)
            probs.append(e)
        edge = torch.sigmoid(torch.stack(probs, 0).mean(0))
        edge = edge[0, 0].cpu().numpy()
    return (edge[:h, :w] * 255.0).clip(0, 255).astype(np.uint8)


def nms_thin(x: np.ndarray, thresh: float, sigma: float) -> np.ndarray:
    """非极大值抑制：把软边缘压成 1 px 细线（和 ControlNet 的用法一致）。"""
    import cv2
    f = cv2.GaussianBlur(x.astype(np.float32), (0, 0), sigma)
    f1 = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]], np.uint8)
    f2 = np.array([[0, 1, 0], [0, 1, 0], [0, 1, 0]], np.uint8)
    f3 = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], np.uint8)
    f4 = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], np.uint8)
    y = np.zeros_like(f)
    for kern in (f1, f2, f3, f4):
        y = np.where(cv2.dilate(f, kern) == f, f, y)
    out = np.zeros(y.shape, np.uint8)
    out[y > (thresh * 255.0)] = 255
    return out


def _id_once(sess, tile: Image.Image, crop: bool, normalize: bool = True) -> Image.Image:
    size = int(sess.get_inputs()[0].shape[-1])
    sc = size / float(max(tile.size))
    small = tile.resize((max(2, int(tile.width * sc)), max(2, int(tile.height * sc))),
                        Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    ox, oy = (size - small.width) // 2, (size - small.height) // 2
    canvas.paste(small, (ox, oy))
    x = np.asarray(canvas, dtype=np.float32) / 127.5 - 1.0
    y = sess.run(None, {sess.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0]
    g = y[0, 0]
    if normalize:
        g = (g - float(g.min())) / (float(g.max()) - float(g.min()) + 1e-9)   # 0=线, 1=白
    g = (np.clip(g, 0.0, 1.0) * 255.0).astype(np.uint8)
    img = Image.fromarray(g, mode="L")
    if crop:
        img = img.crop((ox, oy, ox + small.width, oy + small.height))
    return img


def _norm_acc(acc: np.ndarray) -> Image.Image:
    """拼接后用整幅图做一次归一化（每块各自归一化会导致对比度忽强忽弱）。"""
    lo, hi = float(acc.min()), float(acc.max())
    a = (acc - lo) / (hi - lo) if hi > lo else np.zeros_like(acc)
    return Image.fromarray((a * 255.0).clip(0, 255).astype(np.uint8), mode="L")


def id_lines_tiled(photo: str, model: str, target: int = 1500, overlap: float = 0.25,
                   tile_size: int = 0) -> Image.Image:
    """分块跑模型再拼起来。

    tile_size>0：固定边长瓦片（照片宽多少就切多大，细节按瓦片算，最细）；
    tile_size=0：按长宽比切条（省事，但竖图一条有 1280 宽，模型只能吃 768）。
    """
    import onnxruntime as ort

    sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    im = Image.open(photo).convert("RGB")
    W, H = im.size
    out_scale = target / float(max(W, H))
    ow, oh = max(2, int(W * out_scale)), max(2, int(H * out_scale))
    acc = np.full((oh, ow), 255.0, dtype=np.float32)

    def blit(line: Image.Image, sx0: int, sy0: int, sx1: int, sy1: int) -> None:
        lx0, ly0 = int(sx0 * out_scale), int(sy0 * out_scale)
        lx1, ly1 = min(ow, int(sx1 * out_scale)), min(oh, int(sy1 * out_scale))
        lw, lh = max(2, lx1 - lx0), max(2, ly1 - ly0)
        part = np.asarray(line.resize((lw, lh), Image.LANCZOS), dtype=np.float32)
        h2 = min(part.shape[0], oh - ly0)
        w2 = min(part.shape[1], ow - lx0)
        if h2 <= 0 or w2 <= 0:
            return
        acc[ly0:ly0 + h2, lx0:lx0 + w2] = np.minimum(
            acc[ly0:ly0 + h2, lx0:lx0 + w2], part[:h2, :w2])

    if tile_size > 0:
        step = max(32, int(tile_size * (1.0 - overlap)))
        ys = list(range(0, max(1, H - tile_size + step), step)) or [0]
        xs = list(range(0, max(1, W - tile_size + step), step)) or [0]
        for y0 in ys:
            for x0 in xs:
                x1, y1 = min(W, x0 + tile_size), min(H, y0 + tile_size)
                x0 = max(0, x1 - tile_size)
                y0 = max(0, y1 - tile_size)
                crop = im.crop((x0, y0, x1, y1))
                blit(_id_once(sess, crop, crop=True, normalize=False), x0, y0, x1, y1)
        return _norm_acc(acc)

    if H >= W:
        tiles = max(1, int(round(H / float(W))))
        step_y = H / float(tiles)
        for k in range(tiles):
            y0 = max(0, int(k * step_y - step_y * overlap * 0.5))
            y1 = min(H, int((k + 1) * step_y + step_y * overlap * 0.5))
            blit(_id_once(sess, im.crop((0, y0, W, y1)), crop=True, normalize=False), 0, y0, W, y1)
    else:
        tiles = max(1, int(round(W / float(H))))
        step_x = W / float(tiles)
        for k in range(tiles):
            x0 = max(0, int(k * step_x - step_x * overlap * 0.5))
            x1 = min(W, int((k + 1) * step_x + step_x * overlap * 0.5))
            blit(_id_once(sess, im.crop((x0, 0, x1, H)), crop=True, normalize=False), x0, 0, x1, H)
    return _norm_acc(acc)


def id_lines(photo: str, model: str, crop: bool = True) -> Image.Image:
    """Informative Drawings：照片 -> 线稿（白底黑线）。模型固定输入 768/1024 方图。"""
    import onnxruntime as ort

    sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    return _id_once(sess, Image.open(photo).convert("RGB"), crop)


def drop_small(img: Image.Image, min_area: int) -> Image.Image:
    """去掉孤立的小线条团（模型在纹理区会拉出零碎短线，也是"杂线"的来源）。"""
    if min_area <= 0:
        return img
    import cv2
    a = np.asarray(img, dtype=np.uint8)
    mask = (a < 160).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep[lab == i] = 1
    out = a.copy()
    out[keep == 0] = 255
    return Image.fromarray(out, mode="L")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="照片 -> 线稿（HED 或 Informative Drawings）")
    ap.add_argument("photo")
    ap.add_argument("--out", default=None, help="输出 PNG（默认同目录 *-lineart.png）")
    ap.add_argument("--ckpt", default=DEFAULT_CKPT)
    ap.add_argument("--thresh", type=float, default=0.10, help="NMS 阈值，越大线越少")
    ap.add_argument("--blur", type=float, default=1.0, help="高斯 sigma，越大越干净")
    ap.add_argument("--scale", type=int, default=1200, help="长边像素")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--invert", action="store_true", help="黑底白线")
    ap.add_argument("--engine", default="hed", choices=["hed", "id"],
                    help="hed=通用边缘(快,纹理多) / id=Informative Drawings(专训照片线稿)")
    ap.add_argument("--id-model", default="anime",
                    help="id 引擎的模型：anime / contour / contour1024")
    ap.add_argument("--no-crop", action="store_true", help="id 输出不裁掉白边")
    ap.add_argument("--tile", action="store_true",
                    help="id 分块推理拼接（长图分辨率翻 2-3 倍，线条更细更准）")
    ap.add_argument("--target", type=int, default=1500, help="--tile 时输出长边像素")
    ap.add_argument("--tile-size", type=int, default=0,
                    help="固定瓦片边长（源图 px；0=按长宽比切条。640-800 最细）")
    ap.add_argument("--denoise", type=int, default=0,
                    help="去掉面积小于 N 像素的碎线条（0=关；20-60 清杂线）")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.photo):
        print(json.dumps({"ok": False, "err": "照片不存在: %s" % a.photo}, ensure_ascii=False))
        return 1
    outp = a.out or (os.path.splitext(a.photo)[0] + "-lineart.png")

    if a.engine == "id":
        model = ID_MODELS.get(a.id_model, a.id_model)
        if not os.path.isabs(model):
            model = os.path.join(MODEL_DIR, model)
        if not os.path.isfile(model):
            print(json.dumps({"ok": False, "err": "缺模型: %s" % model,
                              "hint": "hf-mirror.com/x-Liola-x/informative-drawings-onnx"},
                             ensure_ascii=False))
            return 2
        if a.tile:
            out_img = id_lines_tiled(a.photo, model, target=a.target,
                                     tile_size=a.tile_size)
        else:
            out_img = id_lines(a.photo, model, crop=not a.no_crop)
        out_img = drop_small(out_img, a.denoise)
        if a.invert:
            out_img = Image.fromarray(255 - np.asarray(out_img, dtype=np.uint8), mode="L")
        os.makedirs(os.path.dirname(os.path.abspath(outp)), exist_ok=True)
        out_img.convert("RGB").save(outp, "PNG")
        dark = int((np.asarray(out_img) < 128).sum())
        print(json.dumps({"ok": True, "out": outp, "engine": "id", "model": os.path.basename(model),
                          "size": list(out_img.size), "line_px": dark}, ensure_ascii=False))
        return 0

    if not os.path.isfile(a.ckpt):
        print(json.dumps({"ok": False, "err": "缺模型权重: %s" % a.ckpt,
                          "hint": "从 lllyasviel/Annotators 下 ControlNetHED.pth"},
                         ensure_ascii=False))
        return 2

    im = Image.open(a.photo).convert("RGB")
    sc = a.scale / float(max(im.size))
    if sc < 1.0:
        im = im.resize((max(2, int(im.width * sc)), max(2, int(im.height * sc))), Image.LANCZOS)
    rgb = np.asarray(im, dtype=np.uint8)

    edge = hed_edges(rgb, a.ckpt, a.device)
    lines = nms_thin(edge, a.thresh, a.blur)
    out_img = Image.fromarray(255 - lines if not a.invert else lines, mode="L").convert("RGB")
    os.makedirs(os.path.dirname(os.path.abspath(outp)), exist_ok=True)
    out_img.save(outp, "PNG")

    import torch
    print(json.dumps({"ok": True, "out": outp, "engine": "hed", "size": list(out_img.size),
                      "device": a.device if a.device != "auto" else
                      ("cuda" if torch.cuda.is_available() else "cpu"),
                      "thresh": a.thresh, "blur": a.blur,
                      "line_px": int((lines > 0).sum())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
