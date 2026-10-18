"""窗口版界面冒烟测试：只测 tools/agent_ui.py 的纯函数和窗口构建。

跑法：`python tests/ui_smoke.py`（CI 上也是这一条）。
不需要板子、不进 mainloop、跑完销毁窗口——不会在用户屏幕上留东西。
真机回归（点按钮看效果）不在这里做。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# Windows runner 的控制台默认 cp1252，打不出中文会抛 UnicodeEncodeError——测试先崩在 print 上
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        failures.append(name)


def cjk(text) -> bool:
    return isinstance(text, str) and bool(re.search(r"[\u4e00-\u9fff]", text))


def norm(text: str) -> str:
    """换行规范化：CRLF/CR 都当成 LF，比较内容别被 Windows 换行坑了。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


try:
    import agent_ui  # noqa: E402
except Exception as e:  # noqa: BLE001
    print("FAIL  导入 tools/agent_ui.py  %s: %s" % (type(e).__name__, e))
    print("-" * 60)
    print("失败 1 项: 导入 tools/agent_ui.py")
    sys.exit(1)

# 0) 契约里的东西都得在（少一个就是漏实现了）
CONTRACT = ("MANUAL_FILE_NAME", "manual_text", "save_manual", "services_text",
            "caps_text", "selfcheck_text", "run_job_text", "log_path", "App")
missing = [n for n in CONTRACT if not hasattr(agent_ui, n)]
check("契约名称齐全", not missing, "缺: %s" % missing)
check("MANUAL_FILE_NAME 是说明书文件名",
      getattr(agent_ui, "MANUAL_FILE_NAME", None) == "AI-控制器说明书.md",
      repr(getattr(agent_ui, "MANUAL_FILE_NAME", None)))

# 1) 说明书正文
manual = ""
try:
    manual = agent_ui.manual_text()
    check("manual_text() 不抛异常", True)
except Exception as e:  # noqa: BLE001
    check("manual_text() 不抛异常", False, "%s: %s" % (type(e).__name__, e))

check("manual_text() 返回非空文本", isinstance(manual, str) and bool(manual.strip()),
      repr(manual[:40]) if isinstance(manual, str) else type(manual).__name__)
check("说明书含『禁止改固件』", "禁止改固件" in manual)
check("说明书含『26 个操作』", "26 个操作" in manual)

# 2) 另存一份说明书：字节数、UTF-8、无 BOM、内容一致
with tempfile.TemporaryDirectory(prefix="ui-smoke-") as td:
    dst = os.path.join(td, "manual.md")
    try:
        n = agent_ui.save_manual(dst)
        raw = Path(dst).read_bytes()
        check("save_manual 写出文件", Path(dst).is_file())
        check("save_manual 返回字节数 == 实际字节数",
              isinstance(n, int) and n == len(raw), "返回=%r 实际=%d" % (n, len(raw)))
        check("save_manual 输出没有 BOM", not raw.startswith(b"\xef\xbb\xbf"))
        text = ""
        try:
            text = raw.decode("utf-8")
            check("save_manual 输出是合法 UTF-8", True)
        except UnicodeDecodeError as e:
            check("save_manual 输出是合法 UTF-8", False, str(e))
        check("save_manual 内容与 manual_text() 一致",
              bool(manual) and norm(text) == norm(manual),
              "写出 %d 字符 vs 正文 %d 字符" % (len(text), len(manual)))
    except Exception as e:  # noqa: BLE001
        check("save_manual() 不抛异常", False, "%s: %s" % (type(e).__name__, e))


