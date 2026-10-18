#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/photo-draw-gui.py — 点几下就能「照片 -> 画」的小窗口程序。

用法：双击仓库根的「photo-draw.bat」，或 python tools\\photo-draw-gui.py
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

QUALITY_KEYS = ("sketch", "quick", "good", "hd")
QUALITY_TEXT = {
    "sketch": "线稿（推荐，轮廓清楚，约 1-2 分钟）",
    "quick": "快速素描（笔画少，十几秒到半分钟）",
    "good": "标准素描（明暗层次好，1-3 分钟）",
    "hd": "精细素描（最慢，可能 5-10 分钟）",
}
# (画布尺寸 or None=只预览, 界面文字)
CANVASES = [
    ("760x1040", "竖图 760x1040（推荐，约 2 分钟）"),
    ("1040x760", "横图 1040x760（更细，约 3-5 分钟）"),
    ("820x600", "小画布 820x600（最快）"),
    (None, "只预览 900x900（不动鼠标）"),
]


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("照片转画 · ESP32-P4 手绘")
        root.geometry("760x560")
        root.configure(bg="#1b1d22")
        self.proc: subprocess.Popen | None = None
        self.photo = ""
        self.last_preview = ""
        self.pending_open = False

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
                                    values=[QUALITY_TEXT[k] for k in QUALITY_KEYS])
        self.quality.current(0)
        self.quality.pack(side="left", padx=6)
        self.canvas_mode = ttk.Combobox(mid, state="readonly", width=30,
                                        values=[t for _, t in CANVASES])
        self.canvas_mode.current(0)
        self.canvas_mode.pack(side="left", padx=6)
        self.clear_var = tk.BooleanVar(value=True)
        tk.Checkbutton(mid, text="先清空画布", variable=self.clear_var, fg=fg, bg=bg,
                       selectcolor=bg, activebackground=bg,
                       activeforeground=fg).pack(side="left", padx=4)

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

        tk.Label(root, text="不用先开画图：点「开始画」会自动打开画图、清空画布再画；"
                            "有 5 秒倒计时，画的过程中按 ESC 立刻停笔。",
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
            self.say("已选照片，自动生成预览（不动鼠标）…")
            self.preview()

    def quality_key(self) -> str:
        return QUALITY_KEYS[self.quality.current()]

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
                buf: list[str] = []
                in_json = False
                last_info = None
                for line in self.proc.stdout:
                    raw = line.rstrip("\n")
                    if not raw.strip():
                        continue
                    self.root.after(0, self.say, raw)
                    # CLI 打印的是 indent=2 的多行 JSON：从 "{" 收到 "}" 再解析
                    stripped = raw.strip()
                    if not in_json and stripped == "{":
                        in_json, buf = True, [raw]
                        continue
                    if in_json:
                        buf.append(raw)
                        if stripped == "}":
                            in_json = False
                            try:
                                last_info = json.loads("\n".join(buf))
                            except json.JSONDecodeError:
                                last_info = None
                self.proc.wait()
                if last_info is not None:
                    self.root.after(0, self.report, last_info)
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
            if self.pending_open:
                self.pending_open = False
                self._open(prev)

    def _open(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.say("打开失败：%r" % (exc,))

    def done(self) -> None:
        self.b_preview.configure(state="normal")
        self.b_draw.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self.proc = None

    def preview(self) -> None:
        self.pending_open = True          # 预览完自动把图弹出来
        canvas, _label = CANVASES[self.canvas_mode.current()]
        self.run_cli(["--canvas", "0,0," + (canvas or "900x900").replace("x", ",")])

    def draw(self) -> None:
        canvas, _label = CANVASES[self.canvas_mode.current()]
        if not canvas:                    # 只预览，不碰鼠标
            self.say("选了「只预览」，走预览流程。")
            self.preview()
            return
        if not messagebox.askokcancel("开始真画？",
                                      "接下来会自动打开「画图」、准备 %s 的白画布，"
                                      "然后板子鼠标一笔一笔画。\n\n"
                                      "· 点确定后有 5 秒倒计时，按 ESC 取消\n"
                                      "· 画的过程中按 ESC 立刻停笔\n"
                                      "· 别抢鼠标（抢了会抬笔跳过那一笔）\n\n继续？" % canvas):
            return
        extra = ["--draw", "--progress", "50", "--stop-key", "esc",
                 "--paint-canvas", canvas]
        if not self.clear_var.get():
            extra.append("--keep-canvas")
        self.run_cli(extra)

    def open_preview(self) -> None:
        """没预览就现算一张（不用先点「① 预览」），有就打开。"""
        if self.last_preview and os.path.isfile(self.last_preview):
            self._open(self.last_preview)
            return
        if self.photo:
            self.say("还没有预览，先帮你生成一张（不动鼠标）…")
            self.preview()          # preview() 结束会自动弹图
            return
        newest = self.newest_preview()
        if newest:
            self.last_preview = newest
            self.say("打开最近一张预览：%s" % newest)
            self._open(newest)
            return
        messagebox.showinfo("还没选照片", "先点右上角「选择照片…」")

    @staticmethod
    def newest_preview() -> str:
        inbox = os.path.join(os.path.dirname(_ROOT), "inbox")
        try:
            files = [os.path.join(inbox, f) for f in os.listdir(inbox)
                     if f.endswith("-preview.png")]
        except OSError:
            return ""
        if not files:
            return ""
        return max(files, key=os.path.getmtime)

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
