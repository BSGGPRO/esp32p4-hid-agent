#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/photo-draw-gui.py — 点几下就能「照片 -> 画」的小窗口程序。

用法：双击仓库根的「照片转画.bat」，或 python tools\\photo-draw-gui.py
流程：选照片 -> 选质量 -> 预览（不动鼠标）-> 满意了点「开始画」（板子鼠标真画到画图里）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
CLI = os.path.join(_HERE, "photo-to-draw.py")
PY = sys.executable
CREATE_NO_WINDOW = 0x08000000

QUALITY_TEXT = {
    "quick": "快速（笔画少，十几秒到半分钟）",
    "good": "标准（推荐，明暗层次好，1-3 分钟）",
    "hd": "精细（最慢，可能 5-10 分钟）",
}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("照片转画 · ESP32-P4 手绘")
        root.geometry("760x560")
        root.configure(bg="#1b1d22")
        self.proc: subprocess.Popen | None = None
        self.photo = ""
        self.last_preview = ""

        fg, bg, sub = "#e8eaf0", "#1b1d22", "#262a32"
        pad = {"padx": 12, "pady": 8}

        top = tk.Frame(root, bg=bg)
        top.pack(fill="x", **pad)
        tk.Label(top, text="照片：", fg=fg, bg=bg, font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.path_lbl = tk.Label(top, text="（还没选）", fg="#8b93a7", bg=bg,
                                 anchor="w", font=("Consolas", 9))
        self.path_lbl.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(top, text="选择照片…", command=self.pick, bg="#2f3542", fg=fg,
                  relief="flat", padx=12).pack(side="right")

        mid = tk.Frame(root, bg=bg)
        mid.pack(fill="x", **pad)
        tk.Label(mid, text="质量：", fg=fg, bg=bg).pack(side="left")
        self.quality = ttk.Combobox(mid, state="readonly", width=34,
                                    values=[QUALITY_TEXT[k] for k in ("quick", "good", "hd")])
        self.quality.current(1)
        self.quality.pack(side="left", padx=6)
        self.canvas_mode = ttk.Combobox(mid, state="readonly", width=26,
                                        values=["画布：自动（画图窗口）", "画布：预览 900x900"])
        self.canvas_mode.current(0)
        self.canvas_mode.pack(side="left", padx=6)

        btns = tk.Frame(root, bg=bg)
        btns.pack(fill="x", **pad)
        self.b_preview = tk.Button(btns, text="① 预览（不动鼠标）", command=self.preview,
                                   bg="#2d6cdf", fg="white", relief="flat", padx=14, pady=8)
        self.b_preview.pack(side="left")
        self.b_draw = tk.Button(btns, text="② 开始画（板子鼠标真画）", command=self.draw,
                                bg="#c0392b", fg="white", relief="flat", padx=14, pady=8)
        self.b_draw.pack(side="left", padx=10)
        self.b_open = tk.Button(btns, text="打开预览图", command=self.open_preview,
                                bg="#2f3542", fg=fg, relief="flat", padx=12, pady=8)
        self.b_open.pack(side="left")
        self.b_stop = tk.Button(btns, text="停止", command=self.stop, bg="#2f3542", fg=fg,
                                relief="flat", padx=12, pady=8, state="disabled")
        self.b_stop.pack(side="left", padx=10)

        self.log = tk.Text(root, bg="#12141a", fg="#c8d0e0", insertbackground=fg,
                           wrap="word", font=("Consolas", 9), relief="flat")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.log.configure(state="disabled")

        tk.Label(root, text="提示：画之前先打开「画图」，窗口别最大化；画的过程中别抢鼠标。",
                 fg="#8b93a7", bg=bg, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12, pady=(0, 10))

    # ---------- 工具 ----------
    def say(self, s: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pick(self) -> None:
        p = filedialog.askopenfilename(
            title="选择照片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif *.jfif"),
                       ("所有文件", "*.*")])
        if p:
            self.photo = p
            self.path_lbl.configure(text=p, fg="#e8eaf0")

    def quality_key(self) -> str:
        return ("quick", "good", "hd")[self.quality.current()]

    def run_cli(self, extra: list[str]) -> None:
        if not self.photo:
            messagebox.showinfo("还没选照片", "先点右上角「选择照片…」")
            return
        if not os.path.isfile(CLI):
            messagebox.showerror("文件缺失", "找不到 %s" % CLI)
            return
        cmd = [PY, CLI, self.photo, "--quality", self.quality_key()] + extra
        self.say("$ " + " ".join(cmd))
        self.b_preview.configure(state="disabled")
        self.b_draw.configure(state="disabled")
        self.b_stop.configure(state="normal")

        def worker() -> None:
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True,
                                             encoding="utf-8", errors="replace",
                                             creationflags=CREATE_NO_WINDOW)
                assert self.proc.stdout is not None
                last_json = None
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    self.root.after(0, self.say, line)
                    if line.startswith("{"):
                        last_json = line
                self.proc.wait()
                if last_json:
                    try:
                        info = json.loads(last_json)
                        self.root.after(0, self.report, info)
                    except json.JSONDecodeError:
                        pass
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, self.say, "出错：%r" % (exc,))
            finally:
                self.root.after(0, self.done)

        threading.Thread(target=worker, daemon=True).start()

    def report(self, info: dict) -> None:
        prev = info.get("preview")
        if prev:
            self.last_preview = prev
        sim = info.get("similarity") or {}
        self.say("—— 结果 ——")
        self.say("  笔画 %s 笔 / 顶点 %s / 预估 %s 秒 / 实际 %s 秒"
                 % (info.get("strokes"), info.get("vertices"),
                    info.get("est_seconds"), info.get("seconds")))
        if sim:
            self.say("  像不像（数值）：pearson %s / 墨迹重合 %s"
                     % (sim.get("pearson"), sim.get("ink_iou")))
        if info.get("run"):
            r = info["run"]
            self.say("  真画：完成 %s 笔，被打断 %s 次，用时 %s 秒"
                     % (r.get("strokes_done"), r.get("interruptions"), round(r.get("ms", 0) / 1000, 1)))
        if prev:
            self.say("  预览：%s" % prev)

    def done(self) -> None:
        self.b_preview.configure(state="normal")
        self.b_draw.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self.proc = None

    def preview(self) -> None:
        self.run_cli(["--canvas", "0,0,900,900"])

    def draw(self) -> None:
        if not messagebox.askokcancel("开始真画？",
                                      "接下来板子会真的控制鼠标在「画图」里画。\n\n"
                                      "· 请先打开画图、窗口别最大化\n"
                                      "· 画的过程中别抢鼠标（抢了会抬笔跳过那一笔）\n\n继续？"):
            return
        self.run_cli(["--draw", "--progress", "50"])

    def open_preview(self) -> None:
        if self.last_preview and os.path.isfile(self.last_preview):
            os.startfile(self.last_preview)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("还没有预览", "先点「① 预览」")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.say("已请求停止")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
