#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/hid-panic.py — 一键「解锁卡住的键/鼠标键」。

症状：鼠标滚轮只能缩放不能滚动、拖拽变成框选、点击变多选……本质是
**某个修饰键（常见 Ctrl / Shift / Alt / Win）在系统里卡在"按下"状态**。

成因：HID 键盘只发"按下"没发"抬起"（脚本崩了 / 串口重连导致板子复位 /
别的软件用 SendInput 只按不抬）。板子固件自己 release_all 只能清它自己那一路，
OS 侧的状态得另外补一次 keyup。

本工具做两件事，顺序执行：
  1) 通过 hub 透传让板子 release_all（清板子侧残留的键/鼠标键）；
  2) 用 SendInput 在 Windows 侧补发所有修饰键 + 鼠标键的「抬起」；
  3) 复查并打印 JSON（before/after），确认是否真的松开。

用法：
    python tools\\hid-panic.py                 # 板子 tcp 透传 + 系统 keyup
    python tools\\hid-panic.py --no-board      # 只修 Windows 侧（不用板子）
    python tools\\hid-panic.py --transport uart # 板子直连串口（hub 未运行时）

退出码：0 = 干净（所有修饰键/鼠标键均为抬起）；1 = 仍有键按住（可能是物理按键卡住）。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PC = os.path.join(os.path.dirname(_HERE), "pc")
if _PC not in sys.path:
    sys.path.insert(0, _PC)

u = ctypes.windll.user32
u.GetAsyncKeyState.restype = ctypes.c_short
u.GetAsyncKeyState.argtypes = [ctypes.c_int]

MODIFIERS = {
    "LCONTROL": 0xA2, "RCONTROL": 0xA3,
    "LSHIFT": 0xA0, "RSHIFT": 0xA1,
    "LMENU": 0xA4, "RMENU": 0xA5,
    "LWIN": 0x5B, "RWIN": 0x5C,
    "CAPITAL": 0x14,
}
MOUSE_BTNS = {"LMOUSE": 0x01, "RMOUSE": 0x02, "MMOUSE": 0x04, "XBUT1": 0x05, "XBUT2": 0x06}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XUP = 0x0100


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("padding", ctypes.c_char * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", INPUT_UNION)]


def _send(inp: INPUT) -> int:
    return u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def key_up(vk: int, scan: int, extended: bool = False) -> int:
    flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return _send(INPUT(type=INPUT_KEYBOARD,
                       u=INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                                   time=0, dwExtraInfo=None))))


def mouse_up(flag: int, data: int = 0) -> int:
    return _send(INPUT(type=INPUT_MOUSE,
                       u=INPUT_UNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=data,
                                                   dwFlags=flag, time=0,
                                                   dwExtraInfo=None))))


def state() -> dict:
    out = {}
    for name, vk in list(MODIFIERS.items()) + list(MOUSE_BTNS.items()):
        out[name] = bool(u.GetAsyncKeyState(vk) & 0x8000)
    return out


def pressed(s: dict) -> list:
    return [k for k, v in s.items() if v]


def windows_keyup() -> dict:
    sent = {}
    # 左右修饰键；扩展键（右 Ctrl/Alt、Win）显式标扩展位
    plan = [
        ("LCONTROL", 0xA2, 0x1D, False),
        ("RCONTROL", 0xA3, 0x1D, True),
        ("LSHIFT", 0xA0, 0x2A, False),
        ("RSHIFT", 0xA1, 0x36, False),
        ("LMENU", 0xA4, 0x38, False),
        ("RMENU", 0xA5, 0x38, True),
        ("LWIN", 0x5B, 0x5B, True),
        ("RWIN", 0x5C, 0x5C, True),
        ("CAPITAL", 0x14, 0x3A, False),
    ]
    for name, vk, scan, ext in plan:
        sent[name] = key_up(vk, scan, ext)
    # 鼠标键（含 X1/X2 两个方向）
    sent["LMOUSE"] = mouse_up(MOUSEEVENTF_LEFTUP)
    sent["RMOUSE"] = mouse_up(MOUSEEVENTF_RIGHTUP)
    sent["MMOUSE"] = mouse_up(MOUSEEVENTF_MIDDLEUP)
    sent["XBUT1"] = mouse_up(MOUSEEVENTF_XUP, 1)
    sent["XBUT2"] = mouse_up(MOUSEEVENTF_XUP, 2)
    return sent


def board_release(transport: str) -> dict:
    try:
        from brain import Brain  # 延迟导入：--no-board 时不需要
        with Brain(transport=transport) as b:
            return {"ok": True, "resp": b.release()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": repr(exc)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一键解锁卡住的修饰键/鼠标键")
    ap.add_argument("--no-board", action="store_true", help="只修 Windows 侧，不动板子")
    ap.add_argument("--transport", default="tcp", choices=["tcp", "uart"])
    a = ap.parse_args(argv)

    before = state()
    report = {"before_pressed": pressed(before)}
    if not a.no_board:
        report["board"] = board_release(a.transport)
        time.sleep(0.15)
        report["after_board"] = pressed(state())
    report["sendinput"] = windows_keyup()
    time.sleep(0.25)
    after = state()
    report["after_pressed"] = pressed(after)
    report["clean"] = not pressed(after)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
