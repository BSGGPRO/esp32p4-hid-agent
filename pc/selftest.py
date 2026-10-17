# -*- coding: utf-8 -*-
"""pc/selftest.py — 内置 mock P4 自测（**不需要真硬件**）。

覆盖三件事（task-2 验收要求）:
    1. 协议编解码：§1 行解析、§2 id 关联/事件/字段、§3 全部命令、§4 键名与 Shift
    2. 错误路径：bad_json / unknown_cmd / bad_arg / bad_key / unsupported_char /
       not_mounted / rate_limited / 超时 / 端口不存在 / 中途拔出 / TCP 未实现
    3. aexit 释放语义：context manager 退出、atexit/信号兜底路径都会发 release_all（§6.4）

顺带做依赖审计：用 AST 扫本目录 *.py 的 import，证明只有标准库 + Pillow（无 pyserial）。

跑法::

    python selftest.py                 # 直接跑
    python agent_bridge.py selftest    # 同样内容
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager

import agent_bridge
import keymap
import transport_tcp
import transport_uart
import verify
from agent_bridge import (
    AgentBridge,
    LocalValidationError,
    ProtocolError,
    ProtocolTimeout,
)
from keymap import BadArgError, BadKeyError, UnsupportedCharError
from mock_p4 import MockP4Device, MockP4Transport
from transport_base import PortDisconnected, TransportError, TransportNotImplemented

PC_DIR = pathlib.Path(__file__).resolve().parent

#: (test_name, protocol_ref, callable)
TESTS: list[tuple[str, str, callable]] = []


def test(name: str, ref: str = ""):
    def deco(fn):
        TESTS.append((name, ref, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
@contextmanager
def mock_bridge(*, release_on_close: bool = False, on_event=None, transport_kwargs=None,
                **dev_kwargs):
    """起一个 mock P4 会话，退出时静默关掉（不测 release 时用）。"""
    dev = MockP4Device(**dev_kwargs)
    tr = MockP4Transport(dev, timeout=0.5, **(transport_kwargs or {}))
    b = AgentBridge(tr, timeout=0.5, release_on_close=release_on_close, on_event=on_event)
    b.open()
    try:
        yield b, dev, tr
    finally:
        b.close(release=False)


def _assert(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


def _expect_err(bridge: AgentBridge, cmd: str, err: str, **fields) -> dict:
    """发一条命令，断言设备回了指定错误码（不抛异常的路径）。"""
    resp = bridge.request(cmd, wait=True, **fields)
    _assert(resp is not None, f"{cmd}: 没有响应")
    _assert(resp.get("ok") is False, f"{cmd}: 期望 ok:false，实际 {resp}")
    _assert(resp.get("err") == err, f"{cmd}: 期望 err={err}，实际 {resp.get('err')} ({resp})")
    return resp


# ==========================================================================
# §1 物理层 / 行解析
# ==========================================================================
@test("line_reader_discards_rom_garbage", "§1")
def t_line_reader_garbage():
    events = []
    with mock_bridge(on_event=events.append, transport_kwargs={"emit_garbage": True}) as (b, dev, tr):
        resp = b.command("ping")
        _assert(resp.get("pong") is True, "ping 应成功")
        _assert(b.stats.garbage_lines >= 3,
                f"上电 ROM 乱码行应被静默丢弃，实际只丢了 {b.stats.garbage_lines} 行")
        _assert(b.stats.bad_json >= 1, "`{bad json}` 行应计入 bad_json 且不退出")
        names = [e.get("evt") for e in events]
        _assert("boot" in names, f"应收到 boot 事件，实际 {names}")
        _assert("hid_mounted" in names, f"应收到 hid_mounted 事件，实际 {names}")


@test("line_reader_survives_split_frames", "§1")
def t_line_reader_split():
    with mock_bridge(transport_kwargs={"chunk_outgoing": 7}) as (b, dev, tr):
        for _ in range(3):
            resp = b.command("ping")
            _assert(resp.get("pong") is True, "跨读分片的帧必须能重组")


@test("line_reader_drops_invalid_json_line", "§1")
def t_line_reader_bad_json_line():
    with mock_bridge() as (b, dev, tr):
        b.command("ping")
        before = b.stats.bad_json
        tr._enqueue_bytes(b'{oops}\n')            # 以 { 开头 } 结尾但解析失败
        resp = b.command("ping")
        _assert(resp.get("pong") is True, "坏 JSON 行不得让客户端退出")
        _assert(b.stats.bad_json == before + 1, "坏 JSON 行应计入 bad_json")


@test("firmware_bad_json_returns_err", "§3.4")
def t_firmware_bad_json():
    with mock_bridge() as (b, dev, tr):
        tr.write(b'{"id":4242,"foo":1}\n')        # 无 cmd 字段
        resp = b.wait_for(4242, cmd="raw")
        _assert(resp.get("ok") is False and resp.get("err") == "bad_json",
                f"无 cmd 字段应回 bad_json，实际 {resp}")
        before = dev.rx_bad
        tr.write(b'not json at all\n')            # 非 {...} 行
        _assert(dev.rx_bad >= before + 1, "固件侧非法行应计入 rx_bad")


@test("firmware_drops_oversized_line", "§9.1")
def t_firmware_oversized_line():
    with mock_bridge() as (b, dev, tr):
        before = dev.rx_bad
        # 绕过客户端直接发一条 >512B 的帧；固件必须整行丢弃、rx_bad++、不回响应
        tr.write(b'{"cmd":"ping","id":777,"pad":"' + b"y" * 600 + b'"}\n')
        _assert(dev.rx_bad >= before + 1, "超长行应被固件丢弃并计入 rx_bad")
        _assert(dev.counters["commands"] == 0, "超长行不得被当成命令执行")
        _assert(777 not in b._pending, "超长行不得产生响应")
        _assert(tr.stats["long_lines_in"] >= 1, "应统计到超长行")


@test("client_refuses_oversized_frame", "§9.1")
def t_client_oversized():
    with mock_bridge() as (b, dev, tr):
        try:
            b._write_obj({"cmd": "text", "text": "x" * 600})
        except LocalValidationError:
            pass
        else:
            raise AssertionError("PC 侧必须拒绝发送 >512B 的帧")


# ==========================================================================
# §2 报文总则
# ==========================================================================
@test("ping_roundtrip_caps_and_t", "§2/§3.1")
def t_ping():
    with mock_bridge() as (b, dev, tr):
        resp = b.command("ping", unknown_field="前向兼容应被忽略")
        _assert(resp.get("ok") is True)
        _assert(resp.get("pong") is True)
        _assert(resp.get("proto") == "1.0", f"proto 应为 1.0，实际 {resp.get('proto')}")
        _assert(resp.get("caps") == ["hid_keyboard", "hid_mouse", "uart"], f"caps={resp.get('caps')}")
        _assert(isinstance(resp.get("uptime_ms"), int), "uptime_ms 应为整数")
        _assert("t" in resp, "§2 要求响应带 t")
        _assert(resp.get("id") == b._id, "§9.4 响应 id 必须与请求数值相等")


@test("status_reports_counters", "§3.1")
def t_status():
    with mock_bridge() as (b, dev, tr):
        b.command("ping")
        b.command("mouse_move", dx=5, dy=5)
        resp = b.command("status")
        _assert(resp.get("host_mounted") is True)
        _assert(resp.get("kbd_keys") == 0)
        _assert(resp.get("mouse_btn") == 0)
        _assert(resp.get("rx_ok", 0) >= 2, f"rx_ok 应 ≥2，实际 {resp.get('rx_ok')}")
        _assert(resp.get("safety_timeout_ms") == 1500, "默认 safety_timeout_ms 应为 1500")
        _assert("t" in resp)


@test("id_correlation_skips_stray_response", "§2/§9.4")
def t_id_stray():
    with mock_bridge() as (b, dev, tr):
        dev.emit_extra_response_before_id = 999
        resp = b.command("ping")
        _assert(resp.get("pong") is True, "必须跳过无关 id 的响应并等到自己的响应")
        _assert(b.stats.orphans >= 1, "无关响应应计入 orphans")
        _assert(999 in b._pending, "无关响应应被缓存起来而不是丢弃")


@test("out_of_order_responses_are_matched", "§2")
def t_out_of_order():
    dev = MockP4Device()
    tr = MockP4Transport(dev, timeout=0.5, lifo=True, emit_garbage=False)
    b = AgentBridge(tr, timeout=0.5, release_on_close=False)
    b.open()
    try:
        id1 = b.send_nowait("ping")
        id2 = b.send_nowait("status")
        r2 = b.wait_for(id2, cmd="status")
        r1 = b.wait_for(id1, cmd="ping")
        _assert(r2.get("id") == id2 and "kbd_keys" in r2, f"乱序响应关联错误: {r2}")
        _assert(r1.get("id") == id1 and r1.get("pong") is True, f"乱序响应关联错误: {r1}")
    finally:
        b.close(release=False)


@test("timeout_raises_cleanly_not_hang", "§2/§7")
def t_timeout():
    dev = MockP4Device()
    tr = MockP4Transport(dev, timeout=0.3, drop_responses=True, emit_garbage=False)
    b = AgentBridge(tr, timeout=0.3, release_on_close=False)
    b.open()
    try:
        t0 = time.monotonic()
        try:
            b.command("ping")
        except ProtocolTimeout as exc:
            _assert("id=" in str(exc) and exc.msg_id > 0, "超时异常应带 id 信息")
        else:
            raise AssertionError("设备不回响应时必须抛 ProtocolTimeout")
        elapsed = time.monotonic() - t0
        _assert(elapsed < 1.2, f"超时不应长时间挂住，实际 {elapsed:.2f}s")
        # 恢复后仍可继续用
        tr.drop_responses = False
        _assert(b.command("ping").get("pong") is True, "超时后会话仍应可用")
    finally:
        b.close(release=False)


# ==========================================================================
# §3 命令表 + §3.4 错误码
# ==========================================================================
@test("unknown_cmd_error", "§3.4")
def t_unknown_cmd():
    with mock_bridge() as (b, dev, tr):
        resp = _expect_err(b, "frobnicate", "unknown_cmd")
        try:
            b.command("frobnicate")
        except ProtocolError as exc:
            _assert(exc.err == "unknown_cmd")
        else:
            raise AssertionError("command() 遇到 ok:false 应抛 ProtocolError")


@test("bad_key_reports_first_illegal_key", "§3.2/§3.4")
def t_bad_key():
    with mock_bridge() as (b, dev, tr):
        resp = _expect_err(b, "key_down", "bad_key", keys=["a", "nosuchkey", "b"])
        _assert(resp.get("detail") == "nosuchkey", f"detail 应为第一个非法键名，实际 {resp.get('detail')}")
        _assert(dev.modifier == 0 and dev.keys == [], "bad_key 不得修改按键状态")
        # PC 侧本地预校验也要拦（不必等固件）
        try:
            keymap.validate_keys(["a", "nosuchkey"])
        except BadKeyError as exc:
            _assert(exc.name == "nosuchkey")
        else:
            raise AssertionError("keymap.validate_keys 应抛 BadKeyError")


@test("bad_arg_type_range_missing", "§3.4")
def t_bad_arg():
    with mock_bridge() as (b, dev, tr):
        _expect_err(b, "mouse_move", "bad_arg", dx="x", dy=0)          # 类型错
        _expect_err(b, "mouse_move", "bad_arg", dx=40000, dy=0)        # 越界
        _expect_err(b, "mouse_move", "bad_arg", dx=1)                  # 缺字段
        _expect_err(b, "key_down", "bad_arg", keys="a")                # 数组类型错
        _expect_err(b, "mouse_btn", "bad_arg", btn="x", action="click")
        _expect_err(b, "mouse_btn", "bad_arg", btn="left", action="hold")
        _expect_err(b, "mouse_click", "bad_arg", btn="left", count=0, interval_ms=80)
        _expect_err(b, "text", "bad_arg", text=123)
        _expect_err(b, "text", "bad_arg", text="ok", cps=0)


@test("key_slot_overflow_is_bad_arg", "§3.2")
def t_slot_overflow():
    with mock_bridge() as (b, dev, tr):
        seven = ["a", "b", "c", "d", "e", "f", "g"]
        _expect_err(b, "key_down", "bad_arg", keys=seven)
        # 6 个正好可以；修饰键不占槽位
        resp = b.command("key_down", keys=["a", "b", "c", "d", "e", "f", "leftshift"])
        _assert(len(dev.keys) == 6 and dev.modifier == 0x02, f"状态异常: {dev.snapshot()}")
        _assert(len(resp.get("keys")) == 7, "修饰键 + 6 普通键 = 7 个键名都合法")


@test("unsupported_char_detail_is_0based", "§9.2")
def t_unsupported_char():
    with mock_bridge() as (b, dev, tr):
        resp = _expect_err(b, "text", "unsupported_char", text="ab\u2603c")
        _assert(resp.get("detail") == 2, f"detail 应为 0-based 索引 2，实际 {resp.get('detail')!r}")
        # PC 侧本地校验也必须 0-based
        try:
            keymap.validate_text("ab\u2603c")
        except UnsupportedCharError as exc:
            _assert(exc.index == 2, f"本地校验索引应为 2，实际 {exc.index}")
        else:
            raise AssertionError("validate_text 应拒绝非 ASCII 字符")


@test("text_shift_rules_us_layout", "§4")
def t_text_shift():
    with mock_bridge() as (b, dev, tr):
        value = "Hello, World! 123"
        resp = b.command("text", text=value, cps=40)
        _assert(resp.get("chars") == len(value), f"chars={resp.get('chars')} != {len(value)}")
        _assert(resp.get("shift_chars") == 3, f"H/W/! 需要 Shift，实际 {resp.get('shift_chars')}")
        _assert(dev.text_received == value, "mock 应收到完整文本")
        # 全部 95 个 ASCII 可打印字符都可输入
        allprint = "".join(chr(c) for c in range(0x20, 0x7F))
        keymap.validate_text(allprint)
        pairs = [keymap.text_to_keys(c) for c in allprint]
        _assert(all(p is not None for p in pairs), "ASCII 0x20-0x7E 必须全部可映射")
        _assert(keymap.text_to_keys("A") == ("a", True), "A -> shift + a")
        _assert(keymap.text_to_keys("~") == ("grave", True), "~ -> shift + grave")
        _assert(keymap.text_to_keys("/") == ("slash", False), "/ -> slash")


@test("text_chunking_respects_512_and_chars", "§9.1")
def t_text_chunking():
    with mock_bridge(rate_limit_per_sec=0) as (b, dev, tr):
        value = "".join(chr(0x20 + (i % 95)) for i in range(1200))
        responses = b.text(value, cps=40)
        _assert(len(responses) >= 2, f"1200 字符应分多帧，实际 {len(responses)} 帧")
        _assert(b.stats.max_frame_len <= 512,
                f"任何帧都必须 ≤512B，实际最长 {b.stats.max_frame_len}B")
        _assert(dev.text_received == value, "分块后 mock 收到的文本必须与原文完全一致（不切断字符）")
        _assert(sum(r.get("chars", 0) for r in responses) == 1200)
        # 预演分块也不得越界（含 10 位 id 的最坏情况）
        for chunk in b.split_text(value, cps=40, with_id=True):
            obj = {"cmd": "text", "id": 9999999999, "text": chunk, "cps": 40}
            _assert(len(b.encode_obj(obj)) <= 512, f"分块超界: {len(b.encode_obj(obj))}B")


@test("key_down_up_and_state_roundtrip", "§3.2")
def t_key_state():
    with mock_bridge() as (b, dev, tr):
        resp = b.command("key_down", keys=["leftctrl", "a"])
        _assert(dev.modifier == 0x01 and dev.keys == [0x04], f"按下状态异常 {dev.snapshot()}")
        _assert(resp.get("state", {}).get("modifier") == 0x01)
        b.command("key_up", keys=["leftctrl"])
        _assert(dev.modifier == 0x00 and dev.keys == [0x04], f"抬起状态异常 {dev.snapshot()}")
        b.command("key", keys=["b"])                        # `key` 是 key_down 别名
        _assert(0x05 in dev.keys, "key 别名应等价于 key_down")
        b.command("release_all")
        _assert(not dev.pressed and dev.modifier == 0 and dev.keys == [], "release_all 应清空状态")


@test("mouse_move_and_scroll_split_at_127", "§3.3")
def t_mouse_split():
    with mock_bridge() as (b, dev, tr):
        resp = b.command("mouse_move", dx=300, dy=-200)
        _assert(resp.get("reports") == 3, f"300/-200 应拆 3 帧，实际 {resp.get('reports')}")
        _assert(dev.move_totals == [300, -200], f"累计位移异常 {dev.move_totals}")
        resp = b.command("mouse_scroll", dy=130)
        _assert(resp.get("reports") == 2, f"130 应拆 2 帧，实际 {resp.get('reports')}")
        _assert(dev.scroll_total == 130)
        _expect_err(b, "mouse_scroll", "bad_arg", dy=40000)


@test("mouse_btn_and_click", "§3.3")
def t_mouse_click():
    with mock_bridge() as (b, dev, tr):
        b.command("mouse_btn", btn="left", action="down")
        _assert(dev.buttons == 0x01, f"左键按下应为 0x01，实际 {dev.buttons:#x}")
        b.command("mouse_btn", btn="left", action="up")
        _assert(dev.buttons == 0x00)
        b.command("mouse_btn", btn="middle", action="click")
        _assert(dev.buttons == 0x00 and dev.counters["clicks"] == 1)
        resp = b.command("mouse_click", btn="left", count=3, interval_ms=10)
        _assert(resp.get("count") == 3 and dev.counters["clicks"] == 4)
        _assert(dev.buttons == 0x00, "连击结束后不应有按键残留")
        # 缺省值：btn=left, count=1, interval_ms=80
        resp = b.request("mouse_click", wait=True)
        _assert(resp.get("ok") is True and resp.get("count") == 1, f"缺省参数异常 {resp}")


@test("set_safety_timeout_and_range", "§3.1/§9.4")
def t_set_param():
    with mock_bridge() as (b, dev, tr):
        resp = b.command("set", key="safety_timeout_ms", value=5000)
        _assert(resp.get("key") == "safety_timeout_ms" and resp.get("value") == 5000,
                f"set 回显异常 {resp}")
        _assert(dev.safety_timeout_ms == 5000)
        _expect_err(b, "set", "bad_arg", key="safety_timeout_ms", value=99999)   # >60000
        _expect_err(b, "set", "bad_arg", key="safety_timeout_ms", value=-1)
        _expect_err(b, "set", "bad_arg", key="safety_timeout_ms", value="5")     # 非整数
        _expect_err(b, "set", "bad_arg", key="bogus_key", value=1)
        try:
            b.set_value("safety_timeout_ms", 70000)
        except LocalValidationError:
            pass
        else:
            raise AssertionError("PC 侧应本地拦截越界的 safety_timeout_ms")


@test("keepalive_refreshes_and_replies", "§3.1")
def t_keepalive():
    with mock_bridge() as (b, dev, tr):
        resp = b.command("keepalive")
        _assert(resp.get("ok") is True and "t" in resp, f"keepalive 响应异常 {resp}")


@test("not_mounted_boundary", "§9.3")
def t_not_mounted():
    with mock_bridge(host_mounted=False) as (b, dev, tr):
        # 查询类仍正常
        _assert(b.command("ping").get("host_mounted") is False)
        st = b.command("status")
        _assert(st.get("host_mounted") is False, "status 应正常返回并报告 host_mounted=false")
        _assert(b.command("keepalive").get("ok") is True)
        _assert(b.command("set", key="safety_timeout_ms", value=1500).get("ok") is True)
        # 产生 HID 报文的命令 -> not_mounted，且不得改状态
        for cmd, fields in (
            ("key_down", {"keys": ["a"]}),
            ("key_up", {"keys": ["a"]}),
            ("key_tap", {"keys": ["a"]}),
            ("text", {"text": "hi"}),
            ("mouse_move", {"dx": 1, "dy": 1}),
            ("mouse_btn", {"btn": "left", "action": "down"}),
            ("mouse_scroll", {"dy": 1}),
            ("mouse_click", {"btn": "left", "count": 1, "interval_ms": 80}),
        ):
            resp = _expect_err(b, cmd, "not_mounted", **fields)
            _assert(dev.snapshot()["modifier"] == 0 and dev.keys == [] and dev.buttons == 0,
                    f"{cmd} 在 not_mounted 时不得修改状态: {dev.snapshot()}")
        # 安全原语永远成功
        _assert(b.command("release_all").get("ok") is True, "release_all 在未挂载时必须 ok:true")


# ==========================================================================
# §6 安全语义
# ==========================================================================
@test("safety_timeout_auto_releases_and_events", "§6.1")
def t_safety_timeout():
    fake = {"t": 1000.0}
    events = []
    with mock_bridge(safety_timeout_ms=100, clock=lambda: fake["t"], on_event=events.append) as (b, dev, tr):
        b.command("key_down", keys=["a"])
        _assert(dev.pressed, "按下后应为按下状态")
        fake["t"] += 0.2                                   # 超过 100ms 安全超时
        got = b.poll_events(timeout=0.05)
        _assert(not dev.pressed, "安全超时应强制释放全部按键")
        _assert(dev.safety_released_count == 1, f"safety_release 计数异常 {dev.safety_released_count}")
        names = [e.get("evt") for e in got] + [e.get("evt") for e in events]
        _assert("safety_release" in names, f"应收到 safety_release 事件，实际 {names}")
        detail = [e for e in got + events if e.get("evt") == "safety_release"][0].get("detail")
        _assert("keys" in str(detail), f"safety_release.detail 应说明释放了什么，实际 {detail!r}")


@test("safety_timeout_zero_disables", "§3.1")
def t_safety_disabled():
    fake = {"t": 2000.0}
    events = []
    with mock_bridge(safety_timeout_ms=100, clock=lambda: fake["t"], on_event=events.append) as (b, dev, tr):
        b.command("set", key="safety_timeout_ms", value=0)   # 0 = 关闭安全释放
        b.command("key_down", keys=["a"])
        fake["t"] += 5.0
        b.poll_events(timeout=0.05)
        _assert(dev.pressed, "safety_timeout_ms=0 时不应自动释放")
        _assert(dev.safety_released_count == 0)
        b.command("release_all")
        _assert(not dev.pressed)


@test("context_manager_exit_sends_release_all", "§6.4")
def t_aexit_release():
    dev = MockP4Device()
    tr = MockP4Transport(dev, timeout=0.5, emit_garbage=False)
    b = AgentBridge(tr, timeout=0.5)              # release_on_close 默认 True
    with b:
        b.command("key_down", keys=["leftctrl", "a"])
        _assert(dev.pressed, "按下的键应反映在设备状态里")
    _assert(not dev.pressed, "退出 with 块必须发 release_all 把键释放掉")
    _assert(b.stats.releases_sent >= 1, "应记录已发送 release_all")
    _assert(any(e["cmd"] == "release_all" for e in dev.log), "设备日志里应有 release_all")
    _assert(not tr.is_open, "退出后传输应关闭")


@test("atexit_safety_cleanup_path", "§6.4")
def t_atexit_path():
    dev = MockP4Device()
    tr = MockP4Transport(dev, timeout=0.5, emit_garbage=False)
    b = AgentBridge(tr, timeout=0.5)
    b.open()
    b.command("key_down", keys=["a"])
    _assert(dev.pressed)
    agent_bridge.safety_cleanup()                  # atexit / 信号处理器走的就是这条路径
    _assert(not dev.pressed, "safety_cleanup 必须发 release_all")
    _assert(not tr.is_open, "safety_cleanup 之后传输应关闭")
    _assert(b not in agent_bridge._ACTIVE_BRIDGES, "已关闭的会话不应留在活动表里")
    agent_bridge.safety_cleanup()                  # 幂等：再调一次也不许抛


@test("release_on_close_false_keeps_keys_held", "§6.4")
def t_no_release_on_close():
    dev = MockP4Device()
    tr = MockP4Transport(dev, timeout=0.5, emit_garbage=False)
    b = AgentBridge(tr, timeout=0.5, release_on_close=False)
    with b:
        b.command("key_down", keys=["a"])
    _assert(dev.pressed, "--no-release-on-exit 语义：退出时不释放（跨进程按住键）")


@test("disconnect_mid_session_no_uncaught", "§6.4")
def t_disconnect():
    class Flaky(MockP4Transport):
        def __init__(self, *a, fail_after=2, **kw):
            super().__init__(*a, **kw)
            self.writes = 0
            self.fail_after = fail_after

        def write(self, data):
            self.writes += 1
            if self.writes > self.fail_after:
                raise PortDisconnected("模拟 USB 拔出 / simulated unplug")
            return super().write(data)

    dev = MockP4Device()
    tr = Flaky(dev, timeout=0.5, emit_garbage=False, fail_after=2)
    b = AgentBridge(tr, timeout=0.5)
    b.open()
    try:
        b.command("ping")
        b.command("status")
        try:
            b.command("ping")
        except PortDisconnected:
            pass
        else:
            raise AssertionError("中途拔出应抛 PortDisconnected（TransportError 子类）")
    finally:
        b.close()                                  # 端口没了也不许抛（§6.4）
    _assert(not tr.is_open)
    _assert(b.emergency_release() is False, "端口已断时 emergency_release 应返回 False 而不是抛异常")


@test("missing_port_is_clean_transport_error", "§6.4")
def t_missing_port():
    t = transport_uart.UartTransport("COM250", timeout=0.2)
    opened = False
    try:
        t.open()
        opened = True
    except TransportError as exc:
        _assert(isinstance(exc, TransportError), "必须是 TransportError 子类")
        _assert("COM250" in str(exc), f"错误信息应提到端口名，实际 {exc}")
    finally:
        t.close()
    _assert(not opened, "COM250 不该存在；若存在请改用一个不存在的端口名跑这个测试")
    t.close()                                       # 幂等
    b = AgentBridge(t, timeout=0.2)
    b.close()                                       # 未 open 就 close 也不许抛


@test("port_listing_never_crashes", "§1")
def t_list_ports():
    ports = transport_uart.list_ports()
    _assert(isinstance(ports, list), "list_ports 应返回 list")
    for p in ports:
        _assert(p["com"].startswith("COM"), f"端口名异常 {p}")
        _assert("is_bluetooth" in p and "likely_target" in p)
    table = transport_uart.format_port_table(ports)
    _assert(isinstance(table, str) and table.strip() != "", "表格输出不应为空")
    for p in ports:
        _assert(p["com"] in table, f"{p['com']} 应出现在表格里")
    _assert("no serial port found" in transport_uart.format_port_table([]).lower()
            or "未发现任何串口" in transport_uart.format_port_table([]))
    # 真板子用 CH340（§1）：识别逻辑本身要能工作
    fake_com = [{"com": "COM7", "device": r"\Device\VCP0", "bus": "USB", "vidpid": ("1a86", "7523"),
                 "vidpid_text": "1A86:7523", "friendly": "USB-SERIAL CH340 (COM7)",
                 "kind": "uart_bridge", "kind_label": "USB-UART 桥", "model": "CH340",
                 "is_bluetooth": False, "likely_target": True, "advice": "★ 最可能是开发板", "rank": 0}]
    _assert(transport_uart.detect_port(fake_com)["com"] == "COM7", "CH340 应被自动选中")


@test("tcp_transport_reports_unimplemented", "§1")
def t_tcp_stub():
    t = transport_tcp.TcpTransport("127.0.0.1", 8888, timeout=0.2)
    _assert(t.implemented is False, "TCP 后端必须标记为未实现")
    try:
        t.open()
    except TransportNotImplemented as exc:
        _assert("未实现" in str(exc), "错误信息必须明确说未实现")
        _assert("--transport uart" in str(exc), "应给出可用的替代方案")
    else:
        raise AssertionError("TCP stub 绝不能假装连接成功")
    for call in (lambda: t.write(b"x"), lambda: t.readline(0.1)):
        try:
            call()
        except TransportNotImplemented:
            pass
        else:
            raise AssertionError("TCP stub 的 I/O 也必须明确报未实现")
    probe = t.probe()
    _assert(probe["implemented"] is False and probe["reason"], f"probe 异常 {probe}")
    t.close()


@test("no_wait_burst_throughput", "§7")
def t_throughput():
    with mock_bridge(rate_limit_per_sec=0) as (b, dev, tr):
        n = 300
        b.poll_events(timeout=0.02)                    # 先排空上电事件/乱码（它们不算响应）
        _assert(len(tr._out) == 0, "排空后不应还有待发帧")
        t0 = time.perf_counter()
        for _ in range(n):
            b.send_ff("mouse_move", dx=1, dy=0)        # 不带 id 连发
        elapsed = time.perf_counter() - t0
        rate = n / elapsed if elapsed else float("inf")
        _assert(dev.counters["moves"] == n, f"mock 应收到 {n} 条，实际 {dev.counters['moves']}")
        _assert(len(tr._out) == 0, "不带 id 的命令不应产生任何响应")
        _assert(b.stats.max_frame_len <= 512)
        _assert(rate >= 100, f"连发吞吐应 ≥100 条/秒，实际 {rate:.0f} 条/秒")
        frame = len(b.encode_obj({"cmd": "mouse_move", "dx": 1, "dy": 0}))
        uart_max = 11520 / frame                    # 115200 baud, 10 bit/byte
        t_throughput.measured = {"loopback_msg_per_s": rate, "frame_bytes": frame,
                                 "uart_theoretical_msg_per_s": uart_max}
        _assert(uart_max >= 100, f"115200 8N1 下 {frame}B 帧的理论上限应 ≥100 条/秒，实际 {uart_max:.0f}")
        # 控制帧 switch：3 个字符的 key 命令也检查一下
        _assert(b.stats.tx_frames == n, f"应发出 {n} 帧，实际 {b.stats.tx_frames}")


@test("rate_limited_error_path", "§3.4")
def t_rate_limited():
    # (a) 强制错误码：PC 侧必须能识别并提示退避
    with mock_bridge() as (b, dev, tr):
        dev.force_err = ("rate_limited", ">200 msg/s")
        try:
            b.command("ping")
        except ProtocolError as exc:
            _assert(exc.err == "rate_limited")
            _assert("退避" in str(exc) or "back off" in str(exc), "错误信息应提示退避")
        else:
            raise AssertionError("rate_limited 应抛 ProtocolError")
    # (b) 真限流：同一秒内第 201 条起被丢弃
    fake = {"t": 500.0}
    with mock_bridge(rate_limit_per_sec=200, clock=lambda: fake["t"]) as (b, dev, tr):
        for _ in range(250):
            b.send_ff("ping")
        _assert(dev.rx_rate_limited == 50, f"应限流 50 条，实际 {dev.rx_rate_limited}")
        _assert(dev.counters["commands"] == 250)
        fake["t"] += 1.5                            # 滑出 1 秒窗口后恢复
        _assert(b.command("ping").get("pong") is True, "限流窗口过后应恢复")


# ==========================================================================
# verify / screenshot 模块
# ==========================================================================
@test("verify_module_pixel_diff", "交付物")
def t_verify_module():
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        pa = os.path.join(td, "a.png")
        pb = os.path.join(td, "b.png")
        img_a = Image.new("RGB", (100, 100), (0, 0, 0))
        img_b = img_a.copy()
        for x in range(10):
            for y in range(10):
                img_b.putpixel((x, y), (255, 255, 255))
        img_a.save(pa)
        img_b.save(pb)

        res = verify.diff_images(pa, pb)
        _assert(res.changed_pixels == 100, f"应正好 100 个变化像素，实际 {res.changed_pixels}")
        _assert(res.total_pixels == 10000)
        _assert(abs(res.ratio - 0.01) < 1e-9, f"比例应为 0.01，实际 {res.ratio}")
        _assert(res.has_change is True and res.bbox == (0, 0, 10, 10), f"bbox 异常 {res.bbox}")

        same = verify.diff_images(pa, pa)
        _assert(same.changed_pixels == 0 and same.has_change is False, "同一张图应判定无变化")

        region_out = verify.diff_images(pa, pb, region="50,50,20,20")
        _assert(region_out.has_change is False, "变化区域之外应判定无变化")
        region_in = verify.diff_images(pa, pb, region="0,0,10,10")
        _assert(region_in.changed_pixels == 100 and region_in.has_change is True)

        thr = verify.diff_images(pa, pb, threshold=255)
        _assert(thr.has_change is False, "阈值 255 时（差异必须 >255）不应判为变化")
        thr254 = verify.diff_images(pa, pb, threshold=254)
        _assert(thr254.has_change is True, "黑白差 255 > 254，阈值 254 仍应判为变化")

        minr = verify.diff_images(pa, pb, min_ratio=0.5)
        _assert(minr.has_change is False, "min_ratio=0.5 时 1% 的变化不应判为有变化")

        _assert("有变化" in verify.format_report(res), "报告应包含结论")
        _assert(json.dumps(verify.diff_summary(res, pa, pb), ensure_ascii=False), "summary 应可 JSON 序列化")

        # 尺寸不同 -> size_mismatch 且仍给出结论
        pc = os.path.join(td, "c.png")
        Image.new("RGB", (50, 50), (0, 0, 0)).save(pc)
        mism = verify.diff_images(pa, pc)
        _assert(mism.size_mismatch is True and mism.has_change is True, f"尺寸不一致应被标记 {mism}")

        try:
            verify.diff_images(os.path.join(td, "nope.png"), pa)
        except verify.VerifyError:
            pass
        else:
            raise AssertionError("缺文件应抛 VerifyError")


@test("screenshot_module_available", "交付物")
def t_eyes_module():
    import eyes
    _assert(os.path.isfile(os.path.join(str(PC_DIR), "eyes.py")))
    _assert(callable(eyes.capture) and callable(eyes.capture_report))
    _assert(eyes.Shot._fields == ("width", "height", "path", "size"), "Shot 必须可解包成 (w,h,path,size)")
    _assert(eyes.parse_region("0,0,800,600") == (0, 0, 800, 600))
    try:
        eyes.parse_region("0,0,0,600")
    except ValueError:
        pass
    else:
        raise AssertionError("w/h 必须为正")
    _assert(eyes.human_size(51200).endswith("KiB"))


# ==========================================================================
# CLI 端到端 + 依赖审计
# ==========================================================================
@test("cli_exit_codes_and_mock_run", "CLI")
def t_cli():
    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return agent_bridge.main(argv), out.getvalue(), err.getvalue()

    # --help 由 argparse 处理，exit 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            agent_bridge.main(["--help"])
    except SystemExit as exc:
        _assert(exc.code == 0, f"--help 应 exit 0，实际 {exc.code}")
    else:
        raise AssertionError("--help 应触发 SystemExit(0)")

    rc, out, _ = run(["--list-ports"])
    _assert(rc == 0, f"--list-ports 应 exit 0，实际 {rc}")
    _assert(out.strip() != "", "--list-ports 应有输出")

    rc, out, _ = run(["--list-keys"])
    _assert(rc == 0 and "leftctrl" in out, f"--list-keys 异常 rc={rc}")

    rc, out, _ = run(["ping", "--transport", "mock"])
    _assert(rc == 0 and "PING OK" in out, f"mock ping 应 exit 0 并打印 PING OK，实际 rc={rc} out={out!r}")

    rc, out, _ = run(["mouse-move", "10", "-5", "--transport", "mock", "--no-wait"])
    _assert(rc == 0 and "fire-and-forget" in out, f"no-wait 连发异常 rc={rc} out={out!r}")

    rc, out, _ = run(["text", "hi", "--transport", "mock"])
    _assert(rc == 0 and "TEXT OK" in out, f"text 异常 rc={rc} out={out!r}")

    rc, _, err = run(["ping", "--transport", "tcp", "--host", "127.0.0.1", "--port", "8888"])
    _assert(rc == 6, f"tcp 未实现应 exit 6，实际 {rc} ({err.strip()[:120]})")
    _assert("未实现" in err or "NOT implemented" in err, "tcp 错误信息应说明未实现")

    rc, _, err = run(["ping", "--transport", "uart", "--port", "COM250", "--timeout", "0.2"])
    _assert(rc == 3, f"端口不存在应 exit 3，实际 {rc} ({err.strip()[:120]})")

    rc, _, err = run(["key-down", "nosuchkey", "--transport", "mock"])
    _assert(rc == 4, f"非法键名应 exit 4，实际 {rc} ({err.strip()[:120]})")

    # e2e-demo 用 mock 跑通（截屏->移动->截屏->差分）
    with tempfile.TemporaryDirectory() as td:
        rc, out, _ = run(["e2e-demo", "--transport", "mock", "--outdir", td, "--dx", "30"])
        _assert(rc == 0, f"e2e-demo(mock) 应 exit 0，实际 {rc}\n{out}")
        _assert("差分" in out and os.path.isfile(os.path.join(td, "e2e_before.png"))
                and os.path.isfile(os.path.join(td, "e2e_after.png")), "e2e-demo 应产出两张截图")

    # screenshot 子命令真出图
    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "shot.png")
        rc, out, _ = run(["screenshot", out_png])
        _assert(rc == 0, f"screenshot 应 exit 0，实际 {rc}\n{out}")
        _assert(os.path.isfile(out_png) and os.path.getsize(out_png) > 1024,
                "screenshot 应产出非空 PNG")
        rc, _, _ = run(["screenshot", os.path.join(td, "s2.png"), "--min-bytes", "999999999"])
        _assert(rc == 1, f"--min-bytes 未满足应 exit 1，实际 {rc}")

    # verify 子命令
    with tempfile.TemporaryDirectory() as td:
        import eyes
        p1 = os.path.join(td, "v1.png")
        p2 = os.path.join(td, "v2.png")
        eyes.capture(p1)
        eyes.capture(p2)
        rc, out, _ = run(["verify", p1, p2])
        _assert(rc == 0, f"verify 子命令应 exit 0，实际 {rc}")
        rc, _, _ = run(["verify", p1, p2, "--expect", "nochange"])
        _assert(rc in (0, 1), f"--expect 判定异常 rc={rc}")


@test("dependency_audit_stdlib_plus_pillow", "验收3")
def t_deps():
    """AST 扫全部源码 import：只允许标准库 + PIL（Pillow）+ 本目录模块。"""
    stdlib = set(sys.stdlib_module_names)
    allowed_third = {"PIL"}
    local = {p.stem for p in PC_DIR.glob("*.py")}
    offenders = []
    for path in sorted(PC_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n not in stdlib and n not in local and n not in allowed_third:
                    offenders.append(f"{path.name}: {n}")
    _assert(not offenders, f"发现非白名单依赖（只允许 stdlib + Pillow）: {offenders}")
    _assert(importlib.util.find_spec("serial") is None,
            "本机竟然装了 pyserial；本实现不依赖它（此断言用于证明裸环境也能跑）")
    for name in ("transport_uart", "agent_bridge", "mock_p4"):
        src = (PC_DIR / f"{name}.py").read_text(encoding="utf-8").lower()
        _assert("import serial" not in src and "pyserial" not in src.split('"""')[0],
                f"{name}.py 不应依赖 pyserial")


