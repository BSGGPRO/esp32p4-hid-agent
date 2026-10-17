# -*- coding: utf-8 -*-
"""pc/transport_tcp.py — 通过本地 hub 复用单一 COM7 的 TCP 透传传输。

为什么需要它
------------
板子只有一个 UART 命令通道（COM7）。多个工具（大脑层、绘图、网页仪表盘、固件
烧录）如果各自开串口就会互相踢掉。``pc/hub.py`` 的做法是**独占 COM7**，再监听
``127.0.0.1:8766`` 做**原始字节透传**：每个客户端拿到的字节流与 UART 逐字一致，
帧格式仍是 ``docs/protocol.md`` 的 JSON-lines（UTF-8、一行一帧、≤512B），
所以协议层零改动，只换 transport。

    UART 侧（P4）                  hub（本机进程）                 PC 客户端
    ----------------------        ----------------------         ------------------
    UART0 115200 8N1 JSON  <-->   独占 COM7 + 双向转发      <-->  TcpTransport
                                  监听 127.0.0.1:8766              （brain/draw/…）

行为约定
--------
* ``open()`` 连不上时抛 :class:`TransportError`（**明确报错**，绝不假装成功）；
  ``brain.py`` 会打印原因并回退 ``--transport uart``。
* ``readline()`` 与 UART 实现同语义：超时返回 ``None``，连接被对端关闭抛
  :class:`PortDisconnected`。
* ``close()`` 幂等且绝不抛异常（§6.4 安全兜底）。

命令行自检::

    python pc/transport_tcp.py --host 127.0.0.1 --port 8766 --probe
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

from transport_base import (
    LineBuffer,
    PortDisconnected,
    Transport,
    TransportError,
)

#: 本地 hub 的默认监听地址（web-ui 的 pc/hub.py）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766


class TcpTransport(Transport):
    """本地 TCP 透传客户端（连 hub，不直接碰串口）。"""

    name = "tcp"
    implemented = True

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 1.5, connect_timeout: float = 3.0):
        super().__init__(timeout=timeout)
        self.host = str(host)
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self._sock: socket.socket | None = None
        self._buffer = LineBuffer()

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> None:
        if self._open and self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.connect_timeout)
        except OSError as exc:
            raise TransportError(
                "连不上 hub %s:%d (%s)；hub 是否在跑？"
                "（启动: python pc/hub.py；或改用 --transport uart --port COM7）"
                % (self.host, self.port, exc)) from exc
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        self._open = True
        self._buffer.clear()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._open = False
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- I/O ---------------------------------------------------------------
    def write(self, data: bytes) -> int:
        if not self._open or self._sock is None:
            raise PortDisconnected("TCP transport 未打开")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self._open = False
            raise PortDisconnected("TCP 写入失败: %s" % exc) from exc
        self.stats["bytes_tx"] += len(data)
        return len(data)

    def readline(self, timeout: float | None = None) -> str | None:
        line = self._buffer.next_line()
        if line is not None:
            self.stats["lines_rx"] += 1
            return line
        if not self._open or self._sock is None:
            raise PortDisconnected("TCP transport 未打开")
        wait = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            line = self._buffer.next_line()
            if line is not None:
                self.stats["lines_rx"] += 1
                return line
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            except OSError as exc:
                self._open = False
                raise PortDisconnected("TCP 读取失败: %s" % exc) from exc
            finally:
                try:
                    self._sock.settimeout(None)
                except OSError:
                    pass
            if not chunk:
                self._open = False
                raise PortDisconnected("hub 关闭了 TCP 连接")
            self.stats["bytes_rx"] += len(chunk)
            self._buffer.feed(chunk)

    def drain(self, timeout: float | None = None) -> bool:
        # TCP sendall 已经保证写进内核缓冲；再等一小会儿让 hub 转发
        return True

    # -- 诊断 ---------------------------------------------------------------
    def probe(self, timeout: float = 0.6) -> dict:
        """不建立持久连接，快速探测 hub 是否在监听。"""
        info = {"transport": self.name, "implemented": True,
                "host": self.host, "port": self.port}
        try:
            s = socket.create_connection((self.host, self.port), timeout=timeout)
            s.close()
            info["reachable"] = True
        except OSError as exc:
            info["reachable"] = False
            info["error"] = str(exc)
        return info

    def describe(self) -> dict:
        d = super().describe()
        d.update({"host": self.host, "port": self.port})
        return d

    @staticmethod
    def list_ports() -> list:
        """TCP 后端没有「端口列表」概念，返回空表（与 uart 接口对齐）。"""
        return []


def open_tcp(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
             timeout: float = 1.5) -> TcpTransport:
    """保持与 ``transport_uart.open_uart`` 对称。"""
    t = TcpTransport(host=host, port=port, timeout=timeout)
    t.open()
    return t


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地 hub TCP 透传自检")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--probe", action="store_true", help="只探测 hub 是否在监听")
    ap.add_argument("--send", help="连上后发一条 JSON 行（含换行）并等回一行")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args(argv)
    t = TcpTransport(args.host, args.port, timeout=args.timeout)
    if args.probe:
        print(json.dumps(t.probe(), ensure_ascii=False))
        return 0 if t.probe().get("reachable") else 2
    try:
        t.open()
    except TransportError as exc:
        print("[TCP-ERROR] %s" % exc, file=sys.stderr)
        return 2
    try:
        if args.send:
            t.write((args.send.rstrip("\n") + "\n").encode("utf-8"))
            print(t.readline() or "(timeout)")
        print(json.dumps(t.describe(), ensure_ascii=False))
    finally:
        t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TcpTransport", "open_tcp", "DEFAULT_HOST", "DEFAULT_PORT", "TransportError"]
