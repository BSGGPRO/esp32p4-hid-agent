# -*- coding: utf-8 -*-
"""pc/mock_p4.py — 内置 mock P4（无硬件协议对端）。

它按 docs/protocol.md v1.0 逐条实现 **固件侧** 语义，供 selftest.py 与
``agent_bridge.py --transport mock`` 使用：PC 侧代码路径与接真板时**完全一致**，
只是把 UART 换成内存回环。

实现的固件侧规则:
    §1  只处理 ``{...}`` 行；非法行静默丢弃并计入 rx_bad
    §2  带 id -> 必回同 id 响应；不带 id -> fire-and-forget 不回
        响应/事件都带 ``t``（开机以来 ms）；未知字段忽略
    §3  全部命令 + 错误码 bad_json / unknown_cmd / bad_arg / bad_key /
        unsupported_char / not_mounted / rate_limited
    §3.5 事件 boot / hid_mounted / hid_unmounted / safety_release
    §6  安全释放：超时自动 release_all 并发 safety_release 事件
    §4  键名表来自 keymap.py（与 PC 侧同一份表）

便于测试的可注入点（默认全关闭，不影响真机路径）::

    device.clock                 注入时钟，做到安全超时确定性测试
    MockP4Transport(lifo=True)   响应 LIFO 出队，测 PC 侧 id 关联/乱序缓存
    MockP4Transport(chunk_outgoing=7)  字节分片投递，测 PC 侧跨读残行
    MockP4Transport(drop_responses=True)  只吃不回，测 PC 侧超时
    device.force_err = ("rate_limited", "boom")  强制下一次回错误码
"""

from __future__ import annotations

import json
import time
from collections import deque

import keymap
from transport_base import MAX_LINE_BYTES, LineBuffer, Transport, TransportError

#: §3.1 caps 固定值
CAPS = ["hid_keyboard", "hid_mouse", "uart"]

#: §9.3 not_mounted 边界：未挂载（tud_mounted()==false）时必须回 not_mounted
#: 且不得修改本地状态的命令。release_all / ping / status / keepalive / set 不在其中。
OUTPUT_COMMANDS = {
    "key", "key_down", "key_up", "key_tap",
    "text", "text_tap",
    "mouse_move", "mouse_btn", "mouse_scroll", "mouse_click",
}

#: §3 命令表（唯一白名单；不在此表 -> err unknown_cmd）
COMMAND_TABLE = {
    "ping": "_cmd_ping",
    "status": "_cmd_status",
    "keepalive": "_cmd_keepalive",
    "set": "_cmd_set",
    "key": "_cmd_key",
    "key_down": "_cmd_key_down",
    "key_up": "_cmd_key_up",
    "key_tap": "_cmd_key_tap",
    "release_all": "_cmd_release_all",
    "text": "_cmd_text",
    "text_tap": "_cmd_text_tap",
    "mouse_move": "_cmd_mouse_move",
    "mouse_btn": "_cmd_mouse_btn",
    "mouse_scroll": "_cmd_mouse_scroll",
    "mouse_click": "_cmd_mouse_click",
}

#: §9.1 固件行缓冲必须 ≥640 字节
FIRMWARE_LINE_BUFFER = 640

#: mock 层.hpp：GARBAGE_PREFIX 模拟上电 ROM 乱码（§1 要求 PC 静默丢弃）
GARBAGE_PREFIX = [
    b"ESP-ROM:esp32p4-2024\r\n",
    b"Build:Oct 17 2026\r\n",
    b"\x00\xff\xfe tail garbage bytes\r\n",
    b"{bad json}\r\n",          # 以 { 开头以 } 结尾但 json 解析失败 -> 也必须静默丢弃
    b"\x1b[0m rst:0x1 (POWERON)\r\n",
]