# ==========================================================================
# runner
# ==========================================================================
def _dep_report() -> list[str]:
    lines = [f"解释器/interpreter : {sys.version.split()[0]} ({sys.executable})"]
    try:
        import PIL
        lines.append(f"Pillow            : {PIL.__version__}  (唯一第三方依赖 / only 3rd-party dep)")
    except ImportError:  # pragma: no cover
        lines.append("Pillow            : 未安装（screenshot/e2e-demo 会失败）")
    serial_spec = importlib.util.find_spec("serial")
    lines.append(f"pyserial          : {'已安装但本实现不使用' if serial_spec else '未安装（符合预期，本实现用 ctypes+Win32）'}")
    return lines


def run(argv=None, verbose: bool = False, quiet: bool = False) -> int:
    parser = argparse.ArgumentParser(
        prog="selftest.py",
        description="内置 mock P4 协议自测（无需硬件）/ built-in mock P4 selftest.",
        epilog="退出码/exit: 0=全部通过, 1=有失败",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每个断言的细节")
    parser.add_argument("--quiet", "-q", action="store_true", help="只打印汇总")
    parser.add_argument("--filter", default=None, help="只跑名字包含该子串的用例")
    parser.add_argument("--json", action="store_true", help="JSON 结果")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    verbose = verbose or args.verbose
    quiet = quiet or args.quiet
    agent_bridge.setup_io()

    selected = [(n, r, f) for (n, r, f) in TESTS if (not args.filter or args.filter in n)]
    if not args.json:
        print("== agent_bridge selftest（内置 mock P4，无硬件 / no hardware required）==")
        for line in _dep_report():
            print("  " + line)
        print(f"  用例数/tests      : {len(selected)}")
        print("-" * 78)

    results = []
    t_start = time.perf_counter()
    for name, ref, fn in selected:
        t0 = time.perf_counter()
        ok, err, tb = True, "", ""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 自测要把所有失败收集完
            ok, err, tb = False, f"{type(exc).__name__}: {exc}", traceback.format_exc()
        ms = (time.perf_counter() - t0) * 1000
        results.append({"name": name, "ref": ref, "ok": ok, "error": err, "ms": round(ms, 2)})
        if not args.json and not quiet:
            print(f"[{'PASS' if ok else 'FAIL'}] {name:<46} {ref:<10} {ms:6.1f} ms"
                  + (f"  {err}" if not ok else ""))
            if not ok:
                print("       " + "\n       ".join(tb.strip().splitlines()[-6:]))
    total_ms = (time.perf_counter() - t_start) * 1000

    passed = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]

    # 协议覆盖矩阵
    cov: dict[str, int] = {}
    for r in results:
        cov[r["ref"]] = cov.get(r["ref"], 0) + 1
    if not args.json:
        print("-" * 78)
        if verbose:
            print("协议覆盖 / protocol coverage: " + ", ".join(f"{k}={v}" for k, v in sorted(cov.items())))
        extra = getattr(t_throughput, "measured", None)
        if extra:
            print(f"连发吞吐实测 / burst throughput (loopback): {extra['loopback_msg_per_s']:.0f} 条/秒  "
                  f"(帧 {extra['frame_bytes']}B；115200 8N1 理论上限 {extra['uart_theoretical_msg_per_s']:.0f} 条/秒)")
        print(f"结果 / result: {passed}/{len(results)} 通过 (passed), {len(failed)} 失败 (failed), "
              f"耗时 {total_ms:.0f} ms")
        if failed:
            print("失败用例 / failed: " + ", ".join(r["name"] for r in failed))
        print("== SELFTEST " + ("PASS" if not failed else "FAIL") + " ==")
    else:
        print(json.dumps({"passed": passed, "failed": len(failed), "total": len(results),
                          "duration_ms": round(total_ms, 1), "results": results,
                          "throughput": getattr(t_throughput, "measured", None)},
                         ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())