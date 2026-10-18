"""输入输出边界测试：专测 tools/agent_ui.py 的保存/读取/异常/无控制台场景。

跑法：`python tests/ui_io.py`

功能冒烟归别的测试（tests/smoke.py 那条线），这里只钉边界：
路径里的中文/撇号/空格、缺失目录、写目录目标、覆盖写、BOM、换行、
坏输入、--windowed（stdout=None）、大文件。每条打印期望 vs 实际。
"""
from __future__ import annotations

import faulthandler
import io
import os
import shutil
import sys
import tempfile
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# --windowed 构建里 stdout 可能是 None；控制台是 cp936，中文 print 会先崩在输出上
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 总时长硬上限：单条命令/网络/窗口异常时别把 CI 拖死（要求 < 60 秒）
faulthandler.dump_traceback_later(55, exit=True)

passed = 0
failures: list[str] = []


def check(name: str, ok: bool, expect: str, actual: str) -> None:
    global passed
    print(("PASS  " if ok else "FAIL  ") + name)
    print("      期望: " + expect)
    print("      实际: " + actual)
    if ok:
        passed += 1
    else:
        failures.append(name)


def brief(value, limit: int = 240) -> str:
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[:limit] + "…"


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def norm_newlines(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def call_job(raw: str):
    """跑一份作业文本；把被测函数自己的 print 收走，别搅乱测试输出。"""
    sink = io.StringIO()
    with redirect_stdout(sink), redirect_stderr(sink):
        return ui.run_job_text(raw)


MODULE = ROOT / "tools" / "agent_ui.py"
if not MODULE.exists():
    print("FAIL  被测文件不存在: %s" % MODULE)
    print("      （Lead 还在写；先等 tools/agent_ui.py 出现再跑）")
    sys.exit(1)

try:
    import agent_ui as ui  # noqa: E402
except Exception:  # noqa: BLE001
    print("FAIL  导入 tools/agent_ui.py 失败")
    traceback.print_exc()
    sys.exit(1)

CONTRACT = ("manual_text", "save_manual", "run_job_text",
            "caps_text", "services_text", "log_path")
missing = [n for n in CONTRACT if not callable(getattr(ui, n, None))]
check("契约函数齐全", not missing,
      "六个可调用: " + ", ".join(CONTRACT),
      "缺失: %s" % (missing or "无"))
if missing:
    print("-" * 60)
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)

# ---------------------------------------------------------------- 临时路径
TEMPDIR = Path(tempfile.gettempdir())
SPECIAL_DIR = TEMPDIR / "控制 器's test"          # 中文 + 撇号 + 空格
SPECIAL_FILE = SPECIAL_DIR / "说明书.md"
NEST_ROOT = TEMPDIR / ("ui_io_nested_%d" % os.getpid())
NEST_FILE = NEST_ROOT / "a" / "b" / "c" / "manual.md"   # 目录全用 ASCII，
# 这样异常信息里出现的中文只可能来自代码的说明，而不是路径本身
OVERWRITE_FILE = TEMPDIR / ("ui_io_overwrite_%d.md" % os.getpid())
# 写入目标是一个已存在的目录：管理员和非管理员都必然失败（Linux 上也一样），
# CI 的 windows-latest 是管理员身份，用 System32 会假红，所以不碰系统目录写文件。
BAD_TARGET = Path(os.environ.get("SystemRoot", r"C:\Windows"))

owned_dirs: list[Path] = []
owned_files: list[Path] = [SPECIAL_FILE, OVERWRITE_FILE]


def cleanup() -> None:
    for p in owned_files:
        try:
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    for d in reversed(owned_dirs):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


if not SPECIAL_DIR.exists():
    owned_dirs.append(SPECIAL_DIR)
SPECIAL_DIR.mkdir(parents=True, exist_ok=True)
owned_dirs.append(NEST_ROOT)   # 不管谁建的，这个唯一前缀归本次运行清理

