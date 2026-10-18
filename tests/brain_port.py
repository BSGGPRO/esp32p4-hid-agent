#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""brain 串口解析 `_resolve_uart_port` 的单元测试：只测纯函数，不开串口、不建 Brain。

跑法：`python tests/brain_port.py`（CI 上也是这一条）。
做法：monkeypatch detect_port（同时罩住 brain 模块里的名字和 transport_uart 里的实现），
按契约断言 auto 的三条路径、显式串口原样返回且不探测、大小写/空白变体。

覆盖（对应 task-21 契约）：
  1. _resolve_uart_port 能 import
  2. "auto" + detect → {"com":"COM5"} 返回 COM5
  3. "auto" + detect → None 回退 COM7
  4. "auto" + detect 抛异常 → 回退 COM7，不炸
  5. 显式 "COM4" 原样返回，且 detect_port 一次都不调
  6. "AUTO" / "auto" / ""（带空白）走自动；"com4" 显式也能用（不断言大小写）
"""
from __future__ import annotations

import contextlib
import faulthandler
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pc"))

# Windows runner 的控制台默认 cp1252，打不出中文会抛 UnicodeEncodeError——测试先崩在 print 上
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 总时长兜底：纯函数测试不该慢，超过 60 秒就是哪里卡死了
faulthandler.dump_traceback_later(60, exit=True)

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        failures.append(name)


# 让「回退默认口」可预测：环境变量不该影响单测
_saved_env = {k: os.environ.pop(k) for k in ("HID_PORT",) if k in os.environ}

try:
    import brain  # noqa: E402
    import transport_uart  # noqa: E402
except Exception as e:  # noqa: BLE001
    check("能 import pc/brain.py", False, "%s: %s" % (type(e).__name__, e))
    print("-" * 60)
    print("失败 1 项: 能 import pc/brain.py")
    sys.exit(1)

check("能 import pc/brain.py（没有开端口的副作用）", True)

resolve = getattr(brain, "_resolve_uart_port", None)
check("_resolve_uart_port 存在", callable(resolve),
      "brain 里没有这个函数（代码部还没加？）")
if not callable(resolve):
    for k, v in _saved_env.items():
        os.environ[k] = v
    print("-" * 60)
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)


class DetectStub:
    """假 detect_port：记录调用次数，可返回给定值或抛异常。"""

    def __init__(self, result=None, exc: BaseException | None = None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


@contextlib.contextmanager
def fake_detect(stub):
    """把 detect_port 同时替换到 brain（from 导入）和 transport_uart（模块内调用）两处。"""
    targets = []
    for mod in (brain, transport_uart):
        if hasattr(mod, "detect_port"):
            targets.append((mod, mod.detect_port))
            mod.detect_port = stub
    try:
        yield
    finally:
        for mod, orig in targets:
            mod.detect_port = orig


# ------------------------------------------------------------------ 2) auto → COM5
stub = DetectStub({"com": "COM5"})
with fake_detect(stub):
    got = resolve("auto")
check("auto + detect 找到 COM5 → 返回 COM5", got == "COM5", repr(got))
check("auto 确实调用了 detect_port", stub.calls >= 1, "calls=%d" % stub.calls)

# ------------------------------------------------------------------ 3) detect 返回 None → COM7
stub = DetectStub(None)
with fake_detect(stub):
    got = resolve("auto")
check("detect 返回 None → 回退 COM7", got == "COM7", repr(got))
check("回退前确实探测过", stub.calls >= 1, "calls=%d" % stub.calls)

# ------------------------------------------------------------------ 4) detect 抛异常 → COM7，不炸
stub = DetectStub(exc=RuntimeError("detect boom"))
raised = None
got = None
with fake_detect(stub):
    try:
        got = resolve("auto")
    except BaseException as e:  # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
check("detect 抛异常不炸，回退 COM7", raised is None and got == "COM7",
      "抛了 %s / 返回 %r" % (raised, got))

# ------------------------------------------------------------------ 5) 显式串口原样、不探测
stub = DetectStub({"com": "COM5"})
raised = None
got = None
with fake_detect(stub):
    try:
        got = resolve("COM4")
    except BaseException as e:  # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
check("显式 COM4 原样返回", raised is None and got == "COM4",
      "抛了 %s / 返回 %r" % (raised, got))
check("显式 COM4 不调用 detect_port", stub.calls == 0, "calls=%d" % stub.calls)

# ------------------------------------------------------------------ 6) 大小写 / 空白自动 + 小写显式
for label, value in (("AUTO", "AUTO"), ("auto", "auto"), ("空字符串", ""),
                     ("带空白的 auto", "  auto  ")):
    stub = DetectStub({"com": "COM9"})
    raised = None
    got = None
    with fake_detect(stub):
        try:
            got = resolve(value)
        except BaseException as e:  # noqa: BLE001
            raised = "%s: %s" % (type(e).__name__, e)
    check("%r 走自动（detect 到 COM9）" % (value,),
          raised is None and got == "COM9" and stub.calls >= 1,
          "抛了 %s / 返回 %r / calls=%d" % (raised, got, stub.calls))

stub = DetectStub({"com": "COM5"})
raised = None
got = None
with fake_detect(stub):
    try:
        got = resolve("com4")
    except BaseException as e:  # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
check("显式 com4 也能用（原样或大写都认，大小写不写死）",
      raised is None and isinstance(got, str) and got.upper() == "COM4",
      "抛了 %s / 返回 %r" % (raised, got))
check("显式 com4 不调用 detect_port", stub.calls == 0, "calls=%d" % stub.calls)

for k, v in _saved_env.items():
    os.environ[k] = v

print("-" * 60)
if failures:
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("全部通过")
