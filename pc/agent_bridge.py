# -*- coding: utf-8 -*-
"""pc/agent_bridge.py — PC 桥接 CLI + 协议客户端（逐字实现 docs/protocol.md v1.0.1）。

零第三方依赖（除 Pillow 用于截图）：串口走 transport_uart 的 ctypes/Win32 实现，
**不使用 pyserial**。

实现映射 / protocol -> code:
    §1  逐行读；`{...}` 之外的行静默丢弃；json 解析失败不退出   -> _read_next_dict
    §1  单行 ≤512B；PC 侧负责 text 分块且不切断字符（§9.1）      -> AgentBridge.text/_split_text
    §2  带 id 必回同 id 响应；不带 id = fire-and-forget          -> send_nowait / send_ff
    §2  响应/事件都带 t；未知字段忽略                            -> 只按 key 取值
    §3  全部命令 + 错误码                                        -> KEY/CMD 处理器 + _cmd_* 子命令
    §3.4 rate_limited 退避                                       -> ProtocolError 带 err，CLI 提示退避
    §4  键名表                                                   -> keymap.py
    §6.4 退出（finally/atexit/SIGINT/SIGTERM）必须发 release_all -> safety_cleanup/emergency_release
    §7  ping 往返 / 连发吞吐                                     -> selftest 实测

退出码 / exit codes:
    0  成功
    1  selftest 失败 / verify 期望未满足 / e2e-demo 未检测到变化
    2  用法错误（argparse）
    3  传输错误（端口不存在/被占用/中途拔出）
    4  设备回 ok:false（err）或本地参数校验失败
    5  协议超时（没等到 id 匹配的响应）
    6  TCP 传输未实现（v1.0 预留）
    130 Ctrl+C 中断

CLI::

    python agent_bridge.py --help
    python agent_bridge.py --list-ports
    python agent_bridge.py selftest
    python agent_bridge.py ping
    python agent_bridge.py mouse-move 60 -20 --no-wait
    python agent_bridge.py key-combo leftgui r
    python agent_bridge.py text "hello world"
    python agent_bridge.py screenshot out.png
    python agent_bridge.py e2e-demo
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import threading
import time

import keymap
from keymap import BadArgError, BadKeyError, KeymapError, UnsupportedCharError
from transport_base import (
    MAX_LINE_BYTES,
    PortDisconnected,
    PortNotFound,
    Transport,
    TransportError,
    TransportNotImplemented,
)

VERSION = "1.0.1"
PROTO_VERSION = "1.0"
SUPPORTED_PROTO_VERSIONS = ("1.0",)

#: 单帧硬上限（含 \n 前的 JSON 全文，§9.1）
FRAME_LIMIT = MAX_LINE_BYTES


# ==========================================================================
# 异常
# ==========================================================================
class BridgeError(RuntimeError):
    """本模块错误基类。"""


class LocalValidationError(BridgeError):
    """PC 侧本地校验失败（键名/参数/字符），对应协议错误码。"""

    def __init__(self, message: str, err: str | None = None, detail=None):
        super().__init__(message)
        self.err = err
        self.detail = detail


class ProtocolError(BridgeError):
    """设备回了 ``{"ok":false,"err":...}``。"""

    def __init__(self, resp: dict, cmd: str = ""):
        err = resp.get("err", "unknown")
        detail = resp.get("detail")
        msg = f"设备返回错误 / device error: ok=false err={err}"
        if detail is not None:
            msg += f" detail={detail!r}"
        if cmd:
            msg += f" (cmd={cmd}, id={resp.get('id')})"
        if err == "rate_limited":
            msg += "  -> 已超过 200 条/秒，请退避重试 / back off"
        elif err == "not_mounted":
            msg += "  -> HID 未被主机枚举（检查 USB 线/端口）/ host did not enumerate the HID"
        elif err == "bad_key":
            msg += "  -> 键名不在 §4 表，用 --list-keys 查看 / see --list-keys"
        super().__init__(msg)
        self.err = err
        self.detail = detail
        self.resp = resp
        self.cmd = cmd


# 首帧超时只允许补发的只读命令（重发这些不会有副作用）
_WARMUP_SAFE_CMDS = frozenset({"ping", "status", "keepalive"})

class ProtocolTimeout(BridgeError):
    """在超时内没有收到 id 匹配的响应（§2）。"""

    def __init__(self, msg_id: int, cmd: str, timeout: float, seen: list | None = None):
        msg = (f"协议超时 / timeout: 等 id={msg_id} 的响应超过 {timeout:.2f}s (cmd={cmd})\n"
               f"  * 检查串口是否选对（--list-ports）、波特率是否 115200\n"
               f"  * 板子可能没插好/没烧固件；也可能是 ROM 乱码行被正确丢弃后没有响应\n"
               f"  * 也可用 --transport mock 先验证 PC 侧链路")
        if seen:
            msg += f"\n  超时前收到的最后报文: {seen[-3:]}"
        super().__init__(msg)
        self.msg_id = msg_id
        self.cmd = cmd
        self.timeout = timeout


# ==========================================================================
# 统计
# ==========================================================================
class BridgeStats:
    def __init__(self):
        self.tx_frames = 0
        self.tx_bytes = 0
        self.rx_frames = 0          # 有效 JSON 对象
        self.garbage_lines = 0      # §1 静默丢弃的非 {...} 行
        self.bad_json = 0           # {...} 但 json 解析失败
        self.responses = 0
        self.orphans = 0            # id 不匹配（已缓存）
        self.events = 0
        self.timeouts = 0
        self.releases_sent = 0
        self.max_frame_len = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ==========================================================================
# 安全兜底：atexit + 信号（§6.4）
# ==========================================================================
_ACTIVE_BRIDGES: list["AgentBridge"] = []
_CLEANUP_LOCK = threading.Lock()
_SIGNALS_INSTALLED = False


def register_bridge(bridge: "AgentBridge") -> None:
    with _CLEANUP_LOCK:
        if bridge not in _ACTIVE_BRIDGES:
            _ACTIVE_BRIDGES.append(bridge)


def unregister_bridge(bridge: "AgentBridge") -> None:
    with _CLEANUP_LOCK:
        try:
            _ACTIVE_BRIDGES.remove(bridge)
        except ValueError:
            pass


def safety_cleanup() -> None:
    """进程退出前把所有活动会话的按键/按钮释放掉。**绝不抛异常**。"""
    with _CLEANUP_LOCK:
        bridges = list(_ACTIVE_BRIDGES)
    for b in bridges:
        try:
            b.emergency_release()
        except Exception:  # noqa: BLE001 - 退出路径必须吞掉一切
            pass
    for b in bridges:
        try:
            b.close(release=False)
        except Exception:  # noqa: BLE001
            pass


atexit.register(safety_cleanup)


def setup_io() -> None:
    """控制台编码兜底。

    - 交互式控制台：保留系统编码（GBK 下中文正常显示），不可编码字符退化为 '?'
    - 重定向/管道（采集验收证据、CI）：强制 UTF-8，避免 GBK 字节被下游按 UTF-8 解读成乱码
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
            if is_tty:
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 极少数流不支持 reconfigure
            pass


