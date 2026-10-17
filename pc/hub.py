#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pc/hub.py — 仪表盘中枢：同源 HTTP 服务 + 板子串口采集 + 本机数据 + TCP 透传。

架构（Lead 2026-10-17 裁决：板子没有网络出口，改由 PC 当服务器）：

    手机/浏览器 ──http://<PC>:8765/────────────┐
                                               │  同源静态页 web/index.html|app.js|style.css
                                               │  GET /api/board  板子统计（hub 从串口采集）
                                               │  GET /api/pc     本机统计（ctypes/PowerShell/nvidia-smi）
                                               │  GET /api/health 健康检查
                                               │  GET /api/shutdown（仅 127.0.0.1）
    pc/brain.py ──tcp://127.0.0.1:8766──► TCP 透传 ⇄ COM7（原始 JSON-lines，逐字节转发）

关键约束：
  * 串口**只打开一次、常驻**（CH340 每次打开会复位板子）：后台线程每秒发
    {"cmd":"stats"} 收响应并缓存；TCP 透传期间暂停轮询，避免抢响应。
  * 拿板子前先持锁 tools/board_lock.py（作用域=整个进程，启动时 acquire，
    /api/shutdown 或 Ctrl+C 时 release）。锁被别人占用时继续重试，/api/board 报 board_busy。
  * COM7 打不开/没插板子也要能跑：/api/board 返回 {"ok":false,"err":"board_offline"}。
  * 只依赖标准库（串口用 ctypes 直调 Win32，复用 pc/transport_uart.py 的 DCB 结构）。

用法：
    python pc/hub.py                         # 0.0.0.0:8765 页面 + 127.0.0.1:8766 透传 + COM7
    python pc/hub.py --serial COM5           # 指定串口
    python pc/hub.py --no-firewall           # 不尝试自动加 Windows 防火墙规则
    python pc/hub.py --serial COM99 --no-lock --port 0 --tcp-port 0   # 自检用（板子离线路径）
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 路径 / 依赖（同目录的 pc 模块、上级目录的 tools/board_lock.py）
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
TOOLS_DIR = os.path.join(_ROOT, "tools")
WEB_ROOT = os.path.join(_ROOT, "web")

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from transport_uart import (  # noqa: E402  (复用 pc/transport_uart.py 的 DCB 结构/常量)
    COMMTIMEOUTS,
    DCB,
    ERROR_ACCESS_DENIED,
    ERROR_BAD_COMMAND,
    ERROR_DEVICE_NOT_CONNECTED,
    ERROR_FILE_NOT_FOUND,
    ERROR_GEN_FAILURE,
    ERROR_INVALID_HANDLE,
    ERROR_OPERATION_ABORTED,
    ERROR_PATH_NOT_FOUND,
    ERROR_SEM_TIMEOUT,
    FILE_ATTRIBUTE_NORMAL,
    GENERIC_READ,
    GENERIC_WRITE,
    INVALID_HANDLE_VALUE,
    MAXDWORD,
    NOPARITY,
    ONESTOPBIT,
    OPEN_EXISTING,
    PURGE_RXCLEAR,
    PURGE_TXCLEAR,
    detect_port,
    normalize_port_name,
)

import board_lock  # noqa: E402  (tools/board_lock.py：COM7 跨进程互斥锁)

LOG = logging.getLogger("hub")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 8766
DEFAULT_SERIAL = "COM7"
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_PC_INTERVAL = 1.0
DEFAULT_TOP = 12

CREATE_NO_WINDOW = 0x08000000
STOP = threading.Event()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