class BadArg(Exception):
    """内部：参数非法，携带 detail。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _floor_div_ceil(a: int, b: int) -> int:
    return -(-a // b)


class MockP4Device:
    """按 protocol.md §3 实现的固件侧状态机。"""

    def __init__(self, *, fw: str = "P4-HID-1.0", proto: str = "1.0",
                 host_mounted: bool = True, safety_timeout_ms: int = 1500,
                 clock=None, rate_limit_per_sec: int = 200):
        self.fw = fw
        self.proto = proto
        self.clock = clock or time.monotonic
        self.start_time = self.clock()
        self.rate_limit_per_sec = int(rate_limit_per_sec)

        # HID 状态
        self.host_mounted = bool(host_mounted)
        self.modifier = 0
        self.keys: list[int] = []          # 6 键槽位（usage id）
        self.buttons = 0                   # bit0 左 / bit1 右 / bit2 中

        # 统计（§3.1 status）
        self.rx_ok = 0
        self.rx_bad = 0
        self.rx_rate_limited = 0
        self.rx_ignored_no_id = 0

        # 安全（§6.1）
        self.safety_timeout_ms = int(safety_timeout_ms)
        self.last_msg_time = self.clock()
        self.safety_released_count = 0

        # 事件队列（供 poll 取）
        self.pending_events: deque[dict] = deque()
        # 行为日志（测试断言用）
        self.log: list[dict] = []
        # 计数器
        self.counters = {
            "commands": 0, "replies": 0, "events": 0,
            "moves": 0, "move_reports": 0, "scroll": 0, "scroll_reports": 0,
            "clicks": 0, "taps": 0, "texts": 0, "chars": 0,
            "releases": 0, "key_downs": 0, "key_ups": 0,
            "forced_errors": 0,
        }
        self.move_totals = [0, 0]
        self.scroll_total = 0
        self.text_received = ""
        # 测试注入
        self.force_err: tuple[str, str] | None = None
        self.emit_extra_response_before_id: int | None = None  # 先塞一条无关 id 响应

        self.pending_events.append(self._event("boot", fw=self.fw, proto=self.proto))
        if self.host_mounted:
            self.pending_events.append(self._event("hid_mounted"))

    # -- 时间 / 事件 --------------------------------------------------------
    def uptime_ms(self) -> int:
        return int((self.clock() - self.start_time) * 1000)

    def _event(self, evt: str, **fields) -> dict:
        obj = {"evt": evt, "t": self.uptime_ms()}
        obj.update(fields)
        self.counters["events"] += 1
        return obj

    def emit_event(self, evt: str, **fields) -> dict:
        e = self._event(evt, **fields)
        self.pending_events.append(e)
        return e

    def set_mounted(self, mounted: bool) -> None:
        """模拟 tud_mount_cb / tud_umount_cb（§3.5、§6.2）。"""
        mounted = bool(mounted)
        if mounted == self.host_mounted:
            return
        self.host_mounted = mounted
        if mounted:
            self.emit_event("hid_mounted")
        else:
            # §6.2 断开即释放
            self._clear_all()
            self.emit_event("hid_unmounted")

    def poll_events(self) -> list[dict]:
        """返回当前到期的事件（含安全超时触发的 safety_release）。"""
        self._check_safety()
        out = []
        while self.pending_events:
            out.append(self.pending_events.popleft())
        return out

    # -- 状态 ---------------------------------------------------------------
    @property
    def pressed(self) -> bool:
        return bool(self.modifier) or bool(self.keys) or bool(self.buttons)

    def _clear_all(self) -> None:
        self.modifier = 0
        self.keys = []
        self.buttons = 0

    def snapshot(self) -> dict:
        return {
            "host_mounted": self.host_mounted,
            "modifier": self.modifier,
            "keys": list(self.keys),
            "buttons": self.buttons,
            "pressed": self.pressed,
        }

    def kbd_keys_count(self) -> int:
        """status.kbd_keys 约定：当前按下的键总数（修饰键按位计数 + 普通键槽位）。"""
        return bin(self.modifier).count("1") + len(self.keys)

    def summary(self) -> dict:
        return {
            "fw": self.fw, "proto": self.proto, "host_mounted": self.host_mounted,
            "rx_ok": self.rx_ok, "rx_bad": self.rx_bad,
            "rx_rate_limited": self.rx_rate_limited,
            "safety_timeout_ms": self.safety_timeout_ms,
            "safety_released": self.safety_released_count,
            "state": self.snapshot(),
            "move_totals": list(self.move_totals), "scroll_total": self.scroll_total,
            "text_chars": self.counters["chars"], "clicks": self.counters["clicks"],
            "counters": dict(self.counters),
        }

    # -- 安全释放（§6.1） ----------------------------------------------------
    def _check_safety(self) -> None:
        if self.safety_timeout_ms <= 0 or not self.pressed:
            return
        elapsed_ms = (self.clock() - self.last_msg_time) * 1000.0
        if elapsed_ms >= self.safety_timeout_ms:
            detail_parts = []
            if self.modifier:
                detail_parts.append(f"modifier=0x{self.modifier:02X}")
            if self.keys:
                detail_parts.append(f"keys={self.keys}")
            if self.buttons:
                detail_parts.append(f"buttons=0x{self.buttons:02X}")
            self._clear_all()
            self.safety_released_count += 1
            self.counters["releases"] += 1
            self.pending_events.append(self._event(
                "safety_release",
                detail="safety timeout %dms: released %s" % (self.safety_timeout_ms, ",".join(detail_parts)),
            ))

    # -- 行 / 报文入口 -------------------------------------------------------
    def handle_line(self, line: str) -> list[dict]:
        """处理一行固件输入，返回要发出的响应列表（可能为空）。"""
        self._check_safety()
        text = (line or "").strip()
        if not (text.startswith("{") and text.endswith("}")):
            self.rx_bad += 1
            return []
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            self.rx_bad += 1
            return []
        if not isinstance(obj, dict) or "cmd" not in obj:
            self.rx_bad += 1
            mid = obj.get("id") if isinstance(obj, dict) else None
            if _is_int(mid):
                return [self._reply(mid, ok=False, err="bad_json")]
            return []

        mid = obj.get("id")
        has_id = _is_int(mid)
        if mid is not None and not has_id:
            # id 类型非法：无法关联，回一条不带 id 的错误（PC 当孤儿响应忽略）
            return [{"ok": False, "err": "bad_arg", "detail": "id must be int",
                     "t": self.uptime_ms()}]

        self.rx_ok += 1
        self.counters["commands"] += 1
        self.last_msg_time = self.clock()          # §6.1 收到任何 PC 报文都刷新
        self._check_safety()

        # 限流（§3.4：>200 条/秒）
        if self._rate_limited():
            self.rx_rate_limited += 1
            if has_id:
                return [self._reply(mid, ok=False, err="rate_limited",
                                    detail=f">{self.rate_limit_per_sec} msg/s")]
            self.rx_ignored_no_id += 1
            return []

        if self.force_err is not None:
            err, detail = self.force_err
            self.force_err = None
            self.counters["forced_errors"] += 1
            if has_id:
                return [self._reply(mid, ok=False, err=err, detail=detail)]
            return []

        cmd = obj.get("cmd")
        if not isinstance(cmd, str):
            if has_id:
                return [self._reply(mid, ok=False, err="bad_json", detail="cmd must be string")]
            return []

        try:
            result = self._dispatch(cmd, obj)
        except BadArg as exc:
            self.log.append({"cmd": cmd, "result": "bad_arg", "detail": exc.detail})
            if has_id:
                return [self._reply(mid, ok=False, err="bad_arg", detail=exc.detail)]
            return []
        except keymap.BadArgError as exc:
            # keymap 侧的 bad_arg（例如 keys 里非修饰键 >6 -> 键槽溢出）
            self.log.append({"cmd": cmd, "result": "bad_arg", "detail": str(exc)})
            if has_id:
                return [self._reply(mid, ok=False, err="bad_arg", detail=str(exc))]
            return []
        except keymap.BadKeyError as exc:
            self.log.append({"cmd": cmd, "result": "bad_key", "detail": exc.name})
            if has_id:
                return [self._reply(mid, ok=False, err="bad_key", detail=exc.name)]
            return []
        except keymap.UnsupportedCharError as exc:
            # §9.2 detail = 首个非法字符的 0-based 索引（整数）
            self.log.append({"cmd": cmd, "result": "unsupported_char", "detail": exc.index})
            if has_id:
                return [self._reply(mid, ok=False, err="unsupported_char", detail=exc.index)]
            return []
        except _UnknownCmd as exc:
            self.log.append({"cmd": cmd, "result": "unknown_cmd"})
            if has_id:
                return [self._reply(mid, ok=False, err="unknown_cmd", detail=exc.cmd)]
            return []
        except _NotMounted as exc:
            # §9.3：不得修改本地按键状态（检查发生在任何状态变更之前）
            self.log.append({"cmd": cmd, "result": "not_mounted"})
            if has_id:
                return [self._reply(mid, ok=False, err="not_mounted")]
            return []

        self.log.append({"cmd": cmd, "result": "ok", "args": result})

        if not has_id:
            self.rx_ignored_no_id += 1
            return []                              # §2 fire-and-forget：不回响应
        reply = self._reply(mid, ok=True)
        reply.update(result)

        if self.emit_extra_response_before_id is not None:
            # 测试用：在正确响应之前塞一条无关 id 的响应（PC 必须跳过并缓存）
            stray = {"ok": True, "id": self.emit_extra_response_before_id,
                     "t": self.uptime_ms(), "stray": True}
            self.emit_extra_response_before_id = None
            return [stray, reply]
        return [reply]

    def _rate_limited(self) -> bool:
        if self.rate_limit_per_sec <= 0:
            return False
        now = self.clock()
        if not hasattr(self, "_rate_window"):
            self._rate_window: deque[float] = deque()
        while self._rate_window and now - self._rate_window[0] > 1.0:
            self._rate_window.popleft()
        if len(self._rate_window) >= self.rate_limit_per_sec:
            return True
        self._rate_window.append(now)
        return False

    def _reply(self, mid: int, *, ok: bool, err: str | None = None,
               detail=None) -> dict:
        self.counters["replies"] += 1
        obj = {"ok": ok, "id": mid, "t": self.uptime_ms()}
        if not ok:
            obj["err"] = err or "bad_arg"
            if detail is not None:
                obj["detail"] = detail
        return obj

    # -- 命令分发 -----------------------------------------------------------
    def _dispatch(self, cmd: str, obj: dict) -> dict:
        method = COMMAND_TABLE.get(cmd)
        if method is None:
            raise _UnknownCmd(cmd)                       # §3.4 unknown_cmd
        if cmd in OUTPUT_COMMANDS and not self.host_mounted:
            raise _NotMounted(cmd)                       # §9.3（此前不改任何状态）
        return getattr(self, method)(obj)

    # -- §3.1 ---------------------------------------------------------------
    def _cmd_ping(self, obj: dict) -> dict:
        return {
            "pong": True,
            "fw": self.fw,
            "proto": self.proto,
            "uptime_ms": self.uptime_ms(),
            "host_mounted": self.host_mounted,
            "caps": list(CAPS),
        }

    def _cmd_status(self, obj: dict) -> dict:
        return {
            "host_mounted": self.host_mounted,
            "kbd_keys": self.kbd_keys_count(),
            "mouse_btn": int(self.buttons),
            "rx_ok": self.rx_ok,
            "rx_bad": self.rx_bad,
            "safety_timeout_ms": self.safety_timeout_ms,
        }

    def _cmd_keepalive(self, obj: dict) -> dict:
        return {}   # §3.1：刷新安全超时（last_msg_time 已在入口刷新）

    def _cmd_set(self, obj: dict) -> dict:
        if "key" not in obj:
            raise BadArg("missing key")
        if "value" not in obj:
            raise BadArg("missing value")
        k = obj["key"]
        if not isinstance(k, str):
            raise BadArg("key must be string")
        if not _is_int(obj["value"]):
            raise BadArg("value must be int")
        v = obj["value"]
        if k == "safety_timeout_ms":
            if v < 0 or v > 60000:                       # §9.4 合法范围 0-60000
                raise BadArg("safety_timeout_ms out of range 0..60000")
            self.safety_timeout_ms = v
        else:
            raise BadArg(f"unsupported key {k!r} (v1: safety_timeout_ms)")
        return {"key": k, "value": v}

    # -- §3.2 键盘 ----------------------------------------------------------
    def _keys_arg(self, obj: dict) -> list[str]:
        if "keys" not in obj:
            raise BadArg("missing keys")
        keys = obj["keys"]
        if not isinstance(keys, list):
            raise BadArg("keys must be array")
        checked = keymap.split_keys(keys)   # BadKeyError / BadArgError（>6 槽位）
        return checked[2]

    def _press(self, canon_names: list[str]) -> None:
        for canon in canon_names:
            if canon in keymap.MODIFIER_BITS:
                self.modifier |= keymap.MODIFIER_BITS[canon]
            else:
                usage = keymap.KEYS[canon]
                if usage not in self.keys and len(self.keys) < keymap.MAX_KEY_SLOTS:
                    self.keys.append(usage)

    def _release(self, canon_names: list[str]) -> None:
        for canon in canon_names:
            if canon in keymap.MODIFIER_BITS:
                self.modifier &= ~keymap.MODIFIER_BITS[canon] & 0xFF
            else:
                usage = keymap.KEYS[canon]
                if usage in self.keys:
                    self.keys.remove(usage)

    def _cmd_key(self, obj: dict) -> dict:
        names = self._keys_arg(obj)
        self._press(names)
        self.counters["key_downs"] += len(names)
        return {"keys": names, "state": self.snapshot()}

    def _cmd_key_down(self, obj: dict) -> dict:
        return self._cmd_key(obj)

    def _cmd_key_up(self, obj: dict) -> dict:
        names = self._keys_arg(obj)
        self._release(names)
        self.counters["key_ups"] += len(names)
        return {"keys": names, "state": self.snapshot()}

    def _cmd_key_tap(self, obj: dict) -> dict:
        names = self._keys_arg(obj)
        hold_ms = obj.get("hold_ms", 30)
        if hold_ms is None:
            hold_ms = 30
        if not _is_int(hold_ms):
            raise BadArg("hold_ms must be int")
        if hold_ms < 0 or hold_ms > 10000:
            raise BadArg("hold_ms out of range 0..10000")
        # §3.2 按下 -> 延时 hold_ms -> 全部抬起；组合键用这个
        self._press(names)
        self._release(names)
        self.counters["taps"] += 1
        return {"keys": names, "hold_ms": hold_ms, "state": self.snapshot()}

    def _cmd_release_all(self, obj: dict) -> dict:
        # §6 安全原语：即便未挂载也必须成功
        was = self.snapshot()
        self._clear_all()
        self.counters["releases"] += 1
        return {"released": {k: was[k] for k in ("modifier", "keys", "buttons")},
                "state": self.snapshot()}

    def _cmd_text(self, obj: dict) -> dict:
        if "text" not in obj:
            raise BadArg("missing text")
        text = obj["text"]
        if not isinstance(text, str):
            raise BadArg("text must be string")
        cps = obj.get("cps", 40)
        if cps is None:
            cps = 40
        if not _is_int(cps):
            raise BadArg("cps must be int")
        if cps <= 0 or cps > 1000:
            raise BadArg("cps out of range 1..1000")
        # §3.2：仅 ASCII 0x20-0x7E，否则 err unsupported_char + detail:<位置>
        # （位置为 0-based 字符索引，与 PC 侧 keymap.validate_text 一致）
        keymap.validate_text(text)
        # mock 原子处理：真实固件在 text 期间收到 release_all 会中止后续字符（§6.5）
        shift_used = 0
        for ch in text:
            key_name, shift = keymap.text_to_keys(ch)
            if shift:
                shift_used += 1
        self.text_received += text
        self.counters["texts"] += 1
        self.counters["chars"] += len(text)
        # 注意：响应里**不回显**整段 text —— 否则响应帧会超过 §1 的 512B 上限
        return {"text_len": len(text), "cps": cps, "chars": len(text),
                "shift_chars": shift_used, "state": self.snapshot()}

    def _cmd_text_tap(self, obj: dict) -> dict:
        return self._cmd_text(obj)

    # -- §3.3 鼠标 ----------------------------------------------------------
    def _cmd_mouse_move(self, obj: dict) -> dict:
        dx, dy = self._pair(obj, "dx", "dy")
        reports = max(1, _floor_div_ceil(max(abs(dx), abs(dy)), 127))
        self.move_totals[0] += dx
        self.move_totals[1] += dy
        self.counters["moves"] += 1
        self.counters["move_reports"] += reports
        return {"dx": dx, "dy": dy, "reports": reports}

    def _cmd_mouse_scroll(self, obj: dict) -> dict:
        dy = obj.get("dy", None)
        if dy is None:
            raise BadArg("missing dy")
        if not _is_int(dy):
            raise BadArg("dy must be int")
        if dy < -32767 or dy > 32767:
            raise BadArg("dy out of range -32767..32767")
        reports = max(1, _floor_div_ceil(abs(dy), 127))
        self.scroll_total += dy
        self.counters["scroll"] += 1
        self.counters["scroll_reports"] += reports
        return {"dy": dy, "reports": reports}

    def _cmd_mouse_btn(self, obj: dict) -> dict:
        if "btn" not in obj:
            raise BadArg("missing btn")
        if "action" not in obj:
            raise BadArg("missing action")
        btn = obj["btn"]
        action = obj["action"]
        if btn not in ("left", "right", "middle"):
            raise BadArg(f"btn must be left|right|middle, got {btn!r}")
        if action not in ("down", "up", "click"):
            raise BadArg(f"action must be down|up|click, got {action!r}")
        click_ms = obj.get("click_ms", 40)
        if click_ms is None:
            click_ms = 40
        if not _is_int(click_ms):
            raise BadArg("click_ms must be int")
        if click_ms < 0 or click_ms > 5000:
            raise BadArg("click_ms out of range 0..5000")
        bit = {"left": 0x01, "right": 0x02, "middle": 0x04}[btn]
        if action == "down":
            self.buttons |= bit
        elif action == "up":
            self.buttons &= ~bit & 0xFF
        else:  # click = down + click_ms + up
            self.buttons |= bit
            self.buttons &= ~bit & 0xFF
            self.counters["clicks"] += 1
        return {"btn": btn, "action": action, "click_ms": click_ms, "state": self.snapshot()}

    def _cmd_mouse_click(self, obj: dict) -> dict:
        btn = obj.get("btn", "left")
        count = obj.get("count", 1)
        interval = obj.get("interval_ms", 80)
        if btn is None:
            btn = "left"
        if count is None:
            count = 1
        if interval is None:
            interval = 80
        if btn not in ("left", "right", "middle"):
            raise BadArg(f"btn must be left|right|middle, got {btn!r}")
        if not _is_int(count):
            raise BadArg("count must be int")
        if count < 1 or count > 1000:
            raise BadArg("count out of range 1..1000")
        if not _is_int(interval):
            raise BadArg("interval_ms must be int")
        if interval < 0 or interval > 10000:
            raise BadArg("interval_ms out of range 0..10000")
        bit = {"left": 0x01, "right": 0x02, "middle": 0x04}[btn]
        for _ in range(count):
            self.buttons |= bit
            self.buttons &= ~bit & 0xFF
        self.counters["clicks"] += count
        return {"btn": btn, "count": count, "interval_ms": interval, "state": self.snapshot()}

    def _pair(self, obj: dict, a: str, b: str) -> tuple[int, int]:
        for name in (a, b):
            if name not in obj:
                raise BadArg(f"missing {name}")
            if not _is_int(obj[name]):
                raise BadArg(f"{name} must be int")
            if obj[name] < -32767 or obj[name] > 32767:
                raise BadArg(f"{name} out of range -32767..32767")
        return obj[a], obj[b]


class _UnknownCmd(Exception):
    def __init__(self, cmd: str):
        super().__init__(cmd)
        self.cmd = cmd


class _NotMounted(Exception):
    def __init__(self, cmd: str):
        super().__init__(cmd)
        self.cmd = cmd


# --------------------------------------------------------------------------
# 回环传输
# --------------------------------------------------------------------------
class MockP4Transport(Transport):
    """内存回环：接口与 UartTransport 一致，但对端是 MockP4Device。"""

    name = "mock"
    implemented = True

    def __init__(self, device: MockP4Device | None = None, timeout: float = 1.0, *,
                 lifo: bool = False, chunk_outgoing: int | None = None,
                 drop_responses: bool = False, response_delay_ms: int = 0,
                 emit_garbage: bool = True, **device_kwargs):
        super().__init__(timeout=timeout)
        self.device = device if device is not None else MockP4Device(**device_kwargs)
        self.lifo = bool(lifo)
        self.chunk_outgoing = chunk_outgoing
        self.drop_responses = bool(drop_responses)
        self.response_delay_ms = int(response_delay_ms)
        self.emit_garbage = bool(emit_garbage)
        self._out: deque[bytes] = deque()          # 待发往 PC 的字节块
        self._delayed: deque[tuple[float, bytes]] = deque()
        # PC -> mock：模拟固件行缓冲（§9.1 固件侧 ≥640B；>512B 的行整行丢弃）
        self._rx = LineBuffer(max_line=MAX_LINE_BYTES, max_buffer=FIRMWARE_LINE_BUFFER)
        self._out_buf = LineBuffer()               # mock -> PC 的重组缓冲
        self.stats.update({"frames_in": 0, "frames_out": 0, "long_lines_in": 0})

    # -- 生命周期 -----------------------------------------------------------
    def open(self) -> None:
        if self._open:
            return
        if self.emit_garbage:
            for g in GARBAGE_PREFIX:
                self._enqueue_bytes(g)
        for evt in self.device.poll_events():
            self._enqueue_obj(evt)
        self._open = True

    def close(self) -> None:
        self._open = False

    # -- 编码 / 排队 --------------------------------------------------------
    @staticmethod
    def encode(obj: dict) -> bytes:
        """与真实设备一致的 UTF-8 JSON 一行一帧（≤512B）。"""
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_LINE_BYTES:
            raise TransportError(f"mock 生成超长帧 {len(data)}B > {MAX_LINE_BYTES}B: {obj}")
        return data

    def _enqueue_bytes(self, data: bytes) -> None:
        if self.chunk_outgoing:
            n = max(1, int(self.chunk_outgoing))
            for i in range(0, len(data), n):
                self._out.append(data[i:i + n])
        else:
            self._out.append(data)

    def _enqueue_obj(self, obj: dict) -> None:
        data = self.encode(obj)
        if self.lifo:
            self._out.appendleft(data)
        else:
            self._out.append(data)
        self.stats["frames_out"] += 1

    # -- PC -> mock ---------------------------------------------------------
    def write(self, data: bytes) -> int:
        if not self._open:
            raise TransportError("mock 传输未打开")
        self.stats["bytes_tx"] += len(data)
        overflow_before = self._rx.overflow
        self._rx.feed(data)
        # §9.1：缓冲满仍未遇到 \n -> 丢弃整行 + rx_bad++（不回任何响应）
        new_overflow = self._rx.overflow - overflow_before
        if new_overflow > 0:
            self.device.rx_bad += new_overflow
            self.stats["long_lines_in"] += new_overflow
        while True:
            line = self._rx.next_line()
            if line is None:
                break
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                # PC 侧永不发超长帧；万一发了，固件必须抗住：整行丢弃、rx_bad++、不回响应
                self.device.rx_bad += 1
                self.stats["long_lines_in"] += 1
                continue
            self.stats["frames_in"] += 1
            responses = self.device.handle_line(line)
            if self.drop_responses:
                continue
            for resp in responses:
                if self.response_delay_ms > 0:
                    self._delayed.append((time.monotonic() + self.response_delay_ms / 1000.0,
                                          self.encode(resp)))
                else:
                    self._enqueue_obj(resp)
        return len(data)

    # -- mock -> PC ---------------------------------------------------------
    def _materialize_delayed(self) -> None:
        if not self._delayed:
            return
        now = time.monotonic()
        ready = [item for item in list(self._delayed) if item[0] <= now]
        for item in ready:
            self._delayed.remove(item)
            self._enqueue_bytes(item[1])

    def readline(self, timeout: float | None = None) -> str | None:
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            line = self._rx_out_line()
            if line is not None:
                return line
            self._materialize_delayed()
            for evt in self.device.poll_events():
                self._enqueue_obj(evt)
            line = self._rx_out_line()
            if line is not None:
                return line
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.0005)

    def _rx_out_line(self) -> str | None:
        while True:
            line = self._out_buf.next_line()
            if line is not None:
                return line
            if not self._out:
                return None
            chunk = self._out.popleft()
            self.stats["bytes_rx"] += len(chunk)
            self._out_buf.feed(chunk)

    def readline_nonblocking(self) -> str | None:
        self._materialize_delayed()
        for evt in self.device.poll_events():
            self._enqueue_obj(evt)
        return self._rx_out_line()

    def describe(self) -> dict:
        d = super().describe()
        d.update({"device": self.device.summary(), "lifo": self.lifo,
                  "drop_responses": self.drop_responses,
                  "chunk_outgoing": self.chunk_outgoing})
        return d


__all__ = ["MockP4Device", "MockP4Transport", "CAPS", "OUTPUT_COMMANDS", "GARBAGE_PREFIX"]