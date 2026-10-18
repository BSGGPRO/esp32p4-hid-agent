#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""hub 串口瞬时错重试的单元测试：不需要真板子、真串口、真 hub。

跑法：`python tests/hub_serial.py`（CI 上也是这一条）。
做法：假串口（FakeSerialLink）按脚本逐个返回字节/抛错，手动塞进 BoardLink，
只调 `_reader_loop()`（不调 `start()`，不开端口、不取锁）。
`_reader_loop` 是无限循环：靠假串口抛 permanent 错或置 `_read_stop` 让它退出。

覆盖（对应 task-17 契约）：
  1. SerialError 能带 code / permanent 构造
  2. 瞬时错（permanent=False）之后继续读、serial_open 一直 True
  3. 真拔了（permanent=True）才 close（serial_open 变 False）
  4. 连续 6 次瞬时错兜底也关
  5. --log 真写文件（tempfile，测完删）
"""
from __future__ import annotations

import faulthandler
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(ROOT / "tools"))

# Windows runner 的控制台默认 cp1252，打不出中文会抛 UnicodeEncodeError——测试先崩在 print 上
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 兜底：万一某条用例没退出来，别让 CI 一直挂着（要求 < 60 秒）
faulthandler.dump_traceback_later(75, exit=True)

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        failures.append(name)


import hub  # noqa: E402  （只 import；不开端口，不启动线程）

READER_TIMEOUT = 10.0       # 单条用例里 _reader_loop 最多等多久


def make_serial_error(msg: str, code: int, permanent: bool):
    """按新契约构造 SerialError；旧实现不认字段时退化成普通异常（用例会因此 FAIL 得明明白白）。"""
    try:
        return hub.SerialError(msg, code=code, permanent=permanent)
    except TypeError:
        exc = hub.SerialError(msg)
        exc.code = code
        exc.permanent = permanent
        return exc


class FakeSerialLink:
    """按脚本读的假串口：脚本项是 bytes 就返回，是异常就抛；脚本空了抛 permanent 收尾。"""

    def __init__(self, script=()):
        self._script = list(script)
        self.board = None
        self.closed = False
        self.calls = 0
        self.transient_sent = 0
        self.permanent_sent = 0
        self.serial_open_at_read: list[bool] = []
        self.read_stop_at_read: list[bool] = []

    def open(self) -> None:            # 假串口：真串口才有 open 的开销
        pass

    def close(self) -> None:
        self.closed = True

    def read(self) -> bytes:
        self.calls += 1
        if self.board is not None:
            self.serial_open_at_read.append(self.board.serial_open)
            self.read_stop_at_read.append(self.board._read_stop.is_set())
        if not self._script:
            self.permanent_sent += 1
            raise make_serial_error("脚本耗尽（测试收尾）", 2, True)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            if getattr(item, "permanent", False):
                self.permanent_sent += 1
            else:
                self.transient_sent += 1
            raise item
        return item


def make_board(fake: FakeSerialLink) -> "hub.BoardLink":
    """建一个不取锁、不开端口的 BoardLink，手动接上假串口。"""
    board = hub.BoardLink("COM99", "hub-serial-test", use_lock=False)
    board._serial = fake
    board.serial_open = True
    fake.board = board
    return board


def run_reader(board, fake: FakeSerialLink, timeout: float = READER_TIMEOUT) -> bool:
    """在后台跑 _reader_loop；返回是否在 timeout 内退出了。"""
    orig_cls = hub.SerialLink
    hub.SerialLink = lambda *a, **k: fake        # 万一实现里重开串口，也只拿到假串口
    try:
        t = threading.Thread(target=board._reader_loop, name="fake-reader", daemon=True)
        t.start()
        t.join(timeout)
        alive = t.is_alive()
        if alive:                                 # 兜底收尾：别把死循环留给进程
            board._read_stop.set()
            board._stop.set()
            t.join(2.0)
        return not alive
    finally:
        hub.SerialLink = orig_cls


# ------------------------------------------------------------------ 1) 契约字段
sample = make_serial_error("x", 31, False)
try:
    hub.SerialError("x", code=31, permanent=False)
    check("SerialError 能带 code / permanent 构造", True)
except TypeError as e:
    check("SerialError 能带 code / permanent 构造", False, "TypeError: %s" % e)
check("SerialError 有 permanent 属性", getattr(sample, "permanent", None) is False,
      repr(getattr(sample, "permanent", "没有")))
check("SerialError 有 code 属性", getattr(sample, "code", None) == 31,
      repr(getattr(sample, "code", "没有")))

# ------------------------------------------------------------------ 2) 瞬时错不关串口
transient = make_serial_error("x", 31, False)
permanent = make_serial_error("gone", 2, True)
fake = FakeSerialLink([transient, b'{"ok":true}\n', permanent])
board = make_board(fake)
fed: list[bytes] = []
orig_feed = board._feed_lines


def spy_feed(data: bytes) -> None:
    fed.append(bytes(data))
    orig_feed(data)


board._feed_lines = spy_feed

exited = run_reader(board, fake)
check("瞬时错后 _reader_loop 继续读（permanent 才退出）", exited,
      "还没退出，可能瞬时错后卡死")
check("瞬时错后继续读到了那条 JSON",
      any(b'{"ok":true}' in d for d in fed), "实际喂给解析的数据: %s" % fed)
check("瞬时错期间 serial_open 一直为 True",
      bool(fake.serial_open_at_read) and all(fake.serial_open_at_read),
      "每次 read 时 serial_open=%s" % fake.serial_open_at_read)
check("瞬时错期间 _read_stop 没被置位",
      bool(fake.read_stop_at_read) and not any(fake.read_stop_at_read),
      "每次 read 时 _read_stop=%s" % fake.read_stop_at_read)
check("真拔了之后 serial_open 变 False", board.serial_open is False,
      "serial_open=%r" % board.serial_open)
check("真拔了之后串口 close() 被调用", fake.closed is True)

# ------------------------------------------------------------------ 2b) 读到数据后计数清零
# 「连续」瞬时错才累计：4 次瞬时错 + 一条数据 + 1 次瞬时错 + permanent。
# 如果实现忘了在读到数据时清零，第 5 次累计错就把串口关了，permanent 根本读不到。
script = [make_serial_error("抖 %d" % i, 31, False) for i in range(4)]
script += [b'{"ok":true}\n', make_serial_error("再抖一下", 31, False)]
script += [make_serial_error("gone", 2, True)]
fake = FakeSerialLink(script)
board = make_board(fake)
fed = []
orig_feed = board._feed_lines


def spy_feed2(data: bytes) -> None:
    fed.append(bytes(data))
    orig_feed(data)


board._feed_lines = spy_feed2

exited = run_reader(board, fake)
check("读到数据后瞬时错计数清零（permanent 错误仍被读到）",
      exited and fake.permanent_sent >= 1,
      "瞬时错=%d permanent=%d，可能把累计错当成连续错、提前关了串口"
      % (fake.transient_sent, fake.permanent_sent))
check("数据 + 之后的瞬时错期间串口一直开着",
      bool(fake.serial_open_at_read) and all(fake.serial_open_at_read),
      "每次 read 时 serial_open=%s" % fake.serial_open_at_read)
check("数据 + 之后的瞬时错后仍能读到那条 JSON", any(b'{"ok":true}' in d for d in fed),
      "实际喂给解析的数据: %s" % fed)

# ------------------------------------------------------------------ 3) 真拔了才关
fake = FakeSerialLink([make_serial_error("gone", 2, True)])
board = make_board(fake)
exited = run_reader(board, fake)
check("permanent 错后 _reader_loop 退出", exited)
check("permanent 错后 serial_open=False", board.serial_open is False,
      "serial_open=%r" % board.serial_open)
check("permanent 错后串口 close() 被调用", fake.closed is True)
check("permanent 错不做无谓重试（只读 1 次）", fake.calls == 1, "read 次数=%d" % fake.calls)

# ------------------------------------------------------------------ 4) 连续瞬时错兜底
script = [make_serial_error("瞬时 %d" % i, 31, False) for i in range(20)]
fake = FakeSerialLink(script + [make_serial_error("脚本耗尽", 2, True)])
board = make_board(fake)
exited = run_reader(board, fake, timeout=15.0)
check("连续瞬时错最终触发 close（serial_open=False）", board.serial_open is False,
      "serial_open=%r，瞬时错已发 %d 次" % (board.serial_open, fake.transient_sent))
check("连续瞬时错触发串口 close()", fake.closed is True)
check("重试是有限的：读过 6 次瞬时错内就该关",
      2 <= fake.transient_sent <= 6, "瞬时错次数=%d（期望 2..6）" % fake.transient_sent)
check("连续瞬时错兜底后 _reader_loop 退出", exited,
      "还在跑，可能重试没有上限")

# ------------------------------------------------------------------ 5) --log 真写文件
src = (ROOT / "pc" / "hub.py").read_text(encoding="utf-8")
check("hub.py 有 --log 命令行选项",
      bool(re.search(r"add_argument\(\s*[\"']--log", src)),
      "没找到 add_argument(\"--log\" …)")
check("hub.py 用 FileHandler 把日志落盘", "FileHandler" in src)

with tempfile.TemporaryDirectory(prefix="hub-serial-") as td:
    log_file = os.path.join(td, "hub-test.log")
    logger = hub.LOG
    old_level = logger.level
    handler = logging.FileHandler(log_file, encoding="utf-8")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    marker = "hub-serial 测试日志 %d" % time.time_ns()
    try:
        logger.info("%s", marker)
        handler.flush()
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
        check("FileHandler 把日志真的写进了文件", marker in text,
              "文件内容=%r" % text[:120])
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
    os.remove(log_file)
    check("临时日志文件测完已删除", not os.path.exists(log_file))

# 5b) 真的走一遍 hub.main 的 --log 接线：把 --once-board 换成桩，
#     这样只跑到「加 FileHandler」那几行就返回，不起 HTTP/serial/hub。
with tempfile.TemporaryDirectory(prefix="hub-main-log-") as td:
    log_file = os.path.join(td, "main.log")
    root = logging.getLogger()
    before = list(root.handlers)
    orig_once = hub.run_once_board
    hub.run_once_board = lambda args: 0
    rc = None
    try:
        rc = hub.main(["--serial", "COM99", "--no-lock", "--once-board",
                       "--log", log_file, "--quiet"])
    finally:
        hub.run_once_board = orig_once
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
    text = Path(log_file).read_text(encoding="utf-8", errors="replace") if os.path.isfile(log_file) else ""
    check("hub.main --log 创建了日志文件", os.path.isfile(log_file), "退出码=%r" % rc)
    check("hub.main --log 把启动日志写进了文件", "日志同时写入" in text,
          "文件内容=%r" % text[:200])
    if os.path.isfile(log_file):
        os.remove(log_file)
    check("hub.main 的临时日志文件已删除", not os.path.exists(log_file))

print("-" * 60)
if failures:
    print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("全部通过")
