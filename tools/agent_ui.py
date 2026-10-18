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
  [给 AI 用…]        选一个 AI，复制/保存它要的配置或指令
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

# 深色一套（比 photo-draw-gui 那版更收敛：窗底更暗、卡片分层、边框只留一条）
BG = "#15171c"        # 窗口底
BG2 = "#23262f"       # 输入框/文本框
CARD = "#1d2027"      # 卡片
FIELD = "#2a2e38"     # 普通按钮
BORDER = "#2f3440"    # 卡片边框
FG = "#e8eaf0"        # 正文
SUB = "#8d95a8"       # 次要文字
ACCENT = "#5b9dff"    # 主按钮
ACCENT_DK = "#3f7fe0"
OK = "#49d19b"        # 通过
BAD = "#ff6b6b"       # 失败
WARN = "#f0c04a"      # 进行中

FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
H1 = ("Microsoft YaHei UI", 14, "bold")
SMALL = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 10)

# 「给 AI 用」面板：只放一层薄接入，说明书全文永远在 docs/ai-controller.md 那一份。
# (显示名, 类型, 怎么用)；类型决定生成什么：mcp = 可粘的 JSON，cmd = 自然语言指令，
# chat = 只能喂说明书。
AI_TARGETS = (
    ("Claude Code", "mcp", "粘进项目根的 .mcp.json（或 claude mcp add 加同一段）。"),
    ("Codex", "cmd", "在仓库根目录打开它，把这段贴进对话框，它会读 AGENTS.md。"),
    ("Cursor", "cmd", "贴进对话框；Cursor 也会读 AGENTS.md。"),
    ("Gemini CLI", "mcp", "粘进 .gemini/settings.json（项目或用户目录）的 mcpServers。"),
    ("VS Code Copilot", "mcp", "粘进仓库的 .vscode/mcp.json。"),
    ("Windsurf", "mcp", "粘进 mcp_config.json 的 mcpServers。"),
    ("Cline", "mcp", "粘进 Cline 的 cline_mcp_settings.json。"),
    ("豆包（聊天类）", "chat", "它不能跑命令：把说明书存下来拖进聊天框。"),
)

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


def _write_text(path: str, text: str) -> int:
    """UTF-8（无 BOM）、统一 LF 落盘，返回字节数。

    父目录不存在就建；建不出来或写不进去，抛带中文说明的 RuntimeError——绝不静默失败。
    说明书和「给 AI 用」面板共用这一份落盘逻辑。
    """
    data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
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


def save_manual(path: str) -> int:
    """把说明书写成 UTF-8（无 BOM、统一 LF）。返回写入字节数。"""
    return _write_text(path, manual_text())


def mcp_server_path() -> str:
    """MCP 服务器的真实绝对路径；找不到（EXE 被拷到别处、身边没有仓库）返回空串。

    打包后 sys._MEIPASS 是**临时解压目录**，退出即删——绝不能把那里的路径交给客户端。
    所以在冻结状态下依次找：EXE 旁边 → EXE 旁边的 esp32p4-hid-agent/ → 当前目录。
    """
    if not getattr(sys, "frozen", False):
        p = os.path.join(ROOT, "tools", "mcp_server.py")
        return p if os.path.isfile(p) else ""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for base in (exe_dir, os.path.join(exe_dir, "esp32p4-hid-agent"), os.getcwd()):
        p = os.path.join(base, "tools", "mcp_server.py")
        if os.path.isfile(p):
            return p
    return ""


