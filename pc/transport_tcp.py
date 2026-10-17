# -*- coding: utf-8 -*-
"""pc/transport_tcp.py — C6 Wi-Fi 预留传输 stub（v1.0 **未实现**）。

协议依据: docs/protocol.md v1.0 §1 —— 物理层只定义了 UART0（115200 8N1）。
本文件是接口预留，**不假装成功**：任何真正要用链路的调用都会抛
``TransportNotImplemented``，并把「为什么没实现 / 计划怎么做 / 现在该怎么做」
写清楚，便于 lead 后续排期。

预留设计（尚未实现，写在这里以免以后重新推导）:

    UART 侧（P4）                       Wi-Fi 侧（C6）                PC 侧
    ------------------------------      ------------------------      --------------------
    UART0 115200 8N1 JSON-lines  <-->   UART 连 P4，TCP server   <-->  TcpTransport
                                         监听 host:port，双向透传

    * 帧格式与 UART **逐字一致**（§1/§2）：UTF-8 JSON、一行一帧、≤512B。
    * C6 只做字节级透传，不做 JSON 解析，因此 PC 侧协议层零改动。
    * 需要额外能力时再议：TCP 侧心跳/SO_KEEPALIVE、断线重连、TLS。
    * 复用时只需实现本文件的 open/write/readline，
      并让 transport_uart.LineBuffer 复用同一套「字节 <-> 行」逻辑。

当前可用替代 / workaround:
    --transport uart --port COM7      （板子 CH340C，115200 8N1）
    --transport mock                  （无硬件自测/演示）
"""

from __future__ import annotations

import socket

from transport_base import (
    LineBuffer,
    Transport,
    TransportError,
    TransportNotImplemented,
)

#: 明确的「未实现」原因（CLI 会原样打印，方便定位）
NOT_IMPLEMENTED_REASON = (
    "TCP 传输在 v1.0 未实现 / TCP transport is NOT implemented in v1.0。\n"
    "  原因: docs/protocol.md §1 只冻结了 UART0 物理层（115200 8N1 JSON-lines），\n"
    "        C6 Wi-Fi 透传链路（P4 UART <-> C6 TCP server <-> PC）尚未纳入契约。\n"
    "  预留设计: ESP32-C6 作为字节级透传桥，TCP server 监听 <host>:<port>，\n"
    "        帧格式与 UART 逐字一致（UTF-8 JSON、一行一帧、≤512B），\n"
    "        PC 侧协议层无需改动，只需换 transport 实现。\n"
    "  现在请用: --transport uart --port COM7   （CH340C, 115200 8N1）\n"
    "            --transport mock               （无硬件自测/演示）"
)


class TcpTransport(Transport):
    """C6 Wi-Fi 预留（stub）。

    接口与 :class:`transport_uart.UartTransport` 完全一致，但 ``open()`` 会明确
    报「未实现」，绝不静默假装连接成功。
    """

    name = "tcp"
    implemented = False

    def __init__(self, host: str = "192.168.4.1", port: int = 8888, timeout: float = 1.5):
        super().__init__(timeout=timeout)
        self.host = str(host)
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._buffer = LineBuffer()

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> None:
        raise TransportNotImplemented(NOT_IMPLEMENTED_REASON)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._open = False
        if sock is not None:  # pragma: no cover - stub
            try:
                sock.close()
            except OSError:
                pass

    # -- I/O（同样明确未实现） ---------------------------------------------
    def write(self, data: bytes) -> int:
        raise TransportNotImplemented(NOT_IMPLEMENTED_REASON)

    def readline(self, timeout: float | None = None) -> str | None:
        raise TransportNotImplemented(NOT_IMPLEMENTED_REASON)

    # -- 诊断 ---------------------------------------------------------------
    def probe(self) -> dict:
        """不建立连接，只回报状态（供 CLI/README 显示）。"""
        return {
            "transport": self.name,
            "implemented": False,
            "host": self.host,
            "port": self.port,
            "reason": NOT_IMPLEMENTED_REASON,
        }

    def describe(self) -> dict:
        d = super().describe()
        d.update({"host": self.host, "port": self.port})
        return d

    @staticmethod
    def list_ports() -> list:
        """TCP 后端没有「端口列表」概念，返回空表（与 uart 接口对齐）。"""
        return []


def open_tcp(host: str, port: int, timeout: float = 1.5) -> TcpTransport:
    """保持与 ``transport_uart.open_uart`` 对称；会抛 TransportNotImplemented。"""
    t = TcpTransport(host=host, port=port, timeout=timeout)
    t.open()
    return t


__all__ = ["TcpTransport", "open_tcp", "NOT_IMPLEMENTED_REASON", "TransportNotImplemented",
           "TransportError"]