def install_signal_handlers(verbose: bool = False) -> None:
    """Ctrl+C / SIGTERM 也要 release_all（§6.4）。"""
    global _SIGNALS_INSTALLED
    if _SIGNALS_INSTALLED:
        return
    _SIGNALS_INSTALLED = True

    def _handler(signum, frame):
        if verbose:
            print(f"\n[安全兜底] 收到信号 {signum}，发送 release_all / signal caught, releasing", file=sys.stderr)
        safety_cleanup()
        raise SystemExit(128 + int(signum))

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass


# ==========================================================================
# 协议客户端
# ==========================================================================
class AgentBridge:
    """protocol.md v1.0.1 的 PC 侧客户端。

    线程安全策略：单线程使用（CLI 场景）；``emergency_release`` 会被信号处理器
    从主线程调用，``_closed`` 标志保证幂等。
    """

    QUERY_COMMANDS = {"ping", "status", "set"}

    def __init__(self, transport: Transport, timeout: float = 1.5, *,
                 verbose: bool = False, quiet: bool = False,
                 on_event=None, no_wait: bool = False,
                 release_on_close: bool = True):
        self.transport = transport
        self.timeout = float(timeout)
        self.verbose = verbose
        self.quiet = quiet
        self.on_event = on_event
        self.no_wait = no_wait
        self.release_on_close = release_on_close
        self.stats = BridgeStats()
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._seen: list[dict] = []
        self._keys_held: set[str] = set()
        self._buttons_held: set[str] = set()
        self._released_clean = False   # 已显式 release 且此后没有新的按下
        self._closed = False
        self._in_context = False
        self._first_request_done = False   # 首帧重试用（见 request()）

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> "AgentBridge":
        self.transport.open()
        self._closed = False
        self._first_request_done = False
        register_bridge(self)
        return self

    def __enter__(self) -> "AgentBridge":
        self._in_context = True
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self, release: bool | None = None) -> None:
        """释放（§6.4）并关闭传输；幂等、绝不抛异常。"""
        if self._closed:
            return
        self._closed = True
        do_release = self.release_on_close if release is None else bool(release)
        if do_release:
            self.emergency_release()
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001
            pass
        unregister_bridge(self)

    # -- 危险状态跟踪（关掉会话前要不要 release_all） -----------------------
    @property
    def danger(self) -> bool:
        """本会话是否按下过键/按钮（key_tap/text 是自动抬起的，不算）。"""
        return bool(self._keys_held or self._buttons_held)

    def _mark_down(self, canon_names) -> None:
        for n in canon_names:
            self._keys_held.add(n)
        self._released_clean = False

    def _mark_up(self, canon_names) -> None:
        for n in canon_names:
            self._keys_held.discard(n)

    def _mark_button_down(self, btn: str) -> None:
        self._buttons_held.add(btn)
        self._released_clean = False

    # -- 底层收发 -----------------------------------------------------------
    def _next_id(self) -> int:
        self._id = (self._id % 9007199254740991) + 1
        return self._id

    @staticmethod
    def encode_obj(obj: dict) -> bytes:
        """紧凑 UTF-8 JSON + ``\\n``（§1 一行一帧）。"""
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    def _write_obj(self, obj: dict) -> int:
        data = self.encode_obj(obj)
        if len(data) > FRAME_LIMIT:
            raise LocalValidationError(
                f"拒绝发送超长帧 / refusing oversized frame: {len(data)}B > {FRAME_LIMIT}B (§9.1)")
        n = self.transport.write(data)
        self.stats.tx_frames += 1
        self.stats.tx_bytes += n
        self.stats.max_frame_len = max(self.stats.max_frame_len, len(data) - 1)
        if self.verbose:
            print(f"[tx] {data[:-1].decode('utf-8', 'replace')}", file=sys.stderr)
        return n

    def _handle_event(self, obj: dict) -> None:
        self.stats.events += 1
        if self.verbose:
            print(f"[evt] {json.dumps(obj, ensure_ascii=False)}", file=sys.stderr)
        if self.on_event is not None:
            try:
                self.on_event(obj)
            except Exception:  # noqa: BLE001
                pass

    def _stash_response(self, obj: dict) -> dict:
        """缓存一条响应（id 匹配时直接返回，否则进 _pending）。"""
        self.stats.responses += 1
        self._seen.append(obj)
        del self._seen[:-16]
        mid = obj.get("id")
        if isinstance(mid, int):
            self._pending[mid] = obj
        else:
            self.stats.orphans += 1
        return obj

    def _read_next_dict(self, deadline: float) -> dict | None:
        """§1：逐行读，只认 ``{...}``，json 失败静默丢弃；超时返回 None。"""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            line = self.transport.readline(remaining)
            if line is None:
                return None
            text = line.strip()
            if not (text.startswith("{") and text.endswith("}")):
                self.stats.garbage_lines += 1          # 上电 ROM 乱码：静默丢弃
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                self.stats.bad_json += 1               # 解析失败不退出
                continue
            if not isinstance(obj, dict):
                self.stats.bad_json += 1
                continue
            self.stats.rx_frames += 1
            if self.verbose:
                print(f"[rx] {text}", file=sys.stderr)
            return obj

    # -- 请求 ---------------------------------------------------------------
    def send_ff(self, cmd: str, **fields) -> dict:
        """fire-and-forget：不带 id，不等待（§2，用于低延迟连发）。"""
        obj = {"cmd": cmd}
        obj.update({k: v for k, v in fields.items() if v is not None})
        self._write_obj(obj)
        return obj

    def send_nowait(self, cmd: str, **fields) -> int:
        """带 id 发出但先不等待（响应可用 wait_for 取）。"""
        mid = self._next_id()
        obj = {"cmd": cmd, "id": mid}
        obj.update({k: v for k, v in fields.items() if v is not None})
        self._write_obj(obj)
        return mid

    def wait_for(self, msg_id: int, timeout: float | None = None, cmd: str = "") -> dict:
        """等 id 匹配的响应；期间把事件回调掉、把无关响应缓存起来。"""
        cached = self._pending.pop(msg_id, None)
        if cached is not None:
            return cached
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            obj = self._read_next_dict(deadline)
            if obj is None:
                self.stats.timeouts += 1
                raise ProtocolTimeout(msg_id, cmd, limit, list(self._seen))
            if "evt" in obj and "ok" not in obj:
                self._handle_event(obj)
                continue
            rid = obj.get("id")
            if rid == msg_id:                          # §9.4 数值相等
                self.stats.responses += 1
                self._seen.append(obj)
                del self._seen[:-16]
                return obj
            self.stats.orphans += 1
            self._stash_response(obj)

    def request(self, cmd: str, wait: bool = True, **fields) -> dict | None:
        """通用入口：wait=False -> fire-and-forget（返回 None 之外的发送对象）。"""
        if not wait:
            return self.send_ff(cmd, **fields)
        mid = self.send_nowait(cmd, **fields)
        try:
            resp = self.wait_for(mid, cmd=cmd)
        except ProtocolTimeout:
            # 实测（真板，见 docs/bringup-runbook.md）：刚烧录完 / 刚插上串口时，
            # 打开端口会让 CH340 的 DTR/RTS 抖动一下，板子重新上电；此时第一帧
            # 落在固件 UART RX 任务起来之前，会被丢掉。表现为「第一次 ping 超时、
            # 紧接着所有命令都正常」。
            # 只对只读命令补发一次（绝不重发会动键鼠的命令，避免重复执行）。
            if self._first_request_done or cmd not in _WARMUP_SAFE_CMDS:
                raise
            time.sleep(0.12)
            mid = self.send_nowait(cmd, **fields)
            resp = self.wait_for(mid, cmd=cmd)
        self._first_request_done = True
        return resp

    def command(self, cmd: str, wait: bool = True, **fields) -> dict:
        """同 request，但 ``ok:false`` 时抛 :class:`ProtocolError`。"""
        resp = self.request(cmd, wait=wait, **fields)
        if resp is None:
            return {}
        if resp.get("ok") is False:
            raise ProtocolError(resp, cmd=cmd)
        return resp

    def poll_events(self, timeout: float = 0.0) -> list[dict]:
        """在 ``timeout`` 秒内收集异步事件（§3.5）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        events: list[dict] = []
        while True:
            obj = self._read_next_dict(deadline)
            if obj is None:
                break
            if "evt" in obj and "ok" not in obj:
                events.append(obj)
                self._handle_event(obj)
            else:
                self._stash_response(obj)
        return events

    # -- 安全释放 -----------------------------------------------------------
    def release_all(self, wait: bool = True, timeout: float | None = None) -> dict | None:
        """清除全部按键/按钮（HID 全 0）。协议 §9.3：永远 ok:true。"""
        self._keys_held.clear()
        self._buttons_held.clear()
        resp = self.request("release_all", wait=wait)
        self.stats.releases_sent += 1
        self._released_clean = True
        if wait and resp is not None and resp.get("ok") is False:
            raise ProtocolError(resp, cmd="release_all")
        return resp

    def emergency_release(self, force: bool = False) -> bool:
        """退出路径专用：不带 id 发 release_all + 排空 TX。**绝不抛异常**（§6.4）。"""
        if self._released_clean and not self.danger and not force:
            return False            # 已经显式释放过且之后没有新的按下：省掉多余一帧
        try:
            if not self.transport.is_open:
                return False
        except Exception:  # noqa: BLE001
            return False
        try:
            self._write_obj({"cmd": "release_all"})
            self.stats.releases_sent += 1
        except Exception:  # noqa: BLE001 - 端口已拔：忽略（§6.4）
            return False
        try:
            self.transport.drain()
        except Exception:  # noqa: BLE001
            pass
        self._keys_held.clear()
        self._buttons_held.clear()
        self._released_clean = True
        return True

    # -- §3.1 探活/状态 -----------------------------------------------------
    def ping(self, wait: bool = True) -> dict:
        return self.command("ping", wait=wait)

    def status(self, wait: bool = True) -> dict:
        return self.command("status", wait=wait)

    def keepalive(self, wait: bool = True) -> dict | None:
        return self.request("keepalive", wait=wait)

    def set_value(self, key: str, value: int, wait: bool = True) -> dict:
        if not isinstance(key, str):
            raise LocalValidationError("set 的 key 必须是字符串", err="bad_arg")
        if isinstance(value, bool) or not isinstance(value, int):
            raise LocalValidationError("set 的 value 必须是整数（§9.4）", err="bad_arg")
        if key == "safety_timeout_ms" and not (0 <= value <= 60000):
            raise LocalValidationError("safety_timeout_ms 合法范围 0-60000（§9.4）", err="bad_arg")
        return self.command("set", wait=wait, key=key, value=value)

    def set_safety_timeout(self, ms: int, wait: bool = True) -> dict:
        return self.set_value("safety_timeout_ms", ms, wait=wait)

    # -- §3.2 键盘 ----------------------------------------------------------
    def key_down(self, keys, wait: bool = True) -> dict:
        canon = keymap.validate_keys(keys)
        resp = self.command("key_down", wait=wait, keys=canon)
        self._mark_down(canon)
        return resp

    #: 协议里 `key` 与 `key_down` 同义（都是按下并保持）
    key = key_down

    def key_up(self, keys, wait: bool = True) -> dict:
        canon = keymap.validate_keys(keys)
        resp = self.command("key_up", wait=wait, keys=canon)
        self._mark_up(canon)
        return resp

    def key_combo(self, keys, hold_ms: int = 30, wait: bool = True) -> dict:
        """组合键 = ``key_tap``（按下 -> hold_ms -> 全部抬起，§3.2）。"""
        canon = keymap.validate_keys(keys)
        if isinstance(hold_ms, bool) or not isinstance(hold_ms, int):
            raise LocalValidationError("hold_ms 必须是整数", err="bad_arg")
        if hold_ms < 0 or hold_ms > 10000:
            raise LocalValidationError("hold_ms 合法范围 0-10000", err="bad_arg")
        return self.command("key_tap", wait=wait, keys=canon, hold_ms=hold_ms)

    def text(self, value: str, cps: int = 40, wait: bool = True) -> list[dict | None]:
        """逐字符输入；长文本自动分块（§9.1），**不切断字符**。"""
        keymap.validate_text(value)                 # 非 ASCII -> UnsupportedCharError
        if isinstance(cps, bool) or not isinstance(cps, int):
            raise LocalValidationError("cps 必须是整数", err="bad_arg")
        if cps <= 0 or cps > 1000:
            raise LocalValidationError("cps 合法范围 1-1000", err="bad_arg")
        out: list[dict | None] = []
        for chunk in self.split_text(value, cps=cps, with_id=wait):
            out.append(self.request("text", wait=wait, text=chunk, cps=cps))
        return out

    def split_text(self, value: str, cps: int = 40, with_id: bool = True) -> list[str]:
        """把 text 切成每帧 ≤512B 的分块，按字符切（不切断字符，§9.1）。"""
        probe = {"cmd": "text", "text": "", "cps": cps}
        if with_id:
            probe["id"] = 9999999999                # 按最坏 id 宽度预留，保证任何 id 都 ≤512B
        budget = FRAME_LIMIT - len(self.encode_obj(probe))
        chunks: list[str] = []
        cur: list[str] = []
        used = 0
        for ch in value:
            cost = len(json.dumps(ch, ensure_ascii=False).encode("utf-8")) - 2
            if cur and used + cost > budget:
                chunks.append("".join(cur))
                cur, used = [], 0
            cur.append(ch)
            used += cost
        if cur:
            chunks.append("".join(cur))
        return chunks

    # -- §3.3 鼠标 ----------------------------------------------------------
    @staticmethod
    def _check_delta(v: int, name: str) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LocalValidationError(f"{name} 必须是整数", err="bad_arg")
        if v < -32767 or v > 32767:
            raise LocalValidationError(f"{name} 合法范围 -32767..32767", err="bad_arg")
        return v

    def mouse_move(self, dx: int, dy: int, wait: bool = True) -> dict:
        dx = self._check_delta(dx, "dx")
        dy = self._check_delta(dy, "dy")
        return self.command("mouse_move", wait=wait, dx=dx, dy=dy)

    def mouse_scroll(self, dy: int, wait: bool = True) -> dict:
        dy = self._check_delta(dy, "dy")
        return self.command("mouse_scroll", wait=wait, dy=dy)

    def mouse_btn(self, btn: str = "left", action: str = "click",
                  click_ms: int | None = None, wait: bool = True) -> dict:
        if btn not in ("left", "right", "middle"):
            raise LocalValidationError("btn 必须是 left|right|middle", err="bad_arg")
        if action not in ("down", "up", "click"):
            raise LocalValidationError("action 必须是 down|up|click", err="bad_arg")
        fields = {"btn": btn, "action": action}
        if click_ms is not None:
            if isinstance(click_ms, bool) or not isinstance(click_ms, int) or click_ms < 0:
                raise LocalValidationError("click_ms 必须是非负整数", err="bad_arg")
            fields["click_ms"] = click_ms
        resp = self.command("mouse_btn", wait=wait, **fields)
        if action == "down":
            self._mark_button_down(btn)
        elif action == "up":
            self._buttons_held.discard(btn)
        return resp

    def mouse_click(self, btn: str = "left", count: int = 1,
                    interval_ms: int = 80, wait: bool = True) -> dict:
        if btn not in ("left", "right", "middle"):
            raise LocalValidationError("btn 必须是 left|right|middle", err="bad_arg")
        if isinstance(count, bool) or not isinstance(count, int) or not (1 <= count <= 1000):
            raise LocalValidationError("count 合法范围 1-1000", err="bad_arg")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or not (0 <= interval_ms <= 10000):
            raise LocalValidationError("interval_ms 合法范围 0-10000", err="bad_arg")
        return self.command("mouse_click", wait=wait, btn=btn, count=count, interval_ms=interval_ms)

    # -- 诊断 ---------------------------------------------------------------
    def describe(self) -> dict:
        d = {
            "proto": PROTO_VERSION,
            "client_version": VERSION,
            "stats": self.stats.as_dict(),
            "keys_held": sorted(self._keys_held),
            "buttons_held": sorted(self._buttons_held),
            "transport": self.transport.describe(),
        }
        return d


# ==========================================================================
# CLI
# ==========================================================================
def _bilingual(zh: str, en: str) -> str:
    return f"{zh} / {en}"


def _add_common(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """全局参数。subparser 上以 SUPPRESS 默认值再挂一遍，实现「参数可写在命令前或后」。"""
    S = argparse.SUPPRESS

    def d(v):
        return S if suppress else v

    parser.add_argument("--transport", choices=["uart", "tcp", "mock"], default=d("uart"),
                        help=_bilingual("传输后端：uart=真板子 / tcp=C6预留未实现 / mock=内存回环自测",
                                        "transport backend: uart=real board / tcp=stub / mock=loopback"))
    parser.add_argument("--port", "-p", default=d(None), metavar="PORT",
                        help=_bilingual("串口名（uart: COM7）或 TCP 端口号（tcp: 8888）",
                                        "serial port (uart: COM7) or TCP port (tcp: 8888)"))
    parser.add_argument("--baud", type=int, default=d(115200),
                        help=_bilingual("波特率（默认 115200 8N1，协议 §1）", "baud rate (default 115200 8N1)"))
    parser.add_argument("--host", default=d("192.168.4.1"),
                        help=_bilingual("tcp 后端主机（预留）", "TCP host (stub)"))
    parser.add_argument("--timeout", type=float, default=d(1.5), metavar="SEC",
                        help=_bilingual("协议响应超时秒数（默认 1.5）", "response timeout seconds (default 1.5)"))
    parser.add_argument("--no-wait", action="store_true", default=d(False),
                        help=_bilingual("连发模式：不带 id、不等响应（§2，用于低延迟连发）",
                                        "fire-and-forget: no id, no ack (low-latency bursts)"))
    parser.add_argument("--repeat", type=int, default=d(1), metavar="N",
                        help=_bilingual("动作命令重复 N 次（配合 --no-wait 做连发）", "repeat action N times"))
    parser.add_argument("--no-release-on-exit", action="store_true", default=d(False),
                        help=_bilingual("退出时不发 release_all（默认**会**发，§6.4；跨进程按住键时才需要）",
                                        "do NOT send release_all on exit (default sends it, §6.4)"))
    parser.add_argument("--json", action="store_true", default=d(False),
                        help=_bilingual("原始 JSON 输出", "raw JSON output"))
    parser.add_argument("--stats", action="store_true", default=d(False),
                        help=_bilingual("结束时打印收发统计", "print tx/rx stats at the end"))
    parser.add_argument("--quiet", "-q", action="store_true", default=d(False),
                        help=_bilingual("安静模式", "quiet"))
    parser.add_argument("--verbose", "-v", action="store_true", default=d(False),
                        help=_bilingual("打印每条收发帧", "log every frame"))
    parser.add_argument("--list-ports", action="store_true", default=d(False),
                        help=_bilingual("列出本机串口并识别板子（无硬件也能跑，exit 0）",
                                        "list serial ports and identify the board"))
    parser.add_argument("--list-keys", action="store_true", default=d(False),
                        help=_bilingual("打印 protocol.md §4 键名表", "print the §4 key table"))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)

    parser = argparse.ArgumentParser(
        prog="agent_bridge.py",
        parents=[common],
        description=(
            "JC-ESP32P4-M3 HID Agent — PC 桥接 CLI（protocol.md v1.0.1，零第三方依赖）\n"
            "PC bridge CLI for the P4 HID agent (protocol v1.0.1, stdlib + Pillow only)."
        ),
        epilog=(
            "常用示例 / examples:\n"
            "  python agent_bridge.py --list-ports                 # 列串口并识别板子\n"
            "  python agent_bridge.py selftest                     # 内置 mock P4 自测（无需硬件）\n"
            "  python agent_bridge.py ping                         # 探活\n"
            "  python agent_bridge.py mouse-move 60 -20            # 相对移动\n"
            "  python agent_bridge.py key-combo leftgui r          # Win+R\n"
            "  python agent_bridge.py text \"hello world\"           # 逐字符输入\n"
            "  python agent_bridge.py mouse-move 5 0 --repeat 100 --no-wait   # 连发\n"
            "  python agent_bridge.py screenshot shot.png --min-bytes 51200   # 全屏截图\n"
            "  python agent_bridge.py e2e-demo                     # 截屏->移动->截屏->差分\n"
            "\n"
            "退出码/exit: 0=OK 1=自测/断言失败 2=用法 3=传输 4=设备错误 5=超时 6=TCP未实现 130=中断\n"
            "\n"
            "注意 / note: 默认退出时会发 release_all（§6.4）。要跨进程按住键，请加 --no-release-on-exit。\n"
            "By default the bridge sends release_all on exit (§6.4); use --no-release-on-exit to hold keys across runs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, help_zh, help_en, aliases=()):
        p = sub.add_parser(name, parents=[_suppressed_common()], aliases=list(aliases),
                           help=_bilingual(help_zh, help_en),
                           description=_bilingual(help_zh, help_en),
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        return p

    add("ping", "探活：ping -> pong/fw/proto/caps", "liveness probe (ping/pong)")
    add("status", "查询 HID 状态与计数器", "query HID status/counters")
    add("keepalive", "刷新固件安全超时", "refresh firmware safety timeout")
    p = add("set", "设置固件参数（v1: safety_timeout_ms 0-60000）", "set firmware parameter")
    p.add_argument("key", help=_bilingual("参数名，如 safety_timeout_ms", "parameter name"))
    p.add_argument("value", type=int, help=_bilingual("整数值", "integer value"))

    p = add("mouse-move", "鼠标相对移动 DX DY（dy 正=向下）", "relative mouse move DX DY")
    p.add_argument("dx", type=int)
    p.add_argument("dy", type=int)

    p = add("mouse-scroll", "滚轮 DY（正=上滚）", "mouse wheel DY (positive = up)")
    p.add_argument("dy", type=int)

    p = add("mouse-click", "鼠标连击", "mouse click")
    p.add_argument("--btn", default="left", choices=["left", "right", "middle"])
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval-ms", type=int, default=80, dest="interval_ms")

    p = add("mouse-btn", "鼠标按键 down/up/click", "mouse button down/up/click")
    p.add_argument("--btn", default="left", choices=["left", "right", "middle"])
    p.add_argument("--action", default="click", choices=["down", "up", "click"])
    p.add_argument("--click-ms", type=int, default=None, dest="click_ms")

    p = add("key-combo", "组合键：按下->hold_ms->抬起（key_tap）", "key combo (key_tap)")
    p.add_argument("keys", nargs="+")
    p.add_argument("--hold-ms", type=int, default=30, dest="hold_ms")

    p = add("key-down", "按下并保持（不自动抬起）", "press and hold keys")
    p.add_argument("keys", nargs="+")

    p = add("key", "同 key-down（协议别名）", "alias of key-down", aliases=["key-hold"])
    p.add_argument("keys", nargs="+")

    p = add("key-up", "抬起指定键", "release keys")
    p.add_argument("keys", nargs="+")

    add("release-all", "清空全部按键与鼠标按钮（HID 全 0）", "release everything (HID all-zero)")

    p = add("text", "逐字符输入（仅 ASCII 0x20-0x7E）", "type text (ASCII printable only)")
    p.add_argument("value")
    p.add_argument("--cps", type=int, default=40)

    p = add("screenshot", "全屏截图存 PNG", "full-screen screenshot to PNG")
    p.add_argument("out")
    p.add_argument("--primary", action="store_true", help=_bilingual("只抓主屏", "primary screen only"))
    p.add_argument("--region", metavar="x,y,w,h", help=_bilingual("裁剪区域", "crop region"))
    p.add_argument("--min-bytes", type=int, default=0, dest="min_bytes",
                   help=_bilingual("PNG 小于该字节数则 exit 1（验收用，如 51200）",
                                   "fail if PNG smaller than N bytes"))

    p = add("e2e-demo", "端到端演示：截屏->鼠标移动->截屏->差分", "end-to-end demo: shot->move->shot->diff")
    p.add_argument("--dx", type=int, default=200)
    p.add_argument("--dy", type=int, default=0)
    p.add_argument("--settle", type=float, default=0.35,
                   help=_bilingual("移动后等待秒数", "settle seconds after move"))
    p.add_argument("--outdir", default=".", help=_bilingual("截图输出目录", "screenshot output dir"))
    p.add_argument("--primary", action="store_true")
    p.add_argument("--region", metavar="x,y,w,h")
    p.add_argument("--min-pixels", type=int, default=1, dest="min_pixels")

    p = add("events", "监听异步事件 N 秒（§3.5）", "listen for async events")
    p.add_argument("--seconds", type=float, default=3.0)

    add("list-ports", "别名：--list-ports", "alias of --list-ports")
    add("list-keys", "别名：--list-keys", "alias of --list-keys")
    add("selftest", "内置 mock P4 自测（无需硬件）", "built-in mock P4 selftest (no hardware)")

    p = add("verify", "比较两张 PNG 的像素差异", "pixel-diff two PNGs")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--region", metavar="x,y,w,h")
    p.add_argument("--threshold", type=int, default=0)
    p.add_argument("--min-ratio", type=float, default=0.0, dest="min_ratio")
    p.add_argument("--min-pixels", type=int, default=1, dest="min_pixels")
    p.add_argument("--expect", choices=["change", "nochange"], default=None)

    return parser


def _suppressed_common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    _add_common(p, suppress=True)
    return p


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------
def _emit(args, resp: dict, summary: str, stream=None) -> None:
    stream = stream or sys.stdout
    if getattr(args, "json", False) and isinstance(resp, dict):
        print(json.dumps(resp, ensure_ascii=False), file=stream)
    else:
        print(summary, file=stream)


def _exit_for_resp(resp: dict | None) -> int:
    if isinstance(resp, dict) and resp.get("ok") is False:
        return 4
    return 0


def _warn(args, msg: str) -> None:
    if not getattr(args, "quiet", False):
        print(f"[警告/warn] {msg}", file=sys.stderr)


def _wait_for(args, cmd: str) -> bool:
    """--no-wait 对查询类命令无效（需要响应）。"""
    if cmd in AgentBridge.QUERY_COMMANDS and getattr(args, "no_wait", False):
        _warn(args, f"{cmd} 需要响应，已忽略 --no-wait / {cmd} needs a reply, ignoring --no-wait")
        return True
    return not getattr(args, "no_wait", False)


def _event_printer(args):
    quiet = getattr(args, "quiet", False)
    as_json = getattr(args, "json", False)

    def _cb(evt: dict) -> None:
        if quiet:
            return
        if as_json:
            print(json.dumps({"event": evt}, ensure_ascii=False), file=sys.stderr)
        else:
            name = evt.get("evt")
            extra = {k: v for k, v in evt.items() if k not in ("evt", "t")}
            print(f"[事件/evt] {name} t={evt.get('t')} {extra if extra else ''}".rstrip(), file=sys.stderr)
    return _cb


# --------------------------------------------------------------------------
# 需要传输的命令
# --------------------------------------------------------------------------
def make_transport(args) -> Transport:
    backend = args.transport
    if backend == "mock":
        from mock_p4 import MockP4Transport
        return MockP4Transport(timeout=args.timeout)
    if backend == "tcp":
        from transport_tcp import TcpTransport
        port = args.port
        if port is None:
            port = 8888
        else:
            try:
                port = int(str(port).strip())
            except ValueError:
                raise TransportError(
                    f"--transport tcp 的 --port 必须是端口号（数字），收到 {port!r}；"
                    f"串口名请配合 --transport uart 使用") from None
        return TcpTransport(host=args.host, port=port, timeout=args.timeout)
    # uart
    import transport_uart
    port = args.port
    if not port:
        cand = transport_uart.detect_port()
        if cand is None:
            raise PortNotFound(
                "没有指定 --port，且自动识别不到开发板串口 / no --port and no board port auto-detected.\n"
                "  * 先跑 --list-ports 看看本机串口\n"
                "  * 或用 --port COM7 显式指定\n"
                "  * 只想验证 PC 侧链路可用 --transport mock")
        if not getattr(args, "quiet", False):
            print(f"[info] 自动选择串口 / auto-selected: {cand['com']} ({cand['advice']})", file=sys.stderr)
        port = cand["com"]
    return transport_uart.UartTransport(port=port, baud=args.baud, timeout=args.timeout)


def run_with_transport(args) -> int:
    transport = make_transport(args)
    if args.transport == "mock" and not getattr(args, "quiet", False):
        print("[info] mock 传输：仅协议回环，不驱动真实 HID / mock: protocol loopback only",
              file=sys.stderr)
    bridge = AgentBridge(
        transport,
        timeout=args.timeout,
        verbose=args.verbose,
        quiet=args.quiet,
        on_event=_event_printer(args),
        no_wait=args.no_wait,
        release_on_close=not args.no_release_on_exit,
    )
    with bridge:
        handler = HANDLERS[args.command]
        rc = handler(args, bridge)
        if getattr(args, "stats", False):
            print(json.dumps(bridge.describe(), ensure_ascii=False, indent=2), file=sys.stderr)
        return rc


# -- 各命令 ----------------------------------------------------------------
def cmd_ping(args, bridge: AgentBridge) -> int:
    resp = bridge.ping(wait=_wait_for(args, "ping"))
    if resp.get("ok") is False:
        _emit(args, resp, f"PING FAILED err={resp.get('err')}")
        return 4
    proto = resp.get("proto")
    if proto not in SUPPORTED_PROTO_VERSIONS:
        _warn(args, f"固件 proto={proto!r} 不在 PC 支持列表 {SUPPORTED_PROTO_VERSIONS}（§8/§9）")
    summary = (f"PING OK  fw={resp.get('fw')}  proto={proto}  uptime_ms={resp.get('uptime_ms')}  "
               f"host_mounted={resp.get('host_mounted')}  caps={','.join(resp.get('caps') or [])}")
    _emit(args, resp, summary)
    return 0


def cmd_status(args, bridge: AgentBridge) -> int:
    resp = bridge.status(wait=_wait_for(args, "status"))
    if resp.get("ok") is False:
        _emit(args, resp, f"STATUS FAILED err={resp.get('err')}")
        return 4
    _emit(args, resp,
          f"STATUS OK  t={resp.get('t')}  host_mounted={resp.get('host_mounted')}  "
          f"kbd_keys={resp.get('kbd_keys')}  mouse_btn={resp.get('mouse_btn')}  "
          f"rx_ok={resp.get('rx_ok')}  rx_bad={resp.get('rx_bad')}  "
          f"safety_timeout_ms={resp.get('safety_timeout_ms')}")
    return 0


def cmd_keepalive(args, bridge: AgentBridge) -> int:
    resp = bridge.keepalive(wait=_wait_for(args, "keepalive"))
    if resp is None:
        print(f"KEEPALIVE sent (fire-and-forget)  repeat={args.repeat}")
        return 0
    _emit(args, resp, f"KEEPALIVE OK  t={resp.get('t')}")
    return 0


def cmd_set(args, bridge: AgentBridge) -> int:
    resp = bridge.set_value(args.key, args.value, wait=_wait_for(args, "set"))
    _emit(args, resp, f"SET OK  {resp.get('key')}={resp.get('value')}")
    return 0


def cmd_mouse_move(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "mouse_move")
    last = None
    for _ in range(max(1, args.repeat)):
        last = bridge.mouse_move(args.dx, args.dy, wait=wait)
    if wait and last is not None:
        _warn_if_mounted(args, last)
        _emit(args, last, f"MOUSE_MOVE OK  dx={last.get('dx')} dy={last.get('dy')} "
                          f"reports={last.get('reports')}  repeat={args.repeat}")
        return _exit_for_resp(last)
    print(f"MOUSE_MOVE sent (fire-and-forget)  dx={args.dx} dy={args.dy} repeat={args.repeat}")
    return 0


def cmd_mouse_scroll(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "mouse_scroll")
    last = None
    for _ in range(max(1, args.repeat)):
        last = bridge.mouse_scroll(args.dy, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"MOUSE_SCROLL OK  dy={last.get('dy')} reports={last.get('reports')}")
        return _exit_for_resp(last)
    print(f"MOUSE_SCROLL sent (fire-and-forget)  dy={args.dy} repeat={args.repeat}")
    return 0


def cmd_mouse_click(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "mouse_click")
    last = None
    for _ in range(max(1, args.repeat)):
        last = bridge.mouse_click(args.btn, args.count, args.interval_ms, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"MOUSE_CLICK OK  btn={last.get('btn')} count={last.get('count')} "
                          f"interval_ms={last.get('interval_ms')}  repeat={args.repeat}")
        return _exit_for_resp(last)
    print(f"MOUSE_CLICK sent (fire-and-forget)  btn={args.btn} count={args.count} repeat={args.repeat}")
    return 0


def cmd_mouse_btn(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "mouse_btn")
    last = bridge.mouse_btn(args.btn, args.action, args.click_ms, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"MOUSE_BTN OK  btn={args.btn} action={args.action} state={last.get('state')}")
        return _exit_for_resp(last)
    print(f"MOUSE_BTN sent (fire-and-forget)  btn={args.btn} action={args.action}")
    return 0


def cmd_key_combo(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "key_tap")
    last = None
    for _ in range(max(1, args.repeat)):
        last = bridge.key_combo(args.keys, args.hold_ms, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"KEY_COMBO OK  keys={last.get('keys')} hold_ms={last.get('hold_ms')} "
                          f"state={last.get('state')}  repeat={args.repeat}")
        return _exit_for_resp(last)
    print(f"KEY_COMBO sent (fire-and-forget)  keys={args.keys} hold_ms={args.hold_ms}")
    return 0


def cmd_key_down(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "key_down")
    last = bridge.key_down(args.keys, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"KEY_DOWN OK  keys={last.get('keys')} state={last.get('state')}")
        if bridge.danger and not args.no_release_on_exit:
            _warn(args, "键处于按下状态；本进程退出时会自动 release_all（§6.4）。"
                        "要跨进程保持按住，请加 --no-release-on-exit")
        return _exit_for_resp(last)
    print(f"KEY_DOWN sent (fire-and-forget)  keys={args.keys}")
    return 0


def cmd_key_up(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "key_up")
    last = bridge.key_up(args.keys, wait=wait)
    if wait and last is not None:
        _emit(args, last, f"KEY_UP OK  keys={last.get('keys')} state={last.get('state')}")
        return _exit_for_resp(last)
    print(f"KEY_UP sent (fire-and-forget)  keys={args.keys}")
    return 0


def cmd_release_all(args, bridge: AgentBridge) -> int:
    resp = bridge.release_all(wait=_wait_for(args, "release_all"))
    if resp is None:
        print("RELEASE_ALL sent (fire-and-forget)")
        return 0
    _emit(args, resp, f"RELEASE_ALL OK  state={resp.get('state')}")
    return _exit_for_resp(resp)


def cmd_text(args, bridge: AgentBridge) -> int:
    wait = _wait_for(args, "text")
    try:
        responses = bridge.text(args.value, args.cps, wait=wait)
    except UnsupportedCharError as exc:
        # §9.2 detail = 首个非法字符的 0-based 索引
        print(f"TEXT FAILED  err=unsupported_char detail={exc.index}  "
              f"(字符 {exc.ch!r} 不在 ASCII 0x20-0x7E)", file=sys.stderr)
        return 4
    if not wait:
        print(f"TEXT sent (fire-and-forget)  {len(responses)} 帧 / frames, chars={len(args.value)}")
        return 0
    last = responses[-1] if responses else {}
    frames = len(responses)
    resp = last if isinstance(last, dict) else {}
    _emit(args, resp, f"TEXT OK  chars={resp.get('chars')} shift_chars={resp.get('shift_chars')} "
                      f"frames={frames}  (分块 §9.1, 不切断字符)")
    return _exit_for_resp(resp)


def cmd_events(args, bridge: AgentBridge) -> int:
    end = time.monotonic() + max(0.1, args.seconds)
    total = 0
    while time.monotonic() < end:
        got = bridge.poll_events(timeout=min(0.25, max(0.01, end - time.monotonic())))
        total += len(got)
    print(f"EVENTS done  收到 {total} 个异步事件 / received {total} async events "
          f"in {args.seconds:.1f}s")
    return 0


def cmd_e2e(args, bridge: AgentBridge) -> int:
    import eyes
    import verify

    outdir = args.outdir or "."
    os.makedirs(outdir, exist_ok=True)
    before_p = os.path.join(outdir, "e2e_before.png")
    after_p = os.path.join(outdir, "e2e_after.png")
    wait = _wait_for(args, "mouse_move")

    # [1/5] 先探活，便于判断「屏幕没变化」到底是 HID 没生效还是别的原因
    host_mounted = None
    try:
        pong = bridge.ping(wait=True)
        host_mounted = pong.get("host_mounted")
        print(f"[1/5] ping OK  fw={pong.get('fw')}  proto={pong.get('proto')}  "
              f"host_mounted={host_mounted}  caps={','.join(pong.get('caps') or [])}")
    except (ProtocolError, ProtocolTimeout, LocalValidationError) as exc:
        print(f"[1/5] ping 失败 / ping failed: {exc}")

    before = eyes.capture(before_p, all_screens=not args.primary)
    print(f"[2/5] 截屏 before / shot before: {before.path}  {before.width}x{before.height}  "
          f"{before.size} B")

    hid_error = None
    resp = None
    try:
        resp = bridge.mouse_move(args.dx, args.dy, wait=wait)
    except (ProtocolError, LocalValidationError) as exc:
        hid_error = exc
    if hid_error is not None:
        print(f"[3/5] HID 指令未生效 / HID command failed: {hid_error}")
    else:
        print(f"[3/5] mouse_move dx={args.dx} dy={args.dy} -> "
              f"{'sent (no-wait)' if resp is None else resp}")

    time.sleep(max(0.0, args.settle))
    after = eyes.capture(after_p, all_screens=not args.primary)
    print(f"[4/5] 截屏 after / shot after:  {after.path}  {after.width}x{after.height}  "
          f"{after.size} B")

    res = verify.diff_images(before.path, after.path, region=args.region, min_pixels=args.min_pixels)
    print("[5/5] 差分 / diff:")
    for line in verify.format_report(res, before.path, after.path).splitlines():
        print("      " + line)

    mock_mode = args.transport == "mock"
    if mock_mode:
        print("结论/conclusion: mock 传输只做协议回环、不驱动真实 HID；因此差分检测到的任何变化都来自"
              "桌面自身（光标/终端刷新），不能当作 HID 生效的证据。\n"
              "      本结果证明「截屏 -> 协议编码发送 -> mock 回应 -> 再截屏 -> 差分」链路通畅"
              " / mock loopback: link OK, screen changes are desktop-local.")
        return 0
    if hid_error is not None:
        print("结论/conclusion: HID 指令未能下发（见 [3/5]），屏幕变化不可归因于本工具 / HID failed")
        return 1
    if host_mounted is False:
        print("结论/conclusion: [WARN] 固件报告 host_mounted=false —— 宿主还没枚举 HID，"
              "先查 USB 线/端口 / host has not enumerated the HID")
        return 1
    if res.has_change:
        print("结论/conclusion: [OK] 端到端可用 —— HID 指令已生效（检测到屏幕变化）"
              " / E2E OK: change detected")
        return 0
    print("结论/conclusion: [WARN] 未检测到屏幕变化 / no screen change detected.\n"
          "      可能原因: (1) HID 未被宿主枚举 host_mounted=false（看 [1/5]）\n"
          "                (2) 目标窗口不响应相对移动 (3) 移动量太小或屏幕本来就静止\n"
          "                (4) 差分区域/阈值设置问题（--region/--min-pixels）")
    return 1


def _warn_if_mounted(args, resp: dict) -> None:
    if isinstance(resp, dict) and resp.get("ok") is False and resp.get("err") == "not_mounted":
        _warn(args, "HID 未被宿主枚举（not_mounted）：检查 USB 线/端口，先跑 ping 看 host_mounted")


HANDLERS = {
    "ping": cmd_ping,
    "status": cmd_status,
    "keepalive": cmd_keepalive,
    "set": cmd_set,
    "mouse-move": cmd_mouse_move,
    "mouse-scroll": cmd_mouse_scroll,
    "mouse-click": cmd_mouse_click,
    "mouse-btn": cmd_mouse_btn,
    "key-combo": cmd_key_combo,
    "key-down": cmd_key_down,
    "key": cmd_key_down,
    "key-hold": cmd_key_down,
    "key-up": cmd_key_up,
    "release-all": cmd_release_all,
    "text": cmd_text,
    "events": cmd_events,
    "e2e-demo": cmd_e2e,
}


# --------------------------------------------------------------------------
# 不需要传输的命令
# --------------------------------------------------------------------------
def run_list_ports(args) -> int:
    import transport_uart
    ports = transport_uart.list_ports()
    if getattr(args, "json", False):
        print(json.dumps({"ports": ports, "count": len(ports)}, ensure_ascii=False, indent=2))
    else:
        print(transport_uart.format_port_table(ports))
        print(f"\n共 {len(ports)} 个串口 / {len(ports)} port(s).  协议 §1: 115200 8N1, CH340C。")
    return 0


def run_list_keys(args) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"modifiers": keymap.MODIFIER_BITS, "modifier_aliases": keymap.MODIFIER_ALIASES,
                          "keys": keymap.KEYS, "symbol_aliases": keymap.SYMBOL_ALIASES,
                          "shift_chars": keymap.SHIFT_CHARS}, ensure_ascii=False, indent=2))
    else:
        print(keymap.format_key_table())
    return 0


def run_screenshot(args) -> int:
    import eyes
    shot, report = eyes.capture_report(args.out, all_screens=not args.primary, region=args.region)
    if getattr(args, "json", False):
        print(json.dumps({"width": shot.width, "height": shot.height, "path": shot.path,
                          "size": shot.size, **report}, ensure_ascii=False, indent=2))
    else:
        print(f"SCREENSHOT OK  {shot.path}")
        print(f"  尺寸/size : {shot.width} x {shot.height}")
        print(f"  文件/bytes: {shot.size} B ({eyes.human_size(shot.size)})")
        print(f"  DPI 模式  : {report['dpi_mode']}  (grabbed {report['grabbed_size'][0]}x{report['grabbed_size'][1]})")
        match = "primary" if report["matches_primary"] else ("virtual" if report["matches_virtual"] else "neither")
        print(f"  屏幕校验  : matches {match}")
    if args.min_bytes and shot.size < args.min_bytes:
        print(f"[SCREENSHOT-ERROR] PNG 只有 {shot.size} B < --min-bytes {args.min_bytes}",
              file=sys.stderr)
        return 1
    return 0


def run_verify(args) -> int:
    import verify
    try:
        res = verify.diff_images(args.a, args.b, region=args.region, threshold=args.threshold,
                                 min_ratio=args.min_ratio, min_pixels=args.min_pixels)
    except verify.VerifyError as exc:
        print(f"[VERIFY-ERROR] {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(verify.diff_summary(res, args.a, args.b), ensure_ascii=False, indent=2))
    else:
        print(verify.format_report(res, args.a, args.b))
    if args.expect == "change" and not res.has_change:
        return 1
    if args.expect == "nochange" and res.has_change:
        return 1
    return 0


def run_selftest(args) -> int:
    import selftest
    # argv=[]：不要把 agent_bridge 的命令行参数喂给 selftest 的 argparse
    return selftest.run(argv=[], verbose=getattr(args, "verbose", False),
                        quiet=getattr(args, "quiet", False))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    setup_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    install_signal_handlers(verbose=getattr(args, "verbose", False))

    try:
        if getattr(args, "list_ports", False) or args.command == "list-ports":
            return run_list_ports(args)
        if getattr(args, "list_keys", False) or args.command == "list-keys":
            return run_list_keys(args)
        if args.command == "selftest":
            return run_selftest(args)
        if args.command == "screenshot":
            return run_screenshot(args)
        if args.command == "verify":
            return run_verify(args)
        if args.command is None:
            parser.print_help()
            return 0
        return run_with_transport(args)

    except ProtocolTimeout as exc:
        print(f"[TIMEOUT] {exc}", file=sys.stderr)
        return 5
    except ProtocolError as exc:
        print(f"[DEVICE-ERROR] {exc}", file=sys.stderr)
        return 4
    except (BadKeyError, BadArgError, UnsupportedCharError) as exc:
        # 本地校验失败：标出对应的协议错误码（§3.4），方便与固件报错对齐
        code = getattr(exc, "err", None)
        print(f"[BAD-ARG] err={code} {exc}" if code else f"[BAD-ARG] {exc}", file=sys.stderr)
        return 4
    except LocalValidationError as exc:
        code = getattr(exc, "err", None)
        print(f"[BAD-ARG] err={code} {exc}" if code else f"[BAD-ARG] {exc}", file=sys.stderr)
        return 4
    except TransportNotImplemented as exc:
        print(f"[TCP-NOT-IMPLEMENTED] {exc}", file=sys.stderr)
        return 6
    except (PortNotFound, PortDisconnected) as exc:
        print(f"[PORT-ERROR] {exc}", file=sys.stderr)
        return 3
    except TransportError as exc:
        print(f"[TRANSPORT-ERROR] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n[中断/interrupted] 已发送 release_all（§6.4）/ release sent", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 3


if __name__ == "__main__":
    raise SystemExit(main())