def ai_payload(name: str) -> str:
    """给指定 AI 用的那段：MCP 类是可粘的 JSON，命令类/聊天类是中文指令。"""
    target = next((t for t in AI_TARGETS if t[0] == name), AI_TARGETS[0])
    kind = target[1]
    if kind == "mcp":
        server_py = mcp_server_path()
        if not server_py:                 # 身边没有仓库：不能给一段假路径
            return ("MCP 需要一个真实的 tools/mcp_server.py 路径，这个窗口旁边没有仓库。\n\n"
                    "两种办法：\n"
                    "1. 把整个仓库（或用 git clone / 下载的压缩包）放在旁边，"
                    "从仓库里重新打开这个程序，这一段就会自动填好；\n"
                    "2. 不用 MCP，改用「命令类」那段（Codex / Cursor 的写法）："
                    "让 AI 在仓库根目录里跑 python tools/connector.py。\n\n"
                    "窗口版本身不受影响：这些按钮都能用。")
        server = {"command": "python", "args": [server_py]}
        if not getattr(sys, "frozen", False):
            # 源码运行时用"正在跑这个窗口的那个 python"——它一定装了依赖，
            # 也不受 PATH 里 python 是 Microsoft Store 占位符的影响。
            server["command"] = sys.executable
        key = "esp32p4-hid-board"
        if name == "VS Code Copilot":       # VS Code 的格式是 servers，不是 mcpServers
            cfg = {"servers": {key: {"type": "stdio", **server}}}
        else:
            cfg = {"mcpServers": {key: server}}
        return json.dumps(cfg, ensure_ascii=False, indent=2)
    if kind == "chat":
        return ("把 docs/ai-controller.md 拖进聊天框，然后让它按说明书干活。\n\n"
                "它只能出主意、不能直接按键：要动手时把步骤发回来，"
                "在本窗口的「运行作业」里执行。")
    return ("读 AGENTS.md，按里面的规矩用 python tools/connector.py 操作这块板子；"
            "禁止改 firmware/。\n\n"
            "先跑 python tools/agent.py --services 确认板子和视觉服务在不在，"
            "再一步一核对地做；不确定就先问人。")


