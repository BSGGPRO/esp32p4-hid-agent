#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/agent_ui.py — 窗口版控制器：双击打开一个窗口，不闪退。

命令行版（esp32p4-agent.exe）是给 AI 的：它读 stdin、打 JSON、退出。
人双起点它就"闪退"——因为它本来就是控制台程序，打完用法就退。

这个窗口版是给人的：
  [保存 AI 说明书…]  把说明书存成 .md，直接拖给别的 AI
  [复制说明书]       整份复制到剪贴板，粘进聊天框
  [自检]             屏幕/OCR/板子/指针/视觉 五项
  [服务状态]         hub / 板子 / 视觉服务在不在
  [看说明书]         就在窗口里读
  [运行作业]         粘一段 JSON 作业，用板子执行

执行逻辑一行没重写：全部走 tools/connector.py。
打包：tools\\build-ui.cmd（--windowed）→ dist\\esp32p4-agent-ui.exe
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "pc")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tkinter as tk                       # noqa: E402
from tkinter import filedialog, messagebox, scrolledtext  # noqa: E402

MANUAL_FILE_NAME = "AI-控制器说明书.md"
LOG_NAME = "agent-ui-error.log"

# 和 tools/photo-draw-gui.py 保持一致的一套配色，同一套工具别长两张脸
BG, BG2 = "#1b1d22", "#262a32"
FG, SUB = "#e8eaf0", "#8b93a7"
FONT = ("Microsoft YaHei UI", 10)
MONO = ("Consolas", 10)

_conn = None          # 懒加载：connector 会连带导入 Pillow 等，窗口先出来更重要


# ---------------------------------------------------------------- 基础

def manual_text() -> str:
    """说明书全文。打包时用 --add-data 塞进 EXE，没打包就读仓库里同一份。"""
    p = _agent()._manual_path()       # 定位逻辑只有一份，别在两处各写一遍
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise RuntimeError("找不到说明书文件：%s" % p)
    except OSError as e:
        raise RuntimeError("读说明书失败：%s（%s）" % (p, e))
    return raw.decode("utf-8-sig")


def save_manual(path: str) -> int:
    """把说明书写成 UTF-8（无 BOM、统一 LF）。返回写入字节数。

    父目录不存在就建；建不出来或写不进去，抛带中文说明的 RuntimeError——
    绝不静默失败，否则用户以为存好了其实没有。
    """
    text = manual_text().replace("\r\n", "\n").replace("\r", "\n")
    data = text.encode("utf-8")
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise RuntimeError("保存失败：建不出目录 %s（%s）" % (parent, e))
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        raise RuntimeError("保存失败：%s\n目标：%s" % (e, path))
    return len(data)


def log_path() -> str:
    """崩溃日志放哪儿：优先 EXE 旁边，不行就 %TEMP%。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = ROOT
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    for d in (base, tmp):
        try:
            if os.path.isdir(d) and os.access(d, os.W_OK):
                return os.path.join(d, LOG_NAME)
        except OSError:
            continue
    return os.path.join(tmp, LOG_NAME)


def write_crash(where: str, exc_info: str) -> str:
    """把异常写进日志文件。写成功返回路径，写不进去返回空串——绝不能冒报「已写入」。"""
    p = log_path()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n===== %s | %s =====\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), where, exc_info))
    except OSError:
        return ""
    return p


def _agent():
    import agent as _a                    # 复用命令行版的 caps/services/transport
    return _a


def _connector():
    global _conn
    if _conn is None:                     # import 本身就是线程安全的，不用额外加锁
        import connector as _c
        _conn = _c
    return _conn


def caps_text() -> str:
    return json.dumps(_agent().caps(), ensure_ascii=False, indent=2)


def services_text(s: dict | None = None) -> str:
    s = s or _agent().services()
    hub, tcp, vis = s["hub"], s["board_tcp"], s["vision"]
    ok = "✓"
    no = "✗"
    lines = [
        "hub        %s %s   %s" % (ok if hub["up"] else no, hub["url"],
                                   ("串口 %s 已打开" % hub["port"]) if hub.get("serial_open")
                                   else "串口没打开"),
        "板子 TCP   %s %s" % (ok if tcp["up"] else no, tcp["addr"]),
        "视觉服务   %s %s   %s" % (ok if vis["up"] else no, vis["url"],
                                   ("OCR 可用" if vis.get("ocr") else "OCR 没加载")
                                   + ("· 大模型已装" if vis.get("vlm_ready") else "· 大模型按需加载")),
        "",
        "说明：OCR / 找文字 / 读窗口 需要「视觉服务」；纯键鼠不需要。",
    ]
    return "\n".join(lines)


def _selfcheck() -> tuple[bool, str]:
    t = _agent()._pick_transport("auto")
    r = _connector().selfcheck(t)
    return bool(r.get("ok")), json.dumps(r, ensure_ascii=False, indent=2)


def selfcheck_text() -> str:
    return _selfcheck()[1]


def run_job_text(raw: str, transport: str | None = None) -> tuple[bool, str]:
    """执行一段作业 JSON。永不抛异常——界面上的失败也必须是可读的话。"""
    raw = (raw or "").strip()
    if not raw:
        return False, ("还没有作业内容。\n在上面的框里粘一段 JSON，例如：\n"
                       '{"steps":[{"op":"title","exe":"msedge"}]}')
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, "这不是合法的 JSON：第 %d 行第 %d 列，%s" % (e.lineno, e.colno, e.msg)
    if not isinstance(job, (dict, list)):
        return False, "作业应该是 {\"steps\": [...]} 或 [...]，现在拿到的是 %s" % type(job).__name__
    steps = job.get("steps") if isinstance(job, dict) else job
    if not isinstance(steps, list) or not all(isinstance(s, dict) and s.get("op") for s in steps):
        return False, ("作业格式不对：每一步都得是带 op 的对象，例如\n"
                       '{"steps":[{"op":"title","exe":"msedge"}]}')
    if not steps:
        # require 是"每一步"的前置条件（connector 只认 step 里的 require），
        # 顶层 require 没人执行——所以空 steps 就是什么都没做，不能报成功。
        return False, "作业里没有步骤（什么都没做）。至少给一步，例如\n" \
                      '{"steps":[{"op":"title","exe":"msedge"}]}'
    try:
        t = transport or _agent()._pick_transport("auto")
        out = _connector().run_job(job, transport=t)
    except Exception as e:                                  # noqa: BLE001
        return False, "执行时出错：%s: %s" % (type(e).__name__, e)
    return bool(out.get("ok")), json.dumps(out, ensure_ascii=False, indent=2)


def _dpi_aware() -> None:
    """4K 屏上不让系统把我的窗口拉成糊的。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:                                   # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:                                       # noqa: BLE001
        pass


# ---------------------------------------------------------------- 窗口

class App(tk.Tk):
    """主窗口。构建本身不做任何耗时的事——不然双击后看起来像卡住。"""

    def __init__(self) -> None:
        _dpi_aware()
        super().__init__()
        self.title("ESP32-P4 板子控制器 %s" % _agent().VERSION)
        self.geometry("960x660")
        self.minsize(780, 540)
        self.q: queue.Queue = queue.Queue()
        self._busy = False          # 一次只跑一个动作：两个作业抢同一个串口会互相打乱
        self._pending = 0
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        self._log("就绪。板子是手和眼，这里是遥控器。")
        self._log("要给别的 AI 用：点「保存 AI 说明书…」，把存出来的 .md 拖给它。")
        self._log_busy_hint()

    def report_callback_exception(self, exc, val, tb) -> None:
        """按钮回调里的异常不会传到 main() 的 try；--windowed 下 stderr 是 None，
        tkinter 默认处理器自己还会抛一次，结果就是"点了没反应、什么都没有"。"""
        where = write_crash("回调", "".join(traceback.format_exception(exc, val, tb)))
        text = "%s: %s" % (exc.__name__, val)
        if where:
            text += "\n\n详情：%s" % where
        else:
            text += "\n\n（崩溃日志没写成，可能是目录不可写。）"
        try:
            self._log(text, "bad")
        except Exception:                                    # noqa: BLE001
            pass
        try:
            messagebox.showerror("出错了", text)
        except Exception:                                    # noqa: BLE001
            pass

    def on_close(self) -> None:
        if self._busy and not messagebox.askyesno(
                "正在跑", "还有动作在跑。现在关掉，板子可能停在按键中间。\n确定退出？"):
            return
        self.destroy()

    # -- 布局
    def _build(self) -> None:
        self.configure(bg=BG)
        bar = tk.Frame(self, bg=BG, padx=10, pady=8)
        bar.pack(side=tk.TOP, fill=tk.X)

        self.btn_save = tk.Button(bar, text="保存 AI 说明书…（下载）", width=22, font=FONT,
                                  command=self.on_save_manual)
        self.btn_copy = tk.Button(bar, text="复制说明书", width=11, font=FONT, command=self.on_copy_manual)
        self.btn_check = tk.Button(bar, text="自检", width=8, font=FONT, command=self.on_selfcheck)
        self.btn_svc = tk.Button(bar, text="服务状态", width=10, font=FONT, command=self.on_services)
        self.btn_read = tk.Button(bar, text="看说明书", width=10, font=FONT, command=self.on_read_manual)
        for i, b in enumerate((self.btn_save, self.btn_copy, self.btn_check,
                               self.btn_svc, self.btn_read)):
            b.grid(row=0, column=i, padx=(0, 6))

        tk.Label(bar, text="保存出来的 .md 可以直接拖给 Codex / 豆包 / Gemini 用",
                 fg=SUB, bg=BG, font=FONT).grid(row=1, column=0, columnspan=6,
                                                pady=(6, 0), sticky="w")

        box = tk.LabelFrame(self, text="输出", padx=6, pady=6, fg=FG, bg=BG,
                            font=FONT, labelanchor="nw")
        box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10)
        self.out = scrolledtext.ScrolledText(box, height=18, width=1, wrap=tk.WORD, font=MONO,
                                             state=tk.DISABLED, bg=BG2, fg=FG,
                                             insertbackground=FG, relief=tk.FLAT)
        self.out.pack(fill=tk.BOTH, expand=True)

        job = tk.LabelFrame(self, text="运行作业（JSON）", padx=6, pady=6, fg=FG, bg=BG,
                            font=FONT, labelanchor="nw")
        job.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))
        self.job_in = tk.Text(job, height=4, width=1, wrap=tk.WORD, font=MONO, bg=BG2, fg=FG,
                              insertbackground=FG, relief=tk.FLAT)
        self.job_in.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.job_in.insert("1.0", '{"steps":[{"op":"title","exe":"msedge"}]}')
        side = tk.Frame(job, bg=BG)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        self.btn_run = tk.Button(side, text="用板子运行", width=12, font=FONT, command=self.on_run)
        self.btn_run.pack(side=tk.TOP, pady=(0, 6))
        tk.Button(side, text="清空输出", width=12, font=FONT, command=self.clear).pack(side=tk.TOP)

        self.status = tk.Label(self, text="", anchor="w", fg=SUB, bg=BG, padx=10, pady=4,
                               font=FONT)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    # -- 输出
    def _log(self, text: str, tag: str = "") -> None:
        mark = {"ok": "✓ ", "bad": "✗ ", "busy": "… "}.get(tag, "")
        self.out.configure(state=tk.NORMAL)
        self.out.insert(tk.END, "%s%s\n" % (mark, text))
        self.out.see(tk.END)
        self.out.configure(state=tk.DISABLED)

    def _log_busy_hint(self) -> None:
        self.status.configure(text="提示：耗时的操作会在后台跑，界面不会卡。")

    def clear(self) -> None:
        self.out.configure(state=tk.NORMAL)
        self.out.delete("1.0", tk.END)
        self.out.configure(state=tk.DISABLED)

    def _enable(self, on: bool) -> None:
        state = tk.NORMAL if on else tk.DISABLED
        for b in (self.btn_check, self.btn_svc, self.btn_run):
            b.configure(state=state)

    def _bg(self, label: str, fn) -> None:
        """耗时活放后台线程，结果回主线程显示（tkinter 不是线程安全的）。

        同一时刻只允许一个：两个作业并发会抢同一个串口/透传会话，行为不可预期。
        """
        if self._busy:
            self._log("上一个动作还没结束，等它回来再点「%s」。" % label, "busy")
            return
        self._busy = True
        self._pending += 1
        self._enable(False)
        self._log(label, "busy")

        def work() -> None:
            try:
                ok, text = fn()
            except Exception as e:                          # noqa: BLE001
                ok, text = False, "%s: %s" % (type(e).__name__, e)
            self.q.put((label, bool(ok), text))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                label, ok, text = self.q.get_nowait()
                self._pending -= 1
                self._log("%s → %s" % (label, "完成" if ok else "失败"), "ok" if ok else "bad")
                self._log(text)
        except queue.Empty:
            pass
        except tk.TclError:
            return                       # 窗口已经销毁，轮询到此为止
        if self._pending <= 0:
            self._pending = 0
            if self._busy:
                self._busy = False
                self._enable(True)
        self.after(120, self._drain)

    # -- 按钮
    def on_save_manual(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存 AI 说明书", initialfile=MANUAL_FILE_NAME,
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt"), ("全部文件", "*.*")])
        if not path:
            self._log("已取消保存。")
            return
        try:
            n = save_manual(path)
        except Exception as e:                              # noqa: BLE001
            messagebox.showerror("保存失败", str(e))
            self._log(str(e), "bad")
            return
        self._log("已保存：%s（%d 字节，UTF-8）" % (path, n), "ok")
        self._log("把这个文件拖给别的 AI 就行：能跑命令的给它 EXE，只能读文字的给它这个 .md。")
        messagebox.showinfo("保存好了", "说明书已保存到：\n%s\n\n可以直接拖给别的 AI。" % path)

    def on_copy_manual(self) -> None:
        try:
            text = manual_text()
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()          # 只处理布局/绘制，不重入事件循环
        except Exception as e:                              # noqa: BLE001
            self._log("复制失败：%s" % e, "bad")
            return
        self._log("已把说明书（%d 字符）复制到剪贴板，去粘给 AI。" % len(text), "ok")
        self._log("（这份内容在关掉窗口后可能被系统清掉；要长期留着用「保存 AI 说明书…」。）")

    def on_selfcheck(self) -> None:
        self._bg("五项自检（约 5 秒）", _selfcheck)

    def on_services(self) -> None:
        def work():
            s = _agent().services()
            up = any(x["up"] for x in (s["hub"], s["board_tcp"], s["vision"]))
            return up, services_text(s)
        self._bg("查服务状态", work)

    def on_read_manual(self) -> None:
        try:
            text = manual_text()
        except Exception as e:                              # noqa: BLE001
            messagebox.showerror("读不到说明书", str(e))
            return
        win = tk.Toplevel(self)
        win.title("AI 说明书（%d 行）" % text.count("\n"))
        win.geometry("900x700")
        box = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Microsoft YaHei UI", 10))
        box.pack(fill=tk.BOTH, expand=True)
        box.insert("1.0", text)
        box.configure(state=tk.DISABLED)

    def on_run(self) -> None:
        raw = self.job_in.get("1.0", tk.END)
        self._bg("运行作业", lambda: run_job_text(raw))


def windowless_selftest() -> dict:
    """打包后没法点按钮，用这个把窗口内的关键路径跑一遍（写 JSON，不显示窗口）。

    AGENT_UI_SELFTEST=1 esp32p4-agent-ui.exe  →  %TEMP%\\agent-ui-selftest.json
    """
    out: dict = {}
    tmp = os.path.join(os.environ.get("TEMP") or ".", "agent-ui-selftest.md")

    def probe_app():
        a = App()
        try:
            a.withdraw()
            return [w for w in ("btn_save", "btn_copy", "btn_check", "btn_svc",
                                "btn_read", "btn_run") if hasattr(a, w)]
        finally:
            a.destroy()

    for name, fn in (
        ("说明书可读（字符数）", lambda: len(manual_text())),
        ("说明书含禁止改固件", lambda: "禁止改固件" in manual_text()),
        ("保存说明书（字节数）", lambda: save_manual(tmp)),
        ("能力清单 op_count", lambda: json.loads(caps_text())["op_count"]),
        ("窗口能构建（控件名）", probe_app),
    ):
        try:
            out[name] = {"ok": True, "value": fn()}
        except Exception as e:                              # noqa: BLE001
            out[name] = {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
    out["ok"] = all(v.get("ok") for v in out.values() if isinstance(v, dict))
    return out


def main() -> int:
    if "--selftest" in sys.argv or os.environ.get("AGENT_UI_SELFTEST"):
        r = windowless_selftest()
        p = os.path.join(os.environ.get("TEMP") or ".", "agent-ui-selftest.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            msg = "自检结果：%s" % p
        except OSError:
            msg = json.dumps(r, ensure_ascii=False)
        # --windowed 打包后从资源管理器双击启动时 sys.stdout 是 None，print 会炸
        if sys.stdout is not None:
            print(msg)
        return 0 if r["ok"] else 1
    try:
        app = App()
    except Exception:                                        # noqa: BLE001
        where = write_crash("启动", traceback.format_exc())
        msg = "控制器没能打开窗口。"
        msg += ("\n详情已写入：\n%s" % where) if where else "\n（崩溃日志也没写成，可能是目录不可写。）"
        try:                                  # 先试 tkinter 弹窗（好看）
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", msg)
            root.destroy()
        except Exception:                                    # noqa: BLE001
            try:                              # Tk 本身坏了就直接调系统弹窗，不能一声不响
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "启动失败", 0x10)
            except Exception:                                # noqa: BLE001
                pass
        return 1
    try:
        app.mainloop()
    except Exception:                                        # noqa: BLE001
        where = write_crash("运行", traceback.format_exc())
        if sys.stderr is not None:
            print("崩溃日志：%s" % where, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