# 3/4/5) run_job_text：坏输入只许返回 (False, 中文提示)，不许抛、不许卡死
def run_job(raw: str, timeout: float = 25.0):
    """返回 (值, 错误说明)；错误说明非空表示抛异常或超时。"""
    box: dict = {}

    def work() -> None:
        try:
            box["v"] = agent_ui.run_job_text(raw)
        except BaseException as e:  # noqa: BLE001
            box["e"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "超时 %.0fs（run_job_text 卡住）" % timeout
    if "e" in box:
        return None, "抛异常 " + box["e"]
    return box["v"], ""


def check_job(label: str, raw: str, want_cjk: bool) -> None:
    value, err = run_job(raw)
    if err:
        check("run_job_text(%s) 不抛不卡" % label, False, err)
        check("run_job_text(%s) 返回 (False, 提示)" % label, False, "没拿到返回值")
        return
    check("run_job_text(%s) 返回 (False, 提示)" % label,
          isinstance(value, tuple) and len(value) == 2 and value[0] is False,
          repr(value)[:160])
    msg = value[1] if isinstance(value, tuple) and len(value) == 2 else None
    check("run_job_text(%s) 提示非空字符串" % label,
          isinstance(msg, str) and bool(msg.strip()), repr(msg)[:120])
    if want_cjk:
        check("run_job_text(%s) 提示是中文" % label, cjk(msg), repr(msg)[:120])


check_job("空字符串", "", want_cjk=True)
check_job("非 JSON 文本", "hello", want_cjk=True)
# 未知 op：内部一定会去连板子；连不上也算 False，不断言具体文案
check_job("未知 op", json.dumps({"steps": [{"op": "__ui_smoke_no_such_op__"}]},
                                ensure_ascii=False), want_cjk=False)

# 6) 能力清单
caps = None
try:
    caps_text = agent_ui.caps_text()
    check("caps_text() 返回字符串", isinstance(caps_text, str), type(caps_text).__name__)
    try:
        caps = json.loads(caps_text)
        check("caps_text() 能被 json.loads", True)
    except Exception as e:  # noqa: BLE001
        check("caps_text() 能被 json.loads", False, "%s: %s" % (type(e).__name__, e))
except Exception as e:  # noqa: BLE001
    check("caps_text() 不抛异常", False, "%s: %s" % (type(e).__name__, e))

if caps is not None:
    check("caps_text() op_count == 26",
          isinstance(caps, dict) and caps.get("op_count") == 26,
          "op_count=%r" % (caps.get("op_count") if isinstance(caps, dict) else type(caps).__name__))

# 7) 服务状态
try:
    services = agent_ui.services_text()
    check("services_text() 返回字符串", isinstance(services, str), type(services).__name__)
    check("services_text() 含 hub", isinstance(services, str) and "hub" in services)
except Exception as e:  # noqa: BLE001
    check("services_text() 不抛异常", False, "%s: %s" % (type(e).__name__, e))

# 8) 构建窗口：withdraw（不进 mainloop、不上屏幕），finally 一定销毁
def all_widgets(widget) -> list:
    out = [widget]
    for child in widget.winfo_children():
        out += all_widgets(child)
    return out


root = None
try:
    root = agent_ui.App()
    root.withdraw()
    root.update_idletasks()
    check("App() 能在 withdraw 状态构建", True)
    check("App 是 Tk 窗口", root.winfo_exists() == 1)

    widgets = all_widgets(root)
    classes = [w.winfo_class() for w in widgets]
    check("App 有子控件", len(widgets) > 1, "控件类: %s" % classes)
    check("App 有按钮", any(c in ("Button", "TButton") for c in classes), "控件类: %s" % classes)
    check("App 有文本显示控件",
          any(c in ("Text", "Label", "TLabel") for c in classes), "控件类: %s" % classes)
except Exception as e:  # noqa: BLE001
    check("App() 能在 withdraw 状态构建", False, "%s: %s" % (type(e).__name__, e))
finally:
    if root is not None:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

# 9) 崩溃日志路径
lp = None
try:
    lp = agent_ui.log_path()
    check("log_path() 返回非空字符串", isinstance(lp, str) and bool(lp.strip()), repr(lp)[:120])
except Exception as e:  # noqa: BLE001
    check("log_path() 不抛异常", False, "%s: %s" % (type(e).__name__, e))

if isinstance(lp, str) and lp.strip():
    log_dir = Path(lp).parent
    ok = log_dir.is_dir()
    if not ok:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            ok = log_dir.is_dir()
        except Exception:  # noqa: BLE001
            ok = False
    check("log_path() 所在目录存在或可创建", ok, str(log_dir))

print("-" * 60)
if failures:
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("全部通过")