def _short(text, n: int = 90) -> str:
    """把异常/日志压成一行短句：窗口里只放人话，长堆栈留给日志文件。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n] + "…"


# 自检五项：过了是什么、没过怎么办（用户看不懂 JSON，只看得懂这个）
_CHECK_ADVICE = {
    "屏幕截取": "屏幕没抓到。先看看是不是锁屏了、或者远程桌面里跑；再点一次自检。",
    "OCR 引擎": "认字的引擎没起来。装了依赖的机器上跑 python tools\\visiond.py，或 python tools\\visiond.py --start。",
    "板子连接": "板子连不上。两个 USB 口都要插（UART 给指令、高速 Type-C 给键鼠）；再点「服务状态」看 hub 和板子 TCP。",
    "指针校准": "没有指针校准文件，鼠标定位会不准（不影响按键和读字）。要校准跑 python tools\\calibrate-pointer.py。",
    "视觉服务": "视觉服务没在跑，只影响看图问答（see），键鼠和认字照常。第一次用会自动拉起。",
}


def selfcheck_summary(obj: dict) -> str:
    """把自检 JSON 翻成人话：一句结论 + 五项各一行 ✓/✗ + 没过的怎么办。"""
    checks = obj.get("checks") or []
    bad = [c for c in checks if not c.get("ok")]
    hard_bad = [c for c in bad if c.get("hard", True)]
    if not bad:
        head = "全部通过 —— 屏幕、认字、板子、指针、视觉五项都正常，现在就能用。"
    elif not hard_bad:
        head = "能用。有一项小问题，不影响键鼠：%s。" % "、".join(
            c.get("check", "?") for c in bad)
    else:
        head = "没过：%s。" % "、".join(c.get("check", "?") for c in hard_bad)
    lines = [head]
    for c in checks:
        name = c.get("check", "?")
        if c.get("ok"):
            ms = c.get("ms")
            lines.append("  %s   ✓%s" % (name.ljust(8, "　"), ("  %.2f 秒" % (ms / 1000.0)) if ms else ""))
        else:
            lines.append("  %s   ✗  %s" % (name.ljust(8, "　"), _short(c.get("err", "没成功"), 60)))
    for c in bad:
        advice = _CHECK_ADVICE.get(c.get("check", ""))
        if advice:
            lines.append("  · %s" % advice)
    return "\n".join(lines)


def job_summary(obj: dict) -> str:
    """把作业结果翻成人话：跑没跑成、几步、多久、错在第几步。"""
    if obj.get("ok"):
        steps = obj.get("steps") or []
        ms = sum(int(s.get("ms") or 0) for s in steps)
        ops = " → ".join(str(s.get("op", "?")) for s in steps[:6])
        if len(steps) > 6:
            ops += " → …"
        return "跑完了：%d 步（%s），共 %.2f 秒。" % (len(steps), ops, ms / 1000.0)
    return "没跑成：%s" % _short(obj.get("err") or "（没说原因）", 160)


def job_details(obj: dict, limit: int = 12) -> str:
    """只读作业把"读到了什么"顺手贴几行：人要把这段回给聊天 AI，比 7000 字 JSON 好粘。"""
    lines = []
    for s in obj.get("steps") or []:
        rows = s.get("rows") or []
        if rows:
            lines.append("  " + " | ".join(str(r) for r in rows[:limit]))
            if len(rows) > limit:
                lines.append("  …还有 %d 行，点「复制详情（原始 JSON）」拿完整的" % (len(rows) - limit))
        elif s.get("text"):
            lines.append("  " + _short(s["text"], 200))
    return "\n".join(lines)


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
        self.geometry("1080x820")
        self.minsize(960, 700)
        self.q: queue.Queue = queue.Queue()
        self._busy = False          # 一次只跑一个动作：两个作业抢同一个串口会互相打乱
        self._pending = 0
        self._last_raw = ""         # 上一次的原始 JSON，给「复制详情」用
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain)
        self._status("就绪", OK, "点「自检」看这块板子现在能不能用；要看别的 AI 怎么接，点「给 AI 用…」。")
        self._log("就绪。板子是手和眼，这里是遥控器。")
        self._log("第一次用，建议顺序：自检 → 服务状态。两项都通过就能干活。")
        self._log("要把控制器交给别的 AI：点「保存 AI 说明书…」，或「给 AI 用…」拿一段配置。")

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
    def _button(self, parent, text, cmd, primary: bool = False, width=None):
        """扁平按钮 + 悬停变色。tk 没有圆角，扁平 + 留白就够体面了。"""
        bg = ACCENT if primary else FIELD
        fg = "#0d1117" if primary else FG
        hot = ACCENT_DK if primary else "#343945"
        b = tk.Button(parent, text=text, command=cmd,
                      font=FONT_B if primary else FONT,
                      bg=bg, fg=fg, activebackground=hot, activeforeground=fg,
                      disabledforeground=SUB, relief=tk.FLAT, bd=0,
                      padx=16, pady=8, cursor="hand2", highlightthickness=0)
        if width:
            b.configure(width=width)

        def enter(_e):
            if str(b["state"]) != "disabled":
                b.configure(bg=hot)

        def leave(_e):
            if str(b["state"]) != "disabled":
                b.configure(bg=bg)

        b.bind("<Enter>", enter)
        b.bind("<Leave>", leave)
        b._bg_normal = bg                 # 恢复禁用态时用
        return b

    def _card(self, parent, title: str, hint: str = "") -> tk.Frame:
        """一张卡片：细边框 + 小标题，返回可往里放东西的 body。"""
        wrap = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(wrap, bg=CARD)
        head.pack(fill=tk.X, padx=16, pady=(12, 0))
        tk.Label(head, text=title, bg=CARD, fg=FG, font=FONT_B).pack(side=tk.LEFT)
        if hint:
            tk.Label(head, text=hint, bg=CARD, fg=SUB, font=SMALL).pack(side=tk.LEFT, padx=(10, 0))
        body = tk.Frame(wrap, bg=CARD)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(10, 14))
        return wrap, body

    def _build(self) -> None:
        self.configure(bg=BG)

        # 顶栏：左边标题，右边版本（不抢眼）
        head = tk.Frame(self, bg=BG)
        head.pack(side=tk.TOP, fill=tk.X, padx=18, pady=(14, 6))
        tk.Label(head, text="板子控制器", bg=BG, fg=FG, font=H1).pack(side=tk.LEFT)
        tk.Label(head, text="板子是手和眼 · 这里是遥控器", bg=BG, fg=SUB,
                 font=SMALL).pack(side=tk.LEFT, padx=(12, 0), pady=(4, 0))
        tk.Label(head, text="v%s" % _agent().VERSION, bg=BG, fg=SUB,
                 font=SMALL).pack(side=tk.RIGHT)

        # 底部状态：先 pack 再 pack 可扩展的 body，否则会被挤出去（pack 顺序的坑）
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=(6, 10))
        self._dot = tk.Label(bottom, text="●", fg=OK, bg=BG, font=SMALL)
        self._dot.pack(side=tk.LEFT)
        self.status = tk.Label(bottom, text="", anchor="w", fg=SUB, bg=BG, font=SMALL)
        self.status.pack(side=tk.LEFT, fill=tk.X, padx=(6, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=18)

        # 三行用 grid：中间那行（结果）吃剩余空间，上下两行保自然高度。
        # 用 pack 时窗口一紧就会把最后 pack 的按钮挤成一条缝，grid 的 weight 没这个毛病。
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)

        # 第一排：左边"交给 AI"（主操作），右边"检查"
        row1 = tk.Frame(body, bg=BG)
        row1.grid(row=0, column=0, sticky="ew")
        row1.columnconfigure(0, weight=3, uniform="r1")
        row1.columnconfigure(1, weight=2, uniform="r1")

        c_ai, b_ai = self._card(row1, "把说明书交给 AI", "存成 .md 拖给它，或直接给配置")
        c_ai.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.btn_save = self._button(b_ai, "保存 AI 说明书…（下载）", self.on_save_manual, primary=True)
        self.btn_save.pack(side=tk.TOP, fill=tk.X)
        line = tk.Frame(b_ai, bg=CARD)
        line.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        self.btn_copy = self._button(line, "复制说明书", self.on_copy_manual)
        self.btn_copy.pack(side=tk.LEFT)
        self.btn_ai = self._button(line, "给 AI 用…", self.on_ai_panel)
        self.btn_ai.pack(side=tk.LEFT, padx=(8, 0))

        c_ck, b_ck = self._card(row1, "检查", "只看结论，不看代码")
        c_ck.grid(row=0, column=1, sticky="nsew")
        self.btn_check = self._button(b_ck, "自检", self.on_selfcheck)
        self.btn_check.pack(side=tk.LEFT)
        self.btn_svc = self._button(b_ck, "服务状态", self.on_services)
        self.btn_svc.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_read = self._button(b_ck, "看说明书", self.on_read_manual)
        self.btn_read.pack(side=tk.LEFT, padx=(8, 0))

        # 输出（占大头）
        c_out, b_out = self._card(body, "结果", "✓ 通过  ✗ 没过  … 正在跑")
        c_out.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.out = scrolledtext.ScrolledText(b_out, height=8, width=1, wrap=tk.WORD,
                                            font=MONO, state=tk.DISABLED,
                                            bg=BG2, fg=FG, insertbackground=FG,
                                            relief=tk.FLAT, bd=0, padx=10, pady=8)
        self.out.pack(fill=tk.BOTH, expand=True)
        for tag, color in (("ok", OK), ("bad", BAD), ("busy", WARN), ("dim", SUB)):
            self.out.tag_configure(tag, foreground=color)
        self.out.tag_configure("head", foreground=FG, font=FONT_B)

        # 作业
        c_job, b_job = self._card(body, "用板子运行作业（JSON）", "按下去板子真的会动手")
        c_job.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        jrow = tk.Frame(b_job, bg=CARD)
        jrow.pack(side=tk.TOP, fill=tk.X)
        self.job_in = tk.Text(jrow, height=3, width=1, wrap=tk.WORD, font=MONO,
                              bg=BG2, fg=FG, insertbackground=FG, relief=tk.FLAT,
                              bd=0, padx=10, pady=8)
        self.job_in.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.job_in.insert("1.0", '{"steps":[{"op":"title","exe":"msedge"}]}')
        self.btn_run = self._button(jrow, "用板子运行", self.on_run, primary=True)
        self.btn_run.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        tools = tk.Frame(b_job, bg=CARD)
        tools.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        self.btn_detail = self._button(tools, "复制详情（原始 JSON）", self.on_copy_detail)
        self.btn_detail.pack(side=tk.LEFT)
        btn_clear = self._button(tools, "清空输出", self.clear)
        btn_clear.pack(side=tk.LEFT, padx=(8, 0))

    # -- 输出
    def _log(self, text: str, tag: str = "") -> None:
        mark = {"ok": "✓ ", "bad": "✗ ", "busy": "… "}.get(tag, "")
        self.out.configure(state=tk.NORMAL)
        self.out.insert(tk.END, "%s%s\n" % (mark, text), tag or ())
        self.out.see(tk.END)
        self.out.configure(state=tk.DISABLED)

    def _log_block(self, text: str, tag: str = "") -> None:
        """多行块：第一行按 tag 上色，其余行用暗色，读起来像一张结果卡。"""
        lines = text.splitlines() or [""]
        self.out.configure(state=tk.NORMAL)
        for i, ln in enumerate(lines):
            t = tag if i == 0 else ("dim" if not ln.strip().startswith("·") else "dim")
            self.out.insert(tk.END, "%s%s\n" % ("✓ " if i == 0 and tag == "ok" else
                                                "✗ " if i == 0 and tag == "bad" else "", ln), t)
        self.out.see(tk.END)
        self.out.configure(state=tk.DISABLED)

    def _status(self, text: str, color: str = SUB, tip: str = "") -> None:
        """底部状态栏：一个色点 + 一句话。tip 是更长的解释，鼠标停上去能看到。"""
        try:
            self._dot.configure(fg=color)
            self.status.configure(text=text)
            self.configure(cursor="arrow")
            self.status.configure(text=text)
            if tip:
                self.status.configure(text="%s —— %s" % (text, tip))
        except tk.TclError:
            pass

    def clear(self) -> None:
        self.out.configure(state=tk.NORMAL)
        self.out.delete("1.0", tk.END)
        self.out.configure(state=tk.DISABLED)

    def _enable(self, on: bool) -> None:
        state = tk.NORMAL if on else tk.DISABLED
        for b in (self.btn_check, self.btn_svc, self.btn_run):
            b.configure(state=state)
            b.configure(bg=b._bg_normal if on else "#22262e")

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
        self._status("%s…（在后台跑，界面不会卡）" % label, WARN)

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
                self._log_block(text, "ok" if ok else "bad")
                self._status("%s：%s" % (label, "通过" if ok else "没通过"),
                             OK if ok else BAD)
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
        def work():
            text = selfcheck_text()          # 内部是 JSON，给机器看
            try:
                obj = json.loads(text)
            except Exception:                # noqa: BLE001 — 不是 JSON 就原样给人看
                return False, text
            self._last_raw = text
            return bool(obj.get("ok")), selfcheck_summary(obj)
        self._bg("自检", work)

    def on_services(self) -> None:
        def work():
            s = _agent().services()
            up = any(x["up"] for x in (s["hub"], s["board_tcp"], s["vision"]))
            self._last_raw = json.dumps(s, ensure_ascii=False, indent=2)
            return up, services_text(s)
        self._bg("服务状态", work)

    def on_copy_detail(self) -> None:
        """把上一次的原始 JSON 放进剪贴板：平时藏起来，真要排错时才看。"""
        if not self._last_raw:
            self._log("还没有可复制的详情。先点「自检」或「用板子运行」。")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._last_raw)
            self._log("上一次的原始结果（%d 字符）已复制到剪贴板。" % len(self._last_raw), "ok")
        except tk.TclError:
            self._log("复制失败：剪贴板被别的程序占着。", "bad")

    def on_ai_panel(self) -> tk.Toplevel:
        """把控制器交给别的 AI：选一个，复制/存下它要的配置或指令。

        子窗口不进 mainloop、不最大化也不动别的窗口；重复点只把已有的抬到前面。
        """
        old = getattr(self, "_ai_win", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.deiconify()
                    old.lift()
                    return old
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        win.title("给别的 AI 用")
        win.geometry("820x620")
        win.resizable(False, False)
        win.configure(bg=BG)
        win.transient(self)               # 跟着主窗口，不抢别的程序
        self._ai_win = win

        body = tk.Frame(win, bg=BG, padx=16, pady=12)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text="把控制器交给别的 AI：选一个，复制走这一段（或存成文件）。",
                 fg=FG, bg=BG, font=FONT, anchor="w").pack(fill=tk.X)
        tk.Label(body, text="MCP 类粘进它的 MCP 配置；命令类直接贴进对话框；"
                            "聊天类把说明书拖进去。",
                 fg=SUB, bg=BG, font=FONT, anchor="w", justify=tk.LEFT,
                 wraplength=760).pack(fill=tk.X, pady=(4, 10))

        pick = tk.Frame(body, bg=BG)
        pick.pack(fill=tk.X)
        tk.Label(pick, text="给谁用：", fg=FG, bg=BG, font=FONT).pack(side=tk.LEFT)
        names = [t[0] for t in AI_TARGETS]
        var = tk.StringVar(master=win, value=names[0])
        menu = tk.OptionMenu(pick, var, *names)
        menu.configure(font=FONT, bg=BG2, fg=FG, activebackground=BG2,
                       activeforeground=FG, highlightthickness=0, bd=0)
        menu["menu"].configure(font=FONT, bg=BG2, fg=FG,
                               activebackground=SUB, activeforeground=BG)
        menu.pack(side=tk.LEFT)

        hint = tk.Label(body, text="", fg=SUB, bg=BG, font=FONT, anchor="w",
                        justify=tk.LEFT, wraplength=760)
        hint.pack(fill=tk.X, pady=(6, 8))

        box = scrolledtext.ScrolledText(body, height=18, width=1, wrap=tk.WORD,
                                        font=MONO, bg=BG2, fg=FG, insertbackground=FG,
                                        relief=tk.FLAT, state=tk.DISABLED)
        box.pack(fill=tk.BOTH, expand=True)

        status = tk.Label(body, text="", fg=SUB, bg=BG, font=FONT, anchor="w",
                          justify=tk.LEFT, wraplength=760)
        hints = {t[0]: t[2] for t in AI_TARGETS}
        kinds = {t[0]: t[1] for t in AI_TARGETS}

        def current_text() -> str:
            return box.get("1.0", tk.END).strip("\n")

        def show(*_) -> None:
            box.configure(state=tk.NORMAL)
            box.delete("1.0", tk.END)
            box.insert("1.0", ai_payload(var.get()))
            box.configure(state=tk.DISABLED)
            hint.configure(text="怎么用：" + hints.get(var.get(), ""))
            status.configure(text="")

        def copy_it() -> None:
            if self._busy:
                status.configure(text="主窗口还有动作在跑，等它结束再复制。")
                return
            text = current_text()
            if not text:
                status.configure(text="这段是空的，先选一个 AI。")
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update_idletasks()
            except Exception as e:                       # noqa: BLE001
                status.configure(text="复制失败：%s" % e)
                return
            status.configure(text="已复制 %d 字符，去粘给 %s。" % (len(text), var.get()))
            self._log("已复制给 %s 用的内容（%d 字符）。" % (var.get(), len(text)), "ok")

        def save_it() -> None:
            if self._busy:
                status.configure(text="主窗口还有动作在跑，等它结束再保存。")
                return
            text = current_text()
            ext = ".json" if kinds.get(var.get()) == "mcp" else ".txt"
            path = filedialog.asksaveasfilename(
                parent=win, title="保存给 %s 用的内容" % var.get(),
                initialfile="给-AI-用" + ext, defaultextension=ext,
                filetypes=[("JSON 配置", "*.json"), ("全部文件", "*.*")] if ext == ".json"
                          else [("文本文件", "*.txt"), ("全部文件", "*.*")])
            if not path:
                status.configure(text="已取消保存。")
                return
            try:
                n = _write_text(path, text)
            except Exception as e:                       # noqa: BLE001
                messagebox.showerror("保存失败", str(e), parent=win)
                status.configure(text=str(e))
                return
            status.configure(text="已保存：%s（%d 字节，UTF-8 无 BOM）" % (path, n))
            self._log("给 %s 用的内容已保存：%s（%d 字节）" % (var.get(), path, n), "ok")

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill=tk.X, pady=(8, 0))
        tk.Button(btns, text="复制这段", width=12, font=FONT,
                  command=copy_it).pack(side=tk.LEFT)
        tk.Button(btns, text="保存成文件", width=12, font=FONT,
                  command=save_it).pack(side=tk.LEFT, padx=(8, 0))
        status.pack(fill=tk.X, pady=(8, 0))

        var.trace_add("write", show)
        show()

        # 无窗口自测/排障用：直接拿到控件和动作，不必点按钮
        win.ai_var, win.ai_text, win.ai_status = var, box, status
        win.ai_copy, win.ai_save = copy_it, save_it
        return win

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

        def work():
            ok, text = run_job_text(raw)
            try:
                obj = json.loads(text)
            except Exception:                # noqa: BLE001
                return ok, text
            self._last_raw = text
            head = job_summary(obj)
            detail = job_details(obj) if ok else ""
            return ok, (head + "\n" + detail if detail else head)
        self._bg("运行作业", work)


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
                                "btn_read", "btn_run", "btn_ai") if hasattr(a, w)]
        finally:
            a.destroy()

    def probe_ai_payloads():
        # 打包后 _MEIPASS 是临时解压目录，退出即删：给客户端的路径绝不能指向那里
        p = mcp_server_path()
        for t in AI_TARGETS:
            text = ai_payload(t[0])
            assert "_MEI" not in text, "%s 的配置指向了临时解压目录" % t[0]
        if p:
            assert os.path.isfile(p), "给的 mcp_server.py 不存在：%s" % p
        return "%d 个目标%s" % (len(AI_TARGETS), "，仓库路径可用" if p else "，身边无仓库（会给出替代说明）")

    def probe_speech():
        """结论要人话、而且必须和 JSON 一致：全过、只有软项挂、硬项挂，三种都说对。"""
        base = [{"check": "屏幕截取", "ok": True, "ms": 30},
                {"check": "OCR 引擎", "ok": True, "ms": 120},
                {"check": "板子连接", "ok": True, "ms": 8},
                {"check": "指针校准", "ok": True, "ms": 0},
                {"check": "视觉服务", "ok": True, "ms": 3}]
        s = selfcheck_summary({"ok": True, "checks": base})
        assert "全部通过" in s and s.count("✓") == 5, s
        soft = [dict(c) for c in base] + []
        soft[-1] = {"check": "视觉服务", "ok": False, "err": "没在跑", "hard": False}
        s2 = selfcheck_summary({"ok": True, "checks": soft})
        assert "能用" in s2 and "视觉服务" in s2, s2
        hard = [dict(c) for c in base]
        hard[2] = {"check": "板子连接", "ok": False, "err": "连不上 COM7", "hard": True}
        s3 = selfcheck_summary({"ok": False, "checks": hard})
        assert "没过" in s3 and "板子连接" in s3 and "怎么办" not in s3, s3
        assert "✗" in s3 and "两个 USB 口" in s3, s3
        j = job_summary({"ok": True, "steps": [{"op": "title", "ms": 100}, {"op": "ocr", "ms": 40}]})
        assert "跑完了" in j and "2 步" in j and "title → ocr" in j, j
        j2 = job_summary({"ok": False, "err": "第 2 步 click 失败：x out of range"})
        assert "没跑成" in j2 and "第 2 步" in j2, j2
        d = job_details({"ok": True, "steps": [{"op": "ocr", "rows": ["A", "B", "C"]}]})
        assert "A | B | C" in d, d
        d2 = job_details({"ok": True, "steps": [{"op": "ocr", "rows": ["x"] * 30}]})
        assert "还有 18 行" in d2, d2
        return "自检/作业结论都是人话且与 JSON 一致"

    for name, fn in (
        ("说明书可读（字符数）", lambda: len(manual_text())),
        ("说明书含禁止改固件", lambda: "禁止改固件" in manual_text()),
        ("保存说明书（字节数）", lambda: save_manual(tmp)),
        ("能力清单 op_count", lambda: json.loads(caps_text())["op_count"]),
        ("窗口能构建（控件名）", probe_app),
        ("给 AI 用：不出现临时目录路径", probe_ai_payloads),
        ("自检/作业只说人话", probe_speech),
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