try:
    manual = ui.manual_text()
    manual_bytes = manual.encode("utf-8")

    # 1) 路径含中文 + 撇号 + 空格：保存成功且内容完整
    try:
        written = ui.save_manual(str(SPECIAL_FILE))
        data = SPECIAL_FILE.read_bytes() if SPECIAL_FILE.exists() else b""
        same = norm_newlines(data).rstrip() == norm_newlines(manual_bytes).rstrip()
        ok = (isinstance(written, int) and written > 0 and bool(data) and same)
        check("1 中文+撇号+空格路径可保存且内容完整", ok,
              "int>0 且文件内容与 manual_text() 一致（忽略换行差异）",
              "返回=%r 文件=%d 字节 内容一致=%s" % (written, len(data), same))
    except Exception as e:  # noqa: BLE001
        check("1 中文+撇号+空格路径可保存且内容完整", False,
              "保存成功且内容完整",
              "%s: %s" % (type(e).__name__, e))

    # 2) 多级目录不存在：自动建好并成功，或抛带中文说明的异常
    try:
        ui.save_manual(str(NEST_FILE))
        size = NEST_FILE.stat().st_size if NEST_FILE.exists() else 0
        check("2 多级目录不存在", size > 0,
              "自动建目录并写入成功，或抛带中文说明的异常",
              "自动建目录并写入 %d 字节" % size)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        ok = has_cjk(msg)
        check("2 多级目录不存在", ok,
              "自动建目录并写入成功，或抛带中文说明的异常",
              "%s: %s%s" % (type(e).__name__, brief(msg),
                            "" if ok else "（信息里没有中文说明）"))

    # 3) 失败目标：写一个已存在的目录 → 必须抛可捕获异常，且包装成中文人话
    if not BAD_TARGET.is_dir():
        print("SKIP  3 写入目录目标（%s 不是目录，场景不成立）" % BAD_TARGET)
    else:
        try:
            ui.save_manual(str(BAD_TARGET))
            check("3 写入目录目标必须失败并被包装", False,
                  "抛异常且 str(e) 含「保存失败」",
                  "居然成功返回（目录没被破坏=%s）" % BAD_TARGET.is_dir())
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            ok = "保存失败" in msg
            check("3 写入目录目标必须失败并被包装", ok,
                  "抛异常且 str(e) 含「保存失败」",
                  "%s: %s" % (type(e).__name__, brief(msg)))

    # 4) 覆盖写：第二次必须完整覆盖，不残留旧尾巴
    try:
        OVERWRITE_FILE.write_bytes(b"OLD-TAIL " * 3000)   # 27000 字节诱饵
        ui.save_manual(str(OVERWRITE_FILE))
        data = OVERWRITE_FILE.read_bytes()
        same = norm_newlines(data).rstrip() == norm_newlines(manual_bytes).rstrip()
        leftover = b"OLD-TAIL" in data
        check("4 覆盖写不残留旧内容", same and not leftover,
              "文件内容等于 manual_text()，且不含旧尾巴 OLD-TAIL",
              "%d 字节 内容一致=%s 残留旧尾巴=%s" % (len(data), same, leftover))
    except Exception as e:  # noqa: BLE001
        check("4 覆盖写不残留旧内容", False,
              "文件内容等于 manual_text()，且不含旧尾巴 OLD-TAIL",
              "%s: %s" % (type(e).__name__, e))

    data = SPECIAL_FILE.read_bytes() if SPECIAL_FILE.exists() else b""

    # 5) BOM：前 3 字节不是 EF BB BF
    head = data[:3]
    check("5 无 BOM", head != b"\xef\xbb\xbf",
          "前 3 字节 != EF BB BF",
          "前 3 字节 = %s" % head.hex(" ").upper())

    # 6) 换行不混用，报告实际是 CRLF 还是 LF
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    lone_cr = data.count(b"\r") - crlf
    mixed = (crlf > 0 and lf > 0) or lone_cr > 0
    if crlf and not lf and not lone_cr:
        actual_nl = "CRLF"
    elif lf and not crlf and not lone_cr:
        actual_nl = "LF"
    elif not data:
        actual_nl = "没读到文件"
    else:
        actual_nl = "混合/异常（CRLF=%d 单独LF=%d 单独CR=%d）" % (crlf, lf, lone_cr)
    check("6 换行不混用", bool(data) and not mixed,
          "统一为 CRLF 或 LF 之一",
          actual_nl)

    # 9) 大文件：写出的说明书 > 8000 字节（放在第 5/6 条之后，复用同一份文件）
    size = len(data)
    check("9 说明书大于 8000 字节", size > 8000,
          "> 8000 字节", "%d 字节" % size)

    # 7) run_job_text 的三种坏/空输入
    try:
        res = call_job("hello")
        ok = (isinstance(res, (tuple, list)) and len(res) == 2
              and res[0] is False and has_cjk(str(res[1])))
        check("7a 非 JSON 输入返回 (False, 中文)", ok,
              "(False, 含中文的提示)",
              brief(res))
    except Exception as e:  # noqa: BLE001
        check("7a 非 JSON 输入返回 (False, 中文)", False,
              "(False, 含中文的提示)，不许抛异常",
              "抛了 %s: %s" % (type(e).__name__, e))

    try:
        res = call_job("[1,2]")
        ok = isinstance(res, (tuple, list)) and len(res) == 2 and isinstance(res[0], bool)
        check("7b JSON 但非对象不抛未捕获异常", ok,
              "返回 (bool, str)，不许抛异常",
              brief(res))
    except Exception as e:  # noqa: BLE001
        check("7b JSON 但非对象不抛未捕获异常", False,
              "返回 (bool, str)，不许抛异常",
              "抛了 %s: %s" % (type(e).__name__, e))

    try:
        res = call_job('{"steps": []}')
        ok = isinstance(res, (tuple, list)) and len(res) == 2 and isinstance(res[0], bool)
        check("7c 空作业不崩", ok,
              "返回 (bool, str)，不许抛异常（结果 True/False 都行）",
              brief(res))
    except Exception as e:  # noqa: BLE001
        check("7c 空作业不崩", False,
              "返回 (bool, str)，不许抛异常（结果 True/False 都行）",
              "抛了 %s: %s" % (type(e).__name__, e))

    # 8) --windowed：stdout/stderr 为 None 时调用不许抛 AttributeError
    old_out, old_err = sys.stdout, sys.stderr
    results: dict[str, str] = {}
    try:
        sys.stdout = None
        sys.stderr = None
        for fname in ("caps_text", "manual_text", "services_text"):
            try:
                value = getattr(ui, fname)()
                results[fname] = ("空返回值" if not isinstance(value, str) or not value
                                  else "ok(%d 字符)" % len(value))
            except Exception as e:  # noqa: BLE001
                results[fname] = "%s: %s" % (type(e).__name__, e)
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    check("8 stdout=None 下 caps/manual/services 不抛", all(
        v.startswith("ok(") for v in results.values()),
        "三者都返回非空字符串，不许抛 AttributeError",
        "；".join("%s=%s" % (k, brief(v, 80)) for k, v in results.items()))

    # 契约里的 log_path()：至少返回可用的非空字符串（窗口版会把日志写在哪儿）
    try:
        lp = ui.log_path()
        ok = isinstance(lp, str) and bool(lp.strip())
        check("log_path 返回非空字符串", ok,
              "非空 str", brief(lp))
    except Exception as e:  # noqa: BLE001
        check("log_path 返回非空字符串", False,
              "非空 str", "%s: %s" % (type(e).__name__, e))
finally:
    cleanup()

print("-" * 60)
print("通过 %d 项" % passed)
if failures:
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("全部通过")
