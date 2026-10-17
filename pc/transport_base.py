# -*- coding: utf-8 -*-
"""pc/transport_base.py — 传输层抽象基类 / Transport abstraction.

协议依据: docs/protocol.md v1.0 §1（物理层 + PC 侧解析规则）、§6.4（PC 侧安全兜底）、§7（性能目标）。

三种实现共用同一接口 / all implementations share one interface:

    transport_uart.UartTransport   ctypes 直调 Win32 串口（无 pyserial）
    transport_tcp.TcpTransport     C6 Wi-Fi 预留 stub（v1.0 未实现）
    mock_p4.MockP4Transport        内置 mock P4（自测/演示，无硬件）

接口 / interface:

    open()                        打开；失败抛 TransportError 子类
    close()                       关闭；幂等，绝不抛异常（安全兜底 §6.4）
    write(data: bytes) -> int     写原始字节（由调用方补 \\n）
    readline(timeout=None) -> str | None
                                  读一行（去掉行尾 CR/LF），超时返回 None
    set_timeout(seconds)          设置默认超时
    timeout                       当前默认超时（秒）
    is_open                       是否已打开
    describe() -> dict            诊断信息（供日志/README 证据）

设计约定 / layering:
    传输层只负责「字节 <-> 行」；**不做** `{...}` 过滤，也**不做** JSON 解析。
    协议 §1 的 PC 侧解析规则（strip 后以 `{` 开头 `}` 结尾才 json.loads，
    非法行静默丢弃）由 agent_bridge.AgentBridge 实现。
"""

from __future__ import annotations

import time

#: protocol.md v1.2 §10.1：单行上限。v1.2 的 stats 响应实测约 1.2 KB
#: （9 个任务 + SD/net/mem 字段），旧的 512 B 会把 stats 整行丢掉，故提到 8192。
MAX_LINE_BYTES = 8192
#: 行缓冲上限：超过仍无换行则判定为 ROM 乱码，整体丢弃（防止乱码撑爆内存）
MAX_BUFFER_BYTES = 65536


class TransportError(RuntimeError):
    """传输层错误基类 / base class for transport failures."""


class PortNotFound(TransportError):
    """端口不存在或无法打开（例如 COM 号错误、板子没插）。"""


class PortDisconnected(TransportError):
    """会话中途断开（USB 拔出）。"""


class TransportNotImplemented(TransportError):
    """该传输后端在 v1.0 未实现（必须是清晰报错，不能假装成功）。"""


class LineBuffer:
    """字节流 -> 文本行。纯缓冲，与协议语义无关。

    - 行分隔符: ``\\n``（同时容忍 ``\\r\\n``）
    - 解码: UTF-8, ``errors="replace"``（ROM 乱码不抛异常）
    - 无换行且超过 ``MAX_BUFFER_BYTES`` -> 计数溢出并丢弃
    """

    def __init__(self, max_line: int = MAX_LINE_BYTES, max_buffer: int = MAX_BUFFER_BYTES):
        self._buf = bytearray()
        self.max_line = max_line
        self.max_buffer = max_buffer
        self.overflow = 0  # 丢弃的乱码块数
        self.lines = 0     # 成功切出的行数

    # -- 内部 ---------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        if len(self._buf) > self.max_buffer and b"\n" not in self._buf:
            # 上电 ROM 乱码：没有换行且越界，丢弃
            self.overflow += 1
            del self._buf[:]

    def next_line(self) -> str | None:
        idx = self._buf.find(b"\n")
        if idx < 0:
            return None
        raw = bytes(self._buf[:idx])
        del self._buf[: idx + 1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if len(raw) > self.max_line:
            # 超过协议帧上限：该行不合规，仍然交出去由上层过滤（并计数）
            self.overflow += 1
        self.lines += 1
        return raw.decode("utf-8", errors="replace")

    def clear(self) -> None:
        del self._buf[:]

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


class Transport:
    """传输层抽象基类。子类必须实现 open/close/write/readline。"""

    #: 人类可读后端名 / human readable backend name
    name = "abstract"
    #: 该后端是否已实现（tcp 预留为 False）
    implemented = True

    def __init__(self, timeout: float = 1.5):
        self.timeout = float(timeout)
        self._open = False
        self._buffer = LineBuffer()
        self.stats: dict[str, int] = {
            "bytes_rx": 0,
            "bytes_tx": 0,
            "lines_rx": 0,
            "overflow": 0,
            "io_errors": 0,
        }

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:
        """关闭连接。**必须幂等且绝不抛异常**（§6.4 安全兜底）。"""
        self._open = False

    # -- I/O ---------------------------------------------------------------
    def write(self, data: bytes) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def readline(self, timeout: float | None = None) -> str | None:
        """读取一行文本；超时返回 ``None``；已缓存的完整行立即返回。"""
        raise NotImplementedError

    def drain(self, timeout: float | None = None) -> bool:
        """把已写入但尚未发出的字节推出去。

        安全兜底（§6.4）需要它：``release_all`` 写进 TX 缓冲后立刻 CloseHandle
        有可能丢掉还没发出去的字节，所以关会话前先 drain。
        默认实现是空操作（内存回环不需要）。
        """
        return True

    # -- 辅助 ---------------------------------------------------------------
    def set_timeout(self, seconds: float) -> None:
        self.timeout = float(seconds)

    @property
    def is_open(self) -> bool:
        return self._open

    def describe(self) -> dict:
        d = {"transport": self.name, "implemented": self.implemented, "open": self._open,
             "timeout_s": self.timeout}
        d.update(self.stats)
        return d

    # 供子类复用的等待原语：轮询式等待（返回剩余时间是否还有效）
    @staticmethod
    def _sleep_poll(seconds: float = 0.001) -> None:
        time.sleep(seconds)

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name} open={self._open}>"


def split_lines(data: bytes) -> list[str]:
    """把一段字节切成若干文本行（末尾不足一行的残留会被丢弃）。

    仅供 mock/测试使用；真实串口走 LineBuffer 以保留跨读残行。
    """
    out: list[str] = []
    for raw in data.split(b"\n"):
        raw = raw.strip(b"\r")
        out.append(raw.decode("utf-8", errors="replace"))
    if out and out[-1] == "":
        out.pop()
    return out