# ===========================================================================
# 1) 串口（ctypes 直调 Win32，立即返回模式；不用 pyserial）
# ===========================================================================
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE,
]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.GetCommState.argtypes = [wt.HANDLE, ctypes.POINTER(DCB)]
_k32.GetCommState.restype = wt.BOOL
_k32.SetCommState.argtypes = [wt.HANDLE, ctypes.POINTER(DCB)]
_k32.SetCommState.restype = wt.BOOL
_k32.SetCommTimeouts.argtypes = [wt.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
_k32.SetCommTimeouts.restype = wt.BOOL
_k32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.ReadFile.restype = wt.BOOL
_k32.WriteFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
_k32.WriteFile.restype = wt.BOOL
_k32.PurgeComm.argtypes = [wt.HANDLE, wt.DWORD]
_k32.PurgeComm.restype = wt.BOOL


class SerialError(Exception):
    """串口打不开 / 读写失败 / 被拔出。"""


class SerialLink:
    """一个常驻的 COM 端口句柄：115200 8N1，非阻塞式读取。"""

    def __init__(self, port: str, baud: int = 115200, read_chunk: int = 4096):
        self.port = str(port).strip().upper()
        self.port_path = normalize_port_name(port)
        self.baud = int(baud)
        self.read_chunk = int(read_chunk)
        self._handle = None
        self._write_lock = threading.Lock()

    # -- 打开 / 关闭 ------------------------------------------------------
    def open(self) -> None:
        if self._handle:
            return
        handle = _k32.CreateFileW(
            self.port_path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise SerialError(self._explain(err))
        self._handle = handle
        try:
            self._configure()
            _k32.PurgeComm(self._handle, PURGE_RXCLEAR | PURGE_TXCLEAR)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _explain(err: int) -> str:
        hints = {
            ERROR_FILE_NOT_FOUND: "端口不存在（板子没插 / COM 号不对？）",
            ERROR_PATH_NOT_FOUND: "端口路径不存在",
            ERROR_ACCESS_DENIED: "端口被占用（串口监视器/其他程序开着？或抢锁失败）",
            ERROR_BAD_COMMAND: "设备未就绪",
        }
        return "无法打开串口: WinError %d - %s" % (err, hints.get(err, "未知错误"))

    def _configure(self) -> None:
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not _k32.GetCommState(self._handle, ctypes.byref(dcb)):
            raise SerialError("GetCommState 失败 WinError %d" % ctypes.get_last_error())
        dcb.BaudRate = self.baud
        dcb.ByteSize = 8
        dcb.Parity = NOPARITY
        dcb.StopBits = ONESTOPBIT
        dcb.Flags = 0x00000001 | (0x01 << 4) | (0x01 << 12)  # fBinary + DTR/RTS enable
        dcb.XonLim = 2048
        dcb.XoffLim = 512
        if not _k32.SetCommState(self._handle, ctypes.byref(dcb)):
            raise SerialError("SetCommState 失败 WinError %d" % ctypes.get_last_error())

        timeouts = COMMTIMEOUTS()
        timeouts.ReadIntervalTimeout = MAXDWORD   # 立即返回模式
        timeouts.ReadTotalTimeoutMultiplier = MAXDWORD
        timeouts.ReadTotalTimeoutConstant = 0
        timeouts.WriteTotalTimeoutMultiplier = 0
        timeouts.WriteTotalTimeoutConstant = 3000
        if not _k32.SetCommTimeouts(self._handle, ctypes.byref(timeouts)):
            raise SerialError("SetCommTimeouts 失败 WinError %d" % ctypes.get_last_error())

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle and handle != INVALID_HANDLE_VALUE:
            try:
                _k32.CloseHandle(handle)
            except Exception:
                pass

    @property
    def opened(self) -> bool:
        return bool(self._handle)

    # -- 读写 -------------------------------------------------------------
    def read(self, max_bytes: int | None = None) -> bytes:
        if not self._handle:
            raise SerialError("串口未打开")
        n = int(max_bytes or self.read_chunk)
        buf = ctypes.create_string_buffer(n)
        got = wt.DWORD(0)
        if not _k32.ReadFile(self._handle, buf, n, ctypes.byref(got), None):
            err = ctypes.get_last_error()
            if err in (ERROR_OPERATION_ABORTED, ERROR_SEM_TIMEOUT):
                return b""
            if err in (ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE, ERROR_BAD_COMMAND,
                       ERROR_FILE_NOT_FOUND, ERROR_GEN_FAILURE):
                raise SerialError("读串口失败 WinError %d（设备被拔出？）" % err)
            raise SerialError("读串口失败 WinError %d" % err)
        return buf.raw[: got.value]

    def write(self, data: bytes) -> int:
        if not self._handle:
            raise SerialError("串口未打开")
        view = memoryview(bytes(data))
        total = 0
        with self._write_lock:
            while total < len(view):
                chunk = view[total:]
                written = wt.DWORD(0)
                if not _k32.WriteFile(self._handle, bytes(chunk), len(chunk),
                                      ctypes.byref(written), None):
                    err = ctypes.get_last_error()
                    raise SerialError("写串口失败 WinError %d（设备被拔出？）" % err)
                if written.value == 0:
                    raise SerialError("写串口返回 0 字节")
                total += written.value
        return total

    def flush_input(self) -> None:
        if self._handle:
            try:
                _k32.PurgeComm(self._handle, PURGE_RXCLEAR)
            except Exception:
                pass


# ===========================================================================
# 2) 板子链路：锁 + 常驻串口 + stats 轮询 + 透传状态
# ===========================================================================


def extract_stats(obj: dict) -> dict | None:
    """从 stats 响应里取出 stats 对象（兼容嵌套 / 平铺两种固件实现）。"""
    if not isinstance(obj, dict):
        return None
    nested = obj.get("stats")
    if isinstance(nested, dict):
        return nested
    reserved = {"ok", "id", "t", "evt", "err", "detail"}
    flat = {k: v for k, v in obj.items() if k not in reserved}
    if "uptime_ms" in flat or "cpu" in flat or "mem" in flat or "tasks" in flat:
        return flat
    return None


class BoardLink:
    """持锁 + 常驻串口；后台每秒 `stats`，缓存最新统计；支持 TCP 透传切换。"""

    def __init__(self, port: str, who: str, use_lock: bool = True,
                 poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.port = port
        self.who = who
        self.use_lock = use_lock
        self.poll_interval = max(0.2, float(poll_interval))

        self.state = "init"          # init / busy / offline / no_stats / online
        self.detail = ""
        self.serial_open = False
        self.stats: dict | None = None
        self.stats_ts = 0
        self.passthrough = False
        self.passthrough_client = ""
        self._lock_held = False
        self._lock_holder: dict | None = None

        self._serial: SerialLink | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._read_stop = threading.Event()
        self._mode_lock = threading.Lock()
        self._poll_lock = threading.Lock()   # 保证 stats 轮询与透传切换互斥
        self._passthrough_requested = threading.Event()  # 透传方已「举手」，manager 停发新轮询
        self._mode = "poll"          # poll / passthrough
        self._linebuf = bytearray()
        self._pass_queue: queue.Queue = queue.Queue()
        self._pending_id: int | None = None
        self._pending: dict | None = None
        self._resp_event = threading.Event()
        self._next_id = 1
        self._state_lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._manager, name="board-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_serial()
        self._release_lock()
        LOG.info("BoardLink 已停止")

    # -- 锁 ---------------------------------------------------------------
    def _try_lock(self) -> bool:
        if self._lock_held:
            return True
        if not self.use_lock:
            self._lock_held = True
            return True
        try:
            board_lock.acquire(self.who, timeout=2.0, poll=0.5)
            self._lock_held = True
            self._lock_holder = None
            LOG.info("已拿到板子锁（%s）", self.who)
            return True
        except TimeoutError as exc:
            self._lock_holder = board_lock.status()
            self._set_state("busy", "板子锁被占用：%s" % exc)
            return False

    def _release_lock(self) -> None:
        if self._lock_held and self.use_lock:
            try:
                board_lock.release(self.who)
            except Exception:
                LOG.debug("release lock failed", exc_info=True)
        self._lock_held = False

    # -- 串口 -------------------------------------------------------------
    def _open_serial(self) -> bool:
        try:
            link = SerialLink(self.port)
            link.open()
        except SerialError as exc:
            self._set_state("offline", str(exc))
            LOG.warning("串口打开失败：%s", exc)
            return False
        self._serial = link
        self.serial_open = True
        self._read_stop.clear()
        self._linebuf.clear()
        self._reader = threading.Thread(target=self._reader_loop, name="serial-reader", daemon=True)
        self._reader.start()
        self._set_state("no_stats", "串口已打开，等待 stats 响应")
        LOG.info("串口 %s 已打开（常驻，115200 8N1）", self.port)
        return True

    def _close_serial(self) -> None:
        self.serial_open = False
        self._read_stop.set()
        link, self._serial = self._serial, None
        if link is not None:
            link.close()
            LOG.info("串口 %s 已关闭", self.port)

    def _reader_loop(self) -> None:
        while not self._stop.is_set() and not self._read_stop.is_set():
            link = self._serial
            if link is None:
                return
            try:
                data = link.read()
            except SerialError as exc:
                LOG.warning("串口读失败，稍后重开：%s", exc)
                self._close_serial()
                return
            if not data:
                time.sleep(0.002)
                continue
            with self._mode_lock:
                mode = self._mode
            if mode == "passthrough":
                self._pass_queue.put(data)
            else:
                self._feed_lines(data)

    def _feed_lines(self, data: bytes) -> None:
        self._linebuf.extend(data)
        if len(self._linebuf) > 16384:      # 防御：异常流不撑爆内存
            self._linebuf.clear()
        while True:
            idx = self._linebuf.find(b"\n")
            if idx < 0:
                return
            raw = bytes(self._linebuf[:idx])
            del self._linebuf[: idx + 1]
            line = raw.decode("utf-8", "replace").strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if self._pending_id is not None and obj.get("id") == self._pending_id:
                self._pending = obj
                self._resp_event.set()

    # -- 轮询 stats -------------------------------------------------------
    def _manager(self) -> None:
        while not self._stop.is_set():
            try:
                if self._serial is None:
                    if not self._lock_held and not self._try_lock():
                        self._sleep(1.0)
                        continue
                    if not self._open_serial():
                        self._sleep(2.0)
                        continue
                if self.passthrough or self._passthrough_requested.is_set():
                    self._sleep(0.1)
                    continue
                # 注意：锁只包住「发命令 → 等响应」这一次往返，睡眠必须在锁外，
                # 否则透传线程会一直被 1 s 一轮的轮询饿死（旧版 bug：8766 挂死）。
                t0 = time.monotonic()
                if not self._poll_lock.acquire(timeout=0.5):
                    continue
                try:
                    if self.passthrough or self._passthrough_requested.is_set():
                        continue
                    self._poll_once()
                finally:
                    self._poll_lock.release()
                spent = time.monotonic() - t0
                self._sleep(max(0.05, self.poll_interval - spent))
            except Exception:
                LOG.error("board manager 异常：\n%s", traceback.format_exc())
                self._close_serial()
                self._sleep(2.0)

    def _poll_once(self) -> None:
        link = self._serial
        if link is None:
            return
        with self._state_lock:
            pid = self._next_id
            self._next_id += 1
            self._pending_id = pid
            self._pending = None
            self._resp_event.clear()
        start = time.monotonic()
        try:
            link.write(json.dumps({"cmd": "stats", "id": pid},
                                  ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        except SerialError as exc:
            self._set_state("offline", str(exc))
            self._close_serial()
            return

        resp = None
        deadline = start + min(0.9, max(0.3, self.poll_interval * 0.9))
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._resp_event.wait(0.02):
                resp = self._pending
                break
        with self._state_lock:
            self._pending_id = None
            self._pending = None

        if resp is None:
            if self.stats is None:
                self._set_state("no_stats", "stats 命令 %.1fs 内无响应（固件可能还没实现 v1.2 stats）"
                                % (deadline - start))
            else:
                self.detail = "stats 响应超时（保留上一次数据）"
        elif resp.get("ok") is True:
            stats = extract_stats(resp)
            if stats is None:
                self._set_state("no_stats", "stats 响应里没有可识别的统计字段")
            else:
                with self._state_lock:
                    self.stats = stats
                    self.stats_ts = int(time.time() * 1000)
                self._set_state("online", "")
        else:
            err = resp.get("err") or "unknown"
            self._set_state("no_stats", "stats 返回错误：%s" % err)

        # 节奏由 _manager 在锁外 sleep 控制（这里不能再睡，否则会拖住透传切换）

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(max(0.0, seconds))

    def _set_state(self, state: str, detail: str) -> None:
        if state != self.state:
            LOG.info("板子状态：%s -> %s %s", self.state, state, detail or "")
        self.state = state
        self.detail = detail or ""

    # -- 对外快照（/api/board）--------------------------------------------
    def snapshot(self) -> dict:
        state = self.state
        base = {
            "port": self.port,
            "serial_open": self.serial_open,
            "passthrough": self.passthrough,
        }
        if state == "online" and self.stats is not None:
            now = int(time.time() * 1000)
            return dict(base, ok=True, stats=self.stats,
                        ts=self.stats_ts, age_ms=max(0, now - self.stats_ts))
        if state == "busy":
            holder = self._lock_holder or {}
            return dict(base, ok=False, err="board_busy",
                        detail=self.detail,
                        holder=holder.get("who"),
                        holder_age_s=holder.get("age_s"))
        if state == "no_stats":
            return dict(base, ok=False, err="board_no_stats", detail=self.detail)
        return dict(base, ok=False, err="board_offline", detail=self.detail)

    # -- 网页按钮发命令（/api/cmd）------------------------------------------
    def send_command(self, payload: dict, timeout: float = 6.0) -> dict:
        """把一条 JSON 命令发给板子并等它的响应。

        与 stats 轮询用同一把 _poll_lock 串行化，避免两条请求同时在线上打架。
        返回板子的原始响应 dict；失败时返回 {"ok": false, "err": ...}。
        """
        if self.passthrough:
            return {"ok": False, "err": "passthrough_active",
                    "detail": "板子正在被 pc/brain.py 透传占用，请先断开"}
        if not self.serial_open or self._serial is None:
            return {"ok": False, "err": "board_offline"}
        if not self._poll_lock.acquire(timeout=2.0):
            return {"ok": False, "err": "board_busy", "detail": "stats 轮询占用中，请重试"}
        try:
            link = self._serial
            if link is None or not self.serial_open:
                return {"ok": False, "err": "board_offline"}
            with self._state_lock:
                pid = self._next_id
                self._next_id += 1
                self._pending_id = pid
                self._pending = None
                self._resp_event.clear()
            body = dict(payload)
            body["id"] = pid
            try:
                link.write(json.dumps(body, ensure_ascii=False,
                                      separators=(",", ":")).encode() + b"\n")
            except SerialError as exc:
                self._close_serial()
                return {"ok": False, "err": "serial_write_failed", "detail": str(exc)}
            deadline = time.monotonic() + timeout
            resp = None
            while time.monotonic() < deadline and not self._stop.is_set():
                if self._resp_event.wait(0.02):
                    resp = self._pending
                    break
            with self._state_lock:
                self._pending_id = None
                self._pending = None
            if resp is None:
                return {"ok": False, "err": "timeout", "timeout_s": timeout}
            return resp
        finally:
            self._poll_lock.release()

    # -- TCP 透传支撑 -----------------------------------------------------
    def write_serial(self, data: bytes) -> None:
        link = self._serial
        if link is None:
            raise SerialError("串口未打开")
        link.write(data)

    def begin_passthrough(self, client: str) -> None:
        # 两步握手：先「举手」让 manager 不再发起新的 stats 轮询，再等它在途的那次往返
        # 结束（最多 0.5 s 拿锁 + 0.9 s 等响应），然后切模式。旧版直接抢锁会被饿死。
        self._passthrough_requested.set()
        try:
            if not self._poll_lock.acquire(timeout=3.0):
                raise SerialError("板子 stats 轮询正忙，透传启动超时（3 s）")
            try:
                if self._serial is None or not self.serial_open:
                    raise SerialError("串口未打开，无法进入透传")
                with self._mode_lock:
                    self.passthrough = True
                    self.passthrough_client = client
                    self._mode = "passthrough"
                    self._pass_queue = queue.Queue()   # 丢弃切换前残留字节
                    self._linebuf.clear()
                link = self._serial
                if link is not None:
                    link.flush_input()
            finally:
                self._poll_lock.release()
        except Exception:
            self._passthrough_requested.clear()
            raise
        LOG.info("TCP 透传开始：%s", client)

    def end_passthrough(self) -> None:
        with self._mode_lock:
            self.passthrough = False
            self.passthrough_client = ""
            self._mode = "poll"
            self._pass_queue = queue.Queue()
            self._linebuf.clear()
        link = self._serial
        if link is not None:
            link.flush_input()   # 丢掉客户端断开时留下的半截响应
        self._passthrough_requested.clear()
        LOG.info("TCP 透传结束，恢复 stats 轮询")


# ===========================================================================
# 3) TCP 透传：127.0.0.1:8766，一次一个客户端，⇄ COM7
# ===========================================================================
class TcpBridge(threading.Thread):
    def __init__(self, board: BoardLink, host: str = DEFAULT_TCP_HOST,
                 port: int = DEFAULT_TCP_PORT):
        super().__init__(name="tcp-bridge", daemon=True)
        self.board = board
        self.host = host
        self.port = port
        self._srv: socket.socket | None = None
        # 注意：不能叫 _stop —— Thread 自己有个 _stop() 方法，会被 join() 调用；
        # 用 Event 覆盖它会让 join() 抛 TypeError（selftest 踩过）。
        self._stop_event = threading.Event()
        self.bound_port = 0
        self._client_lock = threading.Lock()
        self._active_client: str | None = None

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(4)
        except OSError as exc:
            LOG.error("TCP 透传端口 %s:%s 绑定失败：%s（透传功能不可用，HTTP 继续）",
                      self.host, self.port, exc)
            srv.close()
            return
        self._srv = srv
        self.bound_port = srv.getsockname()[1]
        LOG.info("TCP 透传监听 %s:%d（一次一个客户端）", self.host, self.bound_port)
        # accept 循环只负责接客：把连接交给独立线程，避免一个卡住的客户端把
        # accept 占死、backlog 塞满、后续连接全部超时（旧版 bug）。
        while not self._stop_event.is_set() and not STOP.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            client = "%s:%d" % (addr[0], addr[1])
            with self._client_lock:
                busy_client = self._active_client
                if busy_client is None and self.board.serial_open:
                    self._active_client = client
            if busy_client is not None:
                LOG.warning("已有透传客户端（%s），拒绝 %s", busy_client, client)
                self._close_quietly(conn)
                continue
            if not self.board.serial_open:
                LOG.warning("串口未打开，拒绝透传客户端 %s", client)
                self._close_quietly(conn)
                continue
            threading.Thread(target=self._serve_connection, args=(conn, client),
                             name="tcp-serve", daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass

    @staticmethod
    def _close_quietly(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    def _serve_connection(self, conn: socket.socket, client: str) -> None:
        """一条透传连接的完整生命周期：异常不逃逸，退出必清 active + 恢复轮询。"""
        try:
            self._serve_client(conn, client)
        except Exception:
            LOG.error("TCP 透传客户端 %s 异常：\n%s", client, traceback.format_exc())
        finally:
            self._close_quietly(conn)
            with self._client_lock:
                if self._active_client == client:
                    self._active_client = None
            LOG.info("TCP 透传客户端已断开：%s", client)

    def _serve_client(self, conn: socket.socket, client: str) -> None:
        begun = False
        try:
            self.board.begin_passthrough(client)
            begun = True
            conn.setblocking(False)
            while not self._stop_event.is_set() and not STOP.is_set():
                if not self.board.serial_open:
                    LOG.warning("透传中串口断开，结束透传 %s", client)
                    break
                try:
                    readable, _, _ = select.select([conn], [], [], 0.02)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        data = conn.recv(4096)
                    except (BlockingIOError, InterruptedError):
                        data = b""
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        self.board.write_serial(data)
                    except SerialError as exc:
                        LOG.warning("透传写串口失败：%s", exc)
                        break
                # 串口 → 客户端
                while True:
                    try:
                        chunk = self.board._pass_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        conn.sendall(chunk)
                    except OSError:
                        return
        finally:
            if begun:
                self.board.end_passthrough()

    def stop(self) -> None:
        self._stop_event.set()
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass


# ===========================================================================
# 4) 本机数据采集（沿用 pc/pcstats.py 的实现：全部标准库）
# ===========================================================================
class _SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("IdleTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("DpcTime", ctypes.c_longlong),
        ("InterruptTime", ctypes.c_longlong),
        ("InterruptCount", wt.ULONG),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class CpuSampler:
    """每核 CPU 占用：NtQuerySystemInformation(SystemProcessorPerformanceInformation)。"""

    SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION = 8

    def __init__(self) -> None:
        self._ntdll = ctypes.WinDLL("ntdll")
        self._ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        self._prev: list[tuple[int, int, int]] | None = None
        self._prev_t = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _struct_size() -> int:
        return ctypes.sizeof(_SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION)

    def _read_times(self) -> list[tuple[int, int, int]]:
        size = self._struct_size() * 128
        buf = ctypes.create_string_buffer(size)
        ret_len = wt.ULONG(0)
        status = self._ntdll.NtQuerySystemInformation(
            self.SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION, buf, size, ctypes.byref(ret_len))
        if status != 0:
            raise OSError("NtQuerySystemInformation failed: status=%#x" % status)
        count = ret_len.value // self._struct_size()
        if count <= 0:
            raise OSError("NtQuerySystemInformation returned 0 processors")
        arr = (_SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION * count).from_buffer(buf)
        return [(int(p.IdleTime), int(p.KernelTime), int(p.UserTime)) for p in arr]

    def sample(self) -> tuple[list[float], float]:
        with self._lock:
            now = time.monotonic()
            times = self._read_times()
            if self._prev is None or now - self._prev_t <= 0.02:
                time.sleep(0.25)
                now = time.monotonic()
                times = self._read_times()
                self._prev = times
                self._prev_t = now
                return [0.0] * len(times), 0.0
            prev = self._prev
            self._prev = times
            self._prev_t = now
        cores: list[float] = []
        sum_total = 0.0
        sum_idle = 0.0
        for (pi, pk, pu), (ci, ck, cu) in zip(prev, times):
            di, dk, du = ci - pi, ck - pk, cu - pu
            total = dk + du
            sum_total += max(0.0, float(total))
            sum_idle += max(0.0, float(di))
            if total <= 0:
                cores.append(0.0)
            else:
                busy = max(0.0, float(total - di))
                cores.append(round(min(100.0, 100.0 * busy / total), 1))
        total_pct = 0.0
        if sum_total > 0:
            total_pct = round(min(100.0, 100.0 * (sum_total - sum_idle) / sum_total), 1)
        return cores, total_pct


def read_memory() -> dict:
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise OSError("GlobalMemoryStatusEx failed")
    total_mb = int(m.ullTotalPhys // (1024 * 1024))
    avail_mb = int(m.ullAvailPhys // (1024 * 1024))
    used_mb = max(0, total_mb - avail_mb)
    pct = round(used_mb * 100.0 / total_mb, 1) if total_mb else 0.0
    return {"total_mb": total_mb, "used_mb": used_mb, "pct": pct}


def read_uptime_s() -> int:
    return int(ctypes.windll.kernel32.GetTickCount64() // 1000)


def read_cpu_mhz() -> int:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            value, _ = winreg.QueryValueEx(key, "~MHz")
            return int(value)
    except Exception:
        return 0


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        cand = os.path.join(directory, name)
        if os.path.isfile(cand):
            return cand
    return None


def _decode_console(data) -> str:
    """解码 Windows 控制台子进程输出：PowerShell 有时给 UTF-8，有时给 GBK。

    统一先按 UTF-8 严格解，失败再按 GBK 解，最后兜底 replace —— 绝不让一个
    控制台重定向的编码问题把采样线程打挂（旧版 gbk 严格解码就崩过）。
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class GpuSampler:
    GPU_QUERY = "name,utilization.gpu,temperature.gpu,memory.used,memory.total"

    def __init__(self) -> None:
        self._exe = None
        for cand in (_which("nvidia-smi"),
                     r"C:\Windows\System32\nvidia-smi.exe",
                     r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if cand and os.path.isfile(cand):
                self._exe = cand
                break
        self._last: list[dict] = []

    def available(self) -> bool:
        return self._exe is not None

    @staticmethod
    def _num(text: str) -> float | None:
        text = text.strip()
        if not text or text.upper() in {"N/A", "[N/A]", "NOT SUPPORTED", "[NOT SUPPORTED]"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def sample(self) -> list[dict]:
        if not self._exe:
            return []
        try:
            proc = subprocess.run(
                [self._exe, "--query-gpu=" + self.GPU_QUERY, "--format=csv,noheader,nounits"],
                capture_output=True, timeout=5, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.debug("nvidia-smi failed: %s", exc)
            return self._last
        if proc.returncode != 0:
            LOG.debug("nvidia-smi rc=%s", proc.returncode)
            return self._last
        gpus: list[dict] = []
        for line in _decode_console(proc.stdout).splitlines():
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) < 5:
                continue
            util = self._num(parts[1])
            temp = self._num(parts[2])
            used = self._num(parts[3])
            total = self._num(parts[4])
            gpus.append({
                "name": parts[0],
                "util_pct": round(util, 1) if util is not None else None,
                "temp_c": round(temp, 1) if temp is not None else None,
                "mem_used_mb": int(used) if used is not None else None,
                "mem_total_mb": int(total) if total is not None else None,
            })
        if gpus:
            self._last = gpus
        return self._last


NPU_PROBE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$pat = '\bNPU\b|Neural|AI Boost|Ryzen AI|Intel\(R\) AI'
$name = $null
try {
  $name = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.FriendlyName -match $pat } |
    Select-Object -First 1 -ExpandProperty FriendlyName
} catch { }
if (-not $name) {
  try {
    $name = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match $pat } |
      Select-Object -First 1 -ExpandProperty Name
  } catch { }
}
if ($name) { [Console]::Out.WriteLine($name) }
"""


class NpuProbe(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="npu-probe", daemon=True)
        self.detected = False
        self.name_ = None
        self.done = threading.Event()

    def run(self) -> None:
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", NPU_PROBE_PS],
                capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            lines = _decode_console(proc.stdout).strip().splitlines()
            if lines:
                self.name_ = lines[0].strip()
                self.detected = bool(self.name_)
        except Exception as exc:
            LOG.debug("npu probe failed: %s", exc)
        finally:
            self.done.set()

    def as_dict(self) -> dict:
        return {"detected": bool(self.detected), "name": self.name_}


PS_WORKER_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$ncores = [Environment]::ProcessorCount
$prev = @{}
$prevT = [DateTime]::UtcNow
while ($true) {
  $now = [DateTime]::UtcNow
  $elapsed = ($now - $prevT).TotalSeconds
  if ($elapsed -le 0.05) { $elapsed = 1.0 }
  $all = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.CPU -ne $null })
  $rows = @()
  foreach ($p in $all) {
    $k = [string]$p.Id
    $pct = $null
    if ($prev.ContainsKey($k)) {
      $pct = [math]::Round((($p.CPU - $prev[$k]) / $elapsed) * 100.0 / $ncores, 1)
      if ($pct -lt 0.0) { $pct = 0.0 }
      if ($pct -gt 100.0) { $pct = 100.0 }
    }
    $rows += [pscustomobject]@{
      name = [string]$p.Name
      pid = [int]$p.Id
      cpu_pct = $pct
      mem_mb = [math]::Round($p.WorkingSet64 / 1MB, 1)
      cpu_s = [math]::Round($p.CPU, 2)
    }
  }
  $prev = @{}
  foreach ($p in $all) { $prev[[string]$p.Id] = $p.CPU }
  $prevT = [DateTime]::UtcNow
  $top = @($rows | Sort-Object cpu_s -Descending | Select-Object -First 40)
  $msg = [pscustomobject]@{ t = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); processes = $top }
  [Console]::Out.WriteLine(($msg | ConvertTo-Json -Compress -Depth 4))
  [Console]::Out.Flush()
  $next = [DateTime]::UtcNow.AddMilliseconds(1000)
  $wait = ($next - [DateTime]::UtcNow).TotalMilliseconds
  if ($wait -lt 100) { $wait = 100 }
  Start-Sleep -Milliseconds ([int]$wait)
}
"""


class PSProcessSampler:
    """常驻 PowerShell 采样进程（每 1 s 一行 JSON），挂掉自动重启。"""

    def __init__(self, top: int = DEFAULT_TOP) -> None:
        self.top = top
        self._lock = threading.Lock()
        self._processes: list[dict] = []
        self._sample_ts = 0
        self._restarts = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._manager, name="ps-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _spawn(self) -> subprocess.Popen:
        encoded = base64.b64encode(PS_WORKER_SCRIPT.encode("utf-16-le")).decode("ascii")
        return subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW)

    def _manager(self) -> None:
        while not self._stop.is_set():
            proc = None
            try:
                proc = self._spawn()
                self._proc = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rows = msg.get("processes")
                    if not isinstance(rows, list):
                        continue
                    cleaned: list[dict] = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        try:
                            pid = int(row.get("pid"))
                        except (TypeError, ValueError):
                            continue
                        cpu_pct = row.get("cpu_pct")
                        try:
                            cpu_pct = float(cpu_pct) if cpu_pct is not None else None
                        except (TypeError, ValueError):
                            cpu_pct = None
                        try:
                            mem_mb = float(row.get("mem_mb") or 0.0)
                        except (TypeError, ValueError):
                            mem_mb = 0.0
                        try:
                            cpu_s = float(row.get("cpu_s") or 0.0)
                        except (TypeError, ValueError):
                            cpu_s = 0.0
                        cleaned.append({"name": str(row.get("name") or "?"), "pid": pid,
                                        "cpu_pct": cpu_pct, "mem_mb": round(mem_mb, 1),
                                        "_cpu_s": cpu_s})
                    cleaned.sort(key=lambda r: (r["cpu_pct"] is None, -(r["cpu_pct"] or 0.0)))
                    with self._lock:
                        self._processes = cleaned[: self.top]
                        self._sample_ts = int(msg.get("t") or 0)
            except FileNotFoundError:
                LOG.error("找不到 powershell.exe，进程列表不可用")
                time.sleep(30)
            except Exception as exc:
                LOG.debug("ps sampler error: %s", exc)
            finally:
                if proc is not None:
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                self._proc = None
                if not self._stop.is_set():
                    self._restarts += 1
                    time.sleep(2.0)

    def latest(self) -> tuple[list[dict], int]:
        with self._lock:
            return [dict(p) for p in self._processes], self._sample_ts


class PcSnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict | None = None

    def publish(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def read(self) -> dict | None:
        with self._lock:
            return json.loads(json.dumps(self._payload)) if self._payload else None


class PcSampler:
    """后台线程：本机 CPU/内存/GPU/进程/NPU → 契约 JSON，每 interval 秒发布一次。"""

    def __init__(self, store: PcSnapshotStore, interval: float, top: int) -> None:
        self.store = store
        self.interval = max(0.2, float(interval))
        self.top = top
        self.cpu = CpuSampler()
        self.gpu = GpuSampler()
        self.ps = PSProcessSampler(top=top)
        self.npu = NpuProbe()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.ps.start()
        self.npu.start()
        self.thread = threading.Thread(target=self._loop, name="pc-sampler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.ps.stop()

    def build_payload(self) -> dict:
        errors: list[str] = []
        try:
            cores, total_pct = self.cpu.sample()
        except Exception as exc:
            cores, total_pct = [], 0.0
            errors.append("cpu: %s" % exc)
        try:
            mem = read_memory()
        except Exception as exc:
            mem = {"total_mb": 0, "used_mb": 0, "pct": 0.0}
            errors.append("mem: %s" % exc)
        processes, _ = self.ps.latest()
        for p in processes:
            p.pop("_cpu_s", None)
        payload = {
            "ok": True,
            "ts": int(time.time() * 1000),
            "cpu": {"total_pct": total_pct, "cores": cores, "mhz": read_cpu_mhz()},
            "mem": mem,
            "gpu": self.gpu.sample(),
            "npu": self.npu.as_dict(),
            "processes": processes,
            "uptime_s": read_uptime_s(),
        }
        if errors:
            payload["errors"] = errors
        if not cores:
            payload["ok"] = False
            payload.setdefault("errors", []).append("cpu sampling warming up")
        return payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                payload = self.build_payload()
                if payload["ok"]:
                    self.store.publish(payload)
            except Exception:
                LOG.error("PC 采样异常：\n%s", traceback.format_exc())
            time.sleep(max(0.05, self.interval - (time.monotonic() - t0)))


# ===========================================================================
# 5) HTTP：静态页 + /api/board + /api/pc + /api/health + /api/shutdown
# ===========================================================================
def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class HubHandler(BaseHTTPRequestHandler):
    server_version = "P4Hub/1.0"
    board: BoardLink | None = None
    pc_store: PcSnapshotStore | None = None
    hub = None                 # Hub 实例（用于 shutdown/stats）
    quiet = False

    # -- 基础 -------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        if not HubHandler.quiet:
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- 路由 -------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/api/board", "/api/board/"):
                self._send_json(200, HubHandler.board.snapshot() if HubHandler.board else
                                {"ok": False, "err": "board_offline"})
            elif path in ("/api/pc", "/api/pc/"):
                payload = HubHandler.pc_store.read() if HubHandler.pc_store else None
                if payload is None:
                    self._send_json(503, {"ok": False, "error": "warming_up"})
                else:
                    self._send_json(200, payload)
            elif path in ("/api/health", "/api/health/"):
                self._handle_health()
            elif path in ("/api/shutdown", "/api/shutdown/"):
                self._handle_shutdown()
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            LOG.error("handler error: %s\n%s", exc, traceback.format_exc())
            try:
                self._send_json(500, {"ok": False, "error": "internal", "detail": str(exc)})
            except Exception:
                pass

    # -- 网页按钮：POST /api/cmd（白名单命令）-----------------------------
    CMD_WHITELIST = {"ping", "stats", "set", "sd_remount", "sd_test", "mouse_move"}

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ("/api/cmd", "/api/cmd/"):
            self._send_json(404, {"ok": False, "err": "not_found", "path": path})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            if len(raw) > 8192:
                self._send_json(413, {"ok": False, "err": "too_large"})
                return
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:
            self._send_json(400, {"ok": False, "err": "bad_json", "detail": str(exc)})
            return
        cmd = str(payload.get("cmd") or "")
        if cmd == "sd_format":
            client = self.client_address[0] if self.client_address else ""
            if client not in ("127.0.0.1", "::1"):
                self._send_json(403, {"ok": False, "err": "loopback_only"})
                return
            if payload.get("confirm") != "ERASE":
                self._send_json(400, {"ok": False, "err": "missing_confirm"})
                return
        elif cmd not in self.CMD_WHITELIST:
            self._send_json(400, {"ok": False, "err": "cmd_not_allowed", "cmd": cmd,
                                  "allowed": sorted(self.CMD_WHITELIST)})
            return
        if cmd == "mouse_move":
            # 安全护栏：只允许 dx=dy=0 的无位移报文（覆盖 HID 发送路径，但不动用户光标）
            try:
                dx = int(payload.get("dx") or 0)
                dy = int(payload.get("dy") or 0)
            except (TypeError, ValueError):
                dx, dy = 1, 1
            if dx != 0 or dy != 0:
                self._send_json(400, {"ok": False, "err": "mouse_move_blocked",
                                      "detail": "只允许 dx=0,dy=0，避免干扰用户光标"})
                return
        board = HubHandler.board
        if board is None:
            self._send_json(503, {"ok": False, "err": "board_offline"})
            return
        timeout = 15.0 if cmd in ("sd_test", "sd_format") else 8.0
        try:
            resp = board.send_command(payload, timeout=timeout)
        except Exception as exc:
            LOG.error("/api/cmd 异常：%s\n%s", exc, traceback.format_exc())
            self._send_json(500, {"ok": False, "err": "internal", "detail": str(exc)})
            return
        self._send_json(200, resp)

    def _handle_health(self) -> None:
        board = HubHandler.board
        snap = board.snapshot() if board else {"ok": False, "err": "board_offline"}
        self._send_json(200, {
            "ok": True,
            "ts": int(time.time() * 1000),
            "board": {
                "ok": snap.get("ok", False),
                "state": board.state if board else "off",
                "serial_open": board.serial_open if board else False,
                "port": board.port if board else None,
                "passthrough": board.passthrough if board else False,
                "detail": board.detail if board else "",
            },
            "pc_ready": bool(HubHandler.pc_store and HubHandler.pc_store.read()),
            "tcp_passthrough_port": getattr(HubHandler.hub, "tcp_bound_port", None),
        })

    def _handle_shutdown(self) -> None:
        client = self.client_address[0] if self.client_address else ""
        if client not in ("127.0.0.1", "::1"):
            self._send_json(403, {"ok": False, "err": "loopback_only"})
            return
        self._send_json(200, {"ok": True, "bye": True})
        hub = HubHandler.hub
        if hub is not None:
            hub.request_shutdown()

    # -- 静态页 -----------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.startswith("..") or os.path.isabs(rel):
            self._send_json(404, {"ok": False, "error": "not_found", "path": path})
            return
        full = os.path.abspath(os.path.join(WEB_ROOT, rel))
        allowed_root = os.path.abspath(WEB_ROOT) + os.sep
        if not full.startswith(allowed_root) or not os.path.isfile(full):
            self._send_json(404, {"ok": False, "error": "not_found", "path": path})
            return
        ext = os.path.splitext(full)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        try:
            body = _read_file_bytes(full)
        except OSError:
            self._send_json(500, {"ok": False, "error": "read_failed"})
            return
        self._send_bytes(200, body, content_type)


# ===========================================================================
# 6) 进程编排
# ===========================================================================
class Hub:
    def __init__(self, args) -> None:
        self.args = args
        self.board = BoardLink(args.serial, who=args.who,
                               use_lock=not args.no_lock,
                               poll_interval=args.poll_interval)
        self.pc_store = PcSnapshotStore()
        self.pc_sampler = PcSampler(self.pc_store, args.pc_interval, args.top)
        self.tcp = TcpBridge(self.board, args.tcp_host, args.tcp_port)
        self.tcp_bound_port = None
        self._httpd: ThreadingHTTPServer | None = None
        self._shutdown_started = False

    def start(self) -> None:
        self.board.start()
        self.pc_sampler.start()
        self.tcp.start()

    def request_shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        LOG.info("收到 /api/shutdown，正在停止…")
        STOP.set()

        def _shutdown() -> None:
            if self._httpd is not None:
                try:
                    self._httpd.shutdown()
                except Exception:
                    pass
        threading.Thread(target=_shutdown, name="shutdown", daemon=True).start()

    def stop(self) -> None:
        LOG.info("正在停止 hub…")
        STOP.set()
        self.tcp.stop()
        self.board.stop()
        self.pc_sampler.stop()


# ===========================================================================
# 7) 网络辅助 / 防火墙 / CLI
# ===========================================================================
def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^127\\.' "
             "-and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -ExpandProperty IPAddress) -join ','"],
            capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        for ip in _decode_console(proc.stdout).strip().split(","):
            if ip.strip():
                ips.append(ip.strip())
    except Exception:
        pass
    if not ips:
        try:
            ips.append(socket.gethostbyname(socket.gethostname()))
        except Exception:
            pass
    return [ip for ip in dict.fromkeys(ips) if not ip.startswith("127.")]


def try_open_firewall(port: int) -> tuple[bool, str]:
    """尝试加 Windows 入站规则；无管理员权限会失败，返回 (ok, message)。"""
    cmd = ["netsh", "advfirewall", "firewall", "add", "rule", "name=dsh-hub",
           "dir=in", "action=allow", "protocol=TCP", "localport=%d" % port]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20,
                              creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        return False, "netsh 调用失败：%s" % exc
    out = (_decode_console(proc.stdout) + _decode_console(proc.stderr)).strip().replace("\r", "")
    return proc.returncode == 0, out


def print_banner(args, httpd: ThreadingHTTPServer, hub: Hub) -> None:
    port = httpd.server_address[1]
    ips = lan_ips()
    print("=" * 78)
    print(" P4 HID Agent · 仪表盘中枢（PC 当服务器，板子走串口）")
    print("-" * 78)
    print(" 页面      : http://127.0.0.1:%d/" % port)
    for ip in ips:
        print(" 手机访问  : http://%s:%d/" % (ip, port))
    print(" 接口      : /api/board  /api/pc  /api/health  /api/cmd(POST)  /api/shutdown(仅本机)")
    print(" 板子串口  : %s（常驻 115200 8N1，每秒 stats）" % args.serial)
    print(" TCP 透传  : %s:%d <-> %s（给 pc/brain.py 用，一次一个客户端）"
          % (args.tcp_host, hub.tcp_bound_port or args.tcp_port, args.serial))
    print(" 板子锁    : %s" % ("已启用（tools/board_lock.py，进程退出时释放）"
                              if not args.no_lock else "已禁用（--no-lock，仅自检）"))
    print(" 本机数据  : nvidia-smi %s / PowerShell 进程采样 / ctypes CPU"
          % ("可用" if hub.pc_sampler.gpu.available() else "不可用"))
    print("=" * 78)


def run_once_board(args) -> int:
    """--once-board：拿锁、开串口、发一次 stats、打印、释放（排障用）。"""
    board = BoardLink(args.serial, who=args.who, use_lock=not args.no_lock,
                      poll_interval=args.poll_interval)
    board.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = board.snapshot()
        if snap.get("ok") or snap.get("err") in ("board_offline", "board_busy", "board_no_stats"):
            # 再给 no_stats 一点时间（串口可能刚打开）
            time.sleep(1.2)
            snap = board.snapshot()
            print(json.dumps(snap, ensure_ascii=False, indent=2))
            board.stop()
            return 0 if snap.get("ok") else 2
        time.sleep(0.3)
    board.stop()
    print(json.dumps({"ok": False, "err": "timeout"}, ensure_ascii=False))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P4 HID Agent 仪表盘中枢：HTTP(:8765) + 板子串口采集 + 本机数据 + TCP 透传(:8766)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="HTTP 绑定地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP 端口（默认 8765，0=随机）")
    parser.add_argument("--tcp-host", default=DEFAULT_TCP_HOST, help="透传绑定地址（默认 127.0.0.1）")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="透传端口（默认 8766，0=随机）")
    parser.add_argument("--serial", default=DEFAULT_SERIAL,
                        help="板子串口（默认 COM7；auto = 自动识别 CH340/开发板）")
    parser.add_argument("--who", default="web-ui:hub", help="board_lock 持有者标识")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="stats 轮询周期（秒）")
    parser.add_argument("--pc-interval", type=float, default=DEFAULT_PC_INTERVAL, help="本机采样周期（秒）")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="返回的进程数量")
    parser.add_argument("--no-lock", action="store_true", help="不取 board_lock（仅自检/无板调试）")
    parser.add_argument("--no-firewall", action="store_true", help="不尝试自动加防火墙规则")
    parser.add_argument("--once-board", action="store_true", help="只采一次板子数据并打印（排障用）")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    parser.add_argument("--quiet", action="store_true", help="不打印每个 HTTP 请求")
    args = parser.parse_args(argv)

    # Windows 控制台常是 GBK；编码不了的字符降级替换；同时强制行缓冲，
    # 后台重定向到文件时日志不再攒 8 KB 才出现（排障用）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once_board:
        return run_once_board(args)

    # --serial auto：换电脑后 COM 号会变，自动挑最像开发板的那个口
    if str(args.serial).strip().lower() in ("auto", ""):
        try:
            pick = detect_port()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("串口自动识别失败（%r），回退 %s", exc, DEFAULT_SERIAL)
            args.serial = DEFAULT_SERIAL
        else:
            if pick:
                LOG.info("串口自动识别 -> %s（%s）", pick.get("com"),
                         pick.get("vidpid_text") or pick.get("model"))
                args.serial = pick["com"]
            else:
                LOG.warning("没找到像开发板的串口，回退 %s（可用 --serial COMx 指定）",
                            DEFAULT_SERIAL)
                args.serial = DEFAULT_SERIAL

    hub = Hub(args)
    HubHandler.board = hub.board
    HubHandler.pc_store = hub.pc_store
    HubHandler.hub = hub
    HubHandler.quiet = args.quiet

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), HubHandler)
    except OSError as exc:
        print("ERROR: 绑定 %s:%d 失败：%s" % (args.host, args.port, exc), file=sys.stderr)
        print("       端口被占用？换一个：python pc/hub.py --port 8767", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    hub._httpd = httpd

    hub.start()
    # TcpBridge 在后台线程 bind，稍等一下再打印 banner（拿实际端口）
    for _ in range(50):
        if hub.tcp.bound_port:
            break
        time.sleep(0.05)
    hub.tcp_bound_port = hub.tcp.bound_port or args.tcp_port

    if not args.no_firewall:
        ok, msg = try_open_firewall(httpd.server_address[1])
        print("[防火墙] %s" % ("已添加/存在入站规则 dsh-hub" if ok else
                              "自动添加失败（需要管理员权限），手机若打不开请手动执行：\n"
                              "  netsh advfirewall firewall add rule name=\"dsh-hub\" dir=in "
                              "action=allow protocol=TCP localport=%d" % httpd.server_address[1]))

    print_banner(args, httpd, hub)
    print("按 Ctrl+C 退出（或本机 GET /api/shutdown）。", flush=True)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        hub.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

