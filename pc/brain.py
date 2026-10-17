"""brain.py - 大脑层：把板子的「手」和截屏的「眼」封装成可编排的动作。

设计要点
--------
1. **绝对坐标靠闭环收敛**：HID 鼠标只有相对移动，没有 SetCursorPos 的权限。
   本模块只读 `GetCursorPos()` 做反馈，位移一律由板子发出，直到误差 <= 容差。
   这样既保住了"所有输入都真的经过 USB HID"这条底线，又拿到了绝对定位能力。
2. **一次连接跑一组动作**：`run --script plan.json` 在一个串口会话里顺序执行，
   避免每条命令都开关串口（省 1.5 s/条，也避免反复 DTR 抖动）。
3. **可验证**：每个动作返回结构化结果；`look`/`diff` 用于动作前后取证。

动作清单（`run` 的 op 与子命令一一对应）
    look / point / move / click / drag / scroll / type / key / tap /
    hold / release / sleep / wins / focus / launch / clip / diff / wait_change / info

退出时无条件 release_all，绝不留按住的键。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from ctypes import wintypes  # noqa: E402

import eyes  # noqa: E402
import verify  # noqa: E402
from agent_bridge import AgentBridge, BridgeError, ProtocolError  # noqa: E402
from transport_base import TransportError  # noqa: E402
from transport_uart import UartTransport  # noqa: E402

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# 全模块统一用物理像素坐标；否则 125% 缩放下 wins 给出的 rect 会和
# GetCursorPos() 差 1.25 倍，点击就会打偏。
eyes.enable_dpi_awareness()

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9


# --------------------------------------------------------------------------- #
# Win32 观测原语（只读，绝不代替板子输入）
# --------------------------------------------------------------------------- #


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

# --- Win32 原型声明：64 位下不声明 restype，指针会被截断成 32 位 int 而崩溃 ---
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = wintypes.HANDLE
k32.GlobalLock.argtypes = [wintypes.HANDLE]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
k32.GlobalUnlock.restype = wintypes.BOOL
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = wintypes.HANDLE
u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
u32.SetClipboardData.restype = wintypes.HANDLE
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.OpenClipboard.restype = wintypes.BOOL
u32.EmptyClipboard.restype = wintypes.BOOL
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
u32.GetForegroundWindow.restype = wintypes.HWND
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsIconic.argtypes = [wintypes.HWND]



def cursor_pos() -> tuple[int, int]:
    pt = POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def screen_size() -> tuple[int, int]:
    eyes.enable_dpi_awareness()
    return (int(u32.GetSystemMetrics(0)), int(u32.GetSystemMetrics(1)))


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_windows() -> list[dict]:
    """可见的顶层窗口：标题、矩形、pid、是否前台。"""
    out: list[dict] = []
    fg = u32.GetForegroundWindow()

    def _cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        r = RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({
            "hwnd": int(hwnd),
            "title": title,
            "pid": int(pid.value),
            "rect": [int(r.left), int(r.top), int(r.right), int(r.bottom)],
            "foreground": int(hwnd) == int(fg),
            "minimized": bool(u32.IsIconic(hwnd)),
        })
        return True

    u32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def focus_window(substr: str) -> dict:
    """按标题子串把窗口切到前台。"""
    hits = [w for w in list_windows() if substr.lower() in w["title"].lower()]
    if not hits:
        return {"ok": False, "error": "no window matches %r" % substr}
    w = hits[0]
    hwnd = w["hwnd"]
    if w["minimized"]:
        u32.ShowWindow(hwnd, SW_RESTORE)
    # SetForegroundWindow 有前台锁；先试一次，失败就模拟 Alt 键解锁（经典做法）
    if not u32.SetForegroundWindow(hwnd):
        u32.keybd_event(0x12, 0, 0, 0)
        u32.keybd_event(0x12, 0, 2, 0)
        u32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return {"ok": u32.GetForegroundWindow() == hwnd, "window": w, "candidates": len(hits)}


def clip_get() -> str:
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = k32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def clip_set(text: str) -> bool:
    data = (text + "\x00").encode("utf-16-le")
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        u32.CloseClipboard()


# --------------------------------------------------------------------------- #
# 大脑
# --------------------------------------------------------------------------- #
# 实测增益表（tools/calibrate-pointer.py 测得，1280@2560x1440 125% 缩放）：
# 请求位移 m -> 光标实际位移 gain(m) * m。加速曲线在 m>=12 后稳定，m<=8 有量化噪声。
_GAIN_TABLE = [
    (1, 1.00), (3, 0.90), (5, 0.90), (8, 1.00), (12, 1.28), (16, 1.67),
    (20, 1.98), (24, 2.22), (30, 2.46), (40, 2.67), (60, 2.91),
    (80, 3.02), (120, 3.13), (200, 3.09),
]


def _gain(mag: float) -> float:
    """按实测表线性插值求增益；超出范围用端点值。"""
    if mag <= _GAIN_TABLE[0][0]:
        return _GAIN_TABLE[0][1]
    if mag >= _GAIN_TABLE[-1][0]:
        return _GAIN_TABLE[-1][1]
    for i in range(len(_GAIN_TABLE) - 1):
        m0, g0 = _GAIN_TABLE[i]
        m1, g1 = _GAIN_TABLE[i + 1]
        if m0 <= mag <= m1:
            t = (mag - m0) / (m1 - m0)
            return g0 + (g1 - g0) * t
    return _GAIN_TABLE[-1][1]


def set_gain_table(rows) -> list:
    """用外部标定结果覆盖内置增益表（换设备后用，见 tools/home-calibrate.py）。

    ``rows``: ``[(counts, gain), ...]``；counts > 0 且 gain > 0 才生效。
    返回当前生效的表（非法输入不修改原表）。
    """
    global _GAIN_TABLE
    try:
        cleaned = sorted((float(m), float(g)) for m, g in rows
                         if float(m) > 0 and float(g) > 0)
    except (TypeError, ValueError):
        return _GAIN_TABLE
    if cleaned:
        _GAIN_TABLE = cleaned
    return _GAIN_TABLE


def _step_for(err: int) -> int:
    """把误差换算成本次该发给板子的相对位移。

    Windows「提高指针精确度」把相对位移按速度非线性放大，所以：
      大误差 -> 按实测增益反推，一步吃掉大半，且不过冲；
      小误差 -> 1:1 逐像素精修（此区间增益≈1）。
    """
    a = abs(err)
    if a == 0:
        return 0
    if a <= 3:
        return err                      # 1:1 精修区
    mag = int(round(a / _gain(a)))
    mag = max(1, min(200, mag))
    return mag if err > 0 else -mag


# --------------------------------------------------------------------------- #
# 抗打断：区分「板子挪的」和「用户手挪的」
# --------------------------------------------------------------------------- #
class PointerGuard:
    """检测真鼠标介入。

    HID 只能发相对位移，Windows 指针加速又按速度放大，所以"期望位移"只能用
    实测增益表预测。预测本身有 10%~20% 误差，阈值必须留够余量：

    * 偏差 <= max(tol_px, rel_tol*|期望位移|)  -> 认为是我们的移动，正常；
    * 偏差更大，或方向与我们发**完全相反**且幅度 > tol_px -> 判定用户介入。

    用户介入不是错误：调用方应把当前真实光标当新锚点，而不是硬拽回原轨迹。
    连续 `max_consecutive` 次介入则说明用户在抢鼠标，应当停下。
    """

    def __init__(self, tol_px: int = 15, rel_tol: float = 0.30,
                 max_consecutive: int = 4):
        self.tol_px = int(tol_px)
        self.rel_tol = float(rel_tol)
        self.max_consecutive = int(max_consecutive)
        self.consecutive = 0
        self.total = 0
        self.events: list[dict] = []

    def expected(self, sx: int, sy: int) -> tuple[float, float]:
        return (sx * _gain(abs(sx)) if sx else 0.0,
                sy * _gain(abs(sy)) if sy else 0.0)

    def observe(self, before, after, sx: int, sy: int) -> dict:
        """一次移动的观测：返回 {interrupt, deviation, expected, actual}。"""
        delta = (after[0] - before[0], after[1] - before[1])
        exp = self.expected(sx, sy)
        dev = math.hypot(delta[0] - exp[0], delta[1] - exp[1])
        exp_mag = math.hypot(*exp)
        allowed = max(self.tol_px, self.rel_tol * exp_mag)
        # 方向反转且幅度够大 -> 一定是外力
        reversed_dir = (delta[0] * exp[0] + delta[1] * exp[1]) < 0
        big = math.hypot(*delta) > self.tol_px
        interrupt = dev > allowed or (reversed_dir and big and exp_mag > 0)
        ev = {"interrupt": bool(interrupt), "deviation": round(dev, 1),
              "expected": [round(exp[0], 1), round(exp[1], 1)],
              "actual": list(delta), "sent": [int(sx), int(sy)],
              "allowed": round(allowed, 1),
              "at": list(after)}
        if interrupt:
            self.consecutive += 1
            self.total += 1
            self.events.append(ev)
            if len(self.events) > 40:
                self.events = self.events[-40:]
        else:
            self.consecutive = 0
        return ev

    def should_pause(self) -> bool:
        return self.consecutive >= self.max_consecutive

    def summary(self) -> dict:
        return {"interruptions": self.total, "consecutive": self.consecutive,
                "events": list(self.events[-10:])}



# 常用简写 -> 协议 §4 规范键名（大脑层替上层兜住这个细节）
_KEY_ALIASES = {
    "ctrl": "leftctrl", "control": "leftctrl",
    "alt": "leftalt", "shift": "leftshift",
    "win": "leftgui", "cmd": "leftgui", "meta": "leftgui", "super": "leftgui",
    "enter": "enter", "return": "enter",
    "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
}


def norm_combo(keys) -> list[str]:
    """把 [ctrl, s] / ["alt+f4"] 统一成协议规范键名列表。"""
    if isinstance(keys, str):
        keys = [p for p in keys.replace("+", " ").split() if p]
    out = []
    for k in keys:
        k = str(k).strip().lower()
        if not k:
            continue
        for part in k.replace("+", " ").split():
            out.append(_KEY_ALIASES.get(part, part))
    return out


class Brain:
    def __init__(self, port: str = "COM7", baud: int = 115200, timeout: float = 1.5,
                 verbose: bool = False, transport: str = "uart",
                 host: str = "127.0.0.1", tcp_port: int = 8766,
                 fallback_uart: bool = True):
        self.port = port
        self.transport_name = transport
        self.transport_note = None
        self.screen_w, self.screen_h = screen_size()
        tr = None
        if transport == "tcp":
            # 串口唯一：hub 独占 COM7，这里只连本地 TCP 透传，不抢串口
            from transport_tcp import TcpTransport
            tr = TcpTransport(host=host, port=tcp_port, timeout=timeout)
            try:
                tr.open()
            except TransportError as exc:
                self.transport_note = "TCP %s:%d 不可用: %s" % (host, tcp_port, exc)
                if not fallback_uart:
                    raise
                print("[transport] %s -> 回退 UART %s" % (self.transport_note, port),
                      file=sys.stderr)
                self.transport_name = "uart"
                tr = UartTransport(port=port, baud=baud)
        else:
            tr = UartTransport(port=port, baud=baud)
        self.bridge = AgentBridge(tr, timeout=timeout, release_on_close=True)
        self.bridge.open()
        self.verbose = verbose
        # 首帧竞态：open 后 CH340 的 DTR/RTS 抖动可能吃掉一帧，先握手
        try:
            self.bridge.ping()
        except BridgeError:
            time.sleep(0.15)
            self.bridge.ping()
        self.safety_timeout_ms = None
        self._actions = 0

        # 换设备自动适配：本机若跑过 tools/home-calibrate.py（CNC 四角归零），
        # 就用那台设备实测的增益表；没有档案则保持内置表（闭环照样收敛）。
        self.gain_source = "builtin"
        try:
            import pointer_profile
            table = pointer_profile.apply_to_brain(sys.modules[__name__])
            if table:
                self.gain_source = "profile:%s" % pointer_profile.screen_signature()
        except Exception:  # noqa: BLE001
            pass

    # -- 生命周期 -----------------------------------------------------------
    def close(self) -> None:
        try:
            self.bridge.release_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.bridge.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 观测 ---------------------------------------------------------------
    def look(self, path: str | None = None, region: str | None = None) -> dict:
        if path is None:
            path = os.path.join(_HERE, "shot.png")
        shot, report = eyes.capture_report(
            path, region=eyes.parse_region(region) if region else None)
        wins = list_windows()
        fg = next((w for w in wins if w["foreground"]), None)
        return {
            "ok": True,
            "shot": shot.path,
            "width": shot.width,
            "height": shot.height,
            "bytes": shot.size,
            "matches_primary": report.get("matches_primary"),
            "dpi_mode": report.get("dpi_mode"),
            "cursor": list(cursor_pos()),
            "screen": [self.screen_w, self.screen_h],
            "foreground": {"title": fg["title"], "rect": fg["rect"]} if fg else None,
            "windows": [{"title": w["title"], "rect": w["rect"],
                         "foreground": w["foreground"], "minimized": w["minimized"]}
                        for w in wins[:25]],
        }

    def diff(self, a: str, b: str, threshold: int = 0) -> dict:
        res = verify.diff_images(a, b, threshold=threshold)
        d = verify.diff_summary(res, a, b)
        d["ok"] = d.get("changed", 0) > 0
        return d

    def read_text(self, select_all: bool = True, restore_clip: bool = True,
                  paste_wait_ms: int = 250) -> dict:
        """用 HID 按键「读」前台窗口的文字：ctrl+a -> ctrl+c -> 读剪贴板。

        给没有视觉能力的文本模型一条真正的读屏通道；读到的内容由 Windows
        剪贴板给出，与板子自报无关，所以是可以当证据用的。
        默认读完把用户原来的剪贴板还原回去。
        """
        before = clip_get()
        if select_all:
            self.bridge.key_combo(norm_combo(["ctrl", "a"]))
            time.sleep(0.12)
        self.bridge.key_combo(norm_combo(["ctrl", "c"]))
        time.sleep(paste_wait_ms / 1000.0)
        text = clip_get()
        restored = False
        if restore_clip and text != before:
            restored = clip_set(before)
        return {"ok": True, "text": text, "chars": len(text),
                "clip_restored": restored,
                "changed": text != before}

    # -- 绝对定位（闭环收敛）------------------------------------------------
    def _move_step(self, sx: int, sy: int, guard: PointerGuard | None,
                   settle_ms: int = 10) -> dict | None:
        """发一次相对位移；有 guard 时读回光标做介入判定。"""
        if guard is None:
            self.bridge.mouse_move(sx, sy)
            self._actions += 1
            return None
        before = cursor_pos()
        self.bridge.mouse_move(sx, sy)
        self._actions += 1
        time.sleep(settle_ms / 1000.0)
        return guard.observe(before, cursor_pos(), sx, sy)

    def locate(self, tx: int, ty: int, tol: int = 1, max_iter: int = 40,
               guard: PointerGuard | None = None, settle_ms: int = 10) -> dict:
        """把光标闭环挪到 (tx, ty)，直到 |误差| <= tol（默认 1 px）。

        与 ``point`` 的区别：容差更严、带抗打断检测。每一步都重新读
        ``GetCursorPos()`` 算残差，所以用户中途推了鼠标，下一步自然会把
        当前真实位置当新锚点，不会硬拽回旧轨迹。
        """
        tx = max(0, min(self.screen_w - 1, int(tx)))
        ty = max(0, min(self.screen_h - 1, int(ty)))
        start = cursor_pos()
        stuck = 0
        last = start
        interruptions = 0
        for i in range(1, max_iter + 1):
            x, y = cursor_pos()
            ex, ey = tx - x, ty - y
            if abs(ex) <= tol and abs(ey) <= tol:
                return {"ok": True, "iterations": i - 1, "target": [tx, ty],
                        "final": [x, y], "error": [ex, ey], "start": list(start),
                        "interruptions": interruptions}
            sx, sy = _step_for(ex), _step_for(ey)
            if sx == 0 and sy == 0:
                sx, sy = (1 if ex > 0 else -1 if ex < 0 else 0,
                          1 if ey > 0 else -1 if ey < 0 else 0)
            ev = self._move_step(sx, sy, guard, settle_ms)
            if ev is not None and ev["interrupt"]:
                interruptions += 1
                if guard is not None and guard.should_pause():
                    x, y = cursor_pos()
                    return {"ok": False, "iterations": i, "target": [tx, ty],
                            "final": [x, y], "error": [tx - x, ty - y],
                            "start": list(start), "interruptions": interruptions,
                            "reason": "user interference (pointer moved by user)",
                            "guard": guard.summary()}
            now = cursor_pos()
            if now == last:
                stuck += 1
                if stuck >= 4:
                    break
            else:
                stuck = 0
            last = now
        x, y = cursor_pos()
        return {"ok": False, "iterations": max_iter, "target": [tx, ty],
                "final": [x, y], "error": [tx - x, ty - y], "start": list(start),
                "interruptions": interruptions, "reason": "did not converge"}

    def point(self, tx: int, ty: int, tol: int = 2, max_iter: int = 60,
              guard: PointerGuard | None = None) -> dict:
        """兼容旧行为：默认容差 2 px 的闭环定位。"""
        return self.locate(tx, ty, tol=tol, max_iter=max_iter, guard=guard)

    def drag_path(self, points, button: str = "left",
                  guard: PointerGuard | None = None,
                  tol: int = 1, settle_ms: int = 10,
                  max_segment_gap: int = 0) -> dict:
        """沿 ``points`` 用**一次按下-连续移动-抬起**画一条折线。

        与 ``drag`` 的区别：``drag`` 是"定位→按下→定位→抬起"，中间没有轨迹；
        ``drag_path`` 在按键保持期间逐点闭环移动，落笔即连续线条，适合画弧线。

        抗打断：每个中间点用 ``locate`` 闭环；若检测到用户动了真鼠标
        （``PointerGuard``），会抬笔、把当前真实位置当新锚点、从**下一个规划点**
        继续画（留下一个小缺口，但绝不把用户的乱动画成长线），并记录事件。
        连续多次介入（guard.should_pause()）则中止剩余笔画。

        返回 {ok, points, interruptions, resumed, events, guard}
        """
        pts = [(int(round(x)), int(round(y))) for x, y in points]
        if len(pts) < 2:
            return {"ok": False, "reason": "drag_path 至少需要 2 个点"}
        res = {"ok": True, "points": len(pts), "interruptions": 0, "resumed": 0,
               "events": [], "aborted": False}
        a = self.locate(pts[0][0], pts[0][1], tol=tol, guard=guard,
                        settle_ms=settle_ms)
        if not a["ok"]:
            res.update({"ok": False, "reason": "start point not reached",
                        "start": a})
            return res
        self.bridge.mouse_btn(button, "down")
        self._actions += 1
        pen_down = True
        i = 1
        try:
            while i < len(pts):
                r = self.locate(pts[i][0], pts[i][1], tol=tol, guard=guard,
                                settle_ms=settle_ms)
                res["interruptions"] += r.get("interruptions", 0)
                if not r["ok"] and r.get("reason", "").startswith("user interference"):
                    # 用户在抢鼠标：抬笔（别把他拖出来的线继续画），记录，然后从
                    # 下一个规划点重新落笔。当前真实位置就是新锚点。
                    if pen_down:
                        self.bridge.mouse_btn(button, "up")
                        self._actions += 1
                        pen_down = False
                    ev = {"index": i, "at": r.get("final"), "error": r.get("error"),
                          "guard": r.get("guard")}
                    res["events"].append(ev)
                    res["resumed"] += 1
                    if guard is not None and guard.should_pause():
                        res["aborted"] = True
                        res["reason"] = "user interference: paused"
                        break
                    i += 2                     # 跳过一个点，换个落笔位置
                    if i < len(pts):
                        rr = self.locate(pts[i][0], pts[i][1], tol=tol,
                                         guard=guard, settle_ms=settle_ms)
                        if rr["ok"]:
                            self.bridge.mouse_btn(button, "down")
                            self._actions += 1
                            pen_down = True
                        else:                  # 落笔点都到不了，剩余笔画放弃
                            res["aborted"] = True
                            res["reason"] = "resume point not reached"
                            break
                    continue
                if not r["ok"]:
                    res.update({"ok": False, "reason": "segment point not reached",
                                "segment": i, "detail": r})
                    break
                i += 1
        finally:
            if pen_down:
                self.bridge.mouse_btn(button, "up")
                self._actions += 1
        if not res.get("ok") and "reason" not in res:
            res["reason"] = "unknown"
        return res


    # -- 动作 ---------------------------------------------------------------
    def move(self, dx: int, dy: int) -> dict:
        r = self.bridge.mouse_move(dx, dy)
        self._actions += 1
        return {"ok": bool(r.get("ok", True)), "cursor": list(cursor_pos()), "raw": r}

    def click(self, at=None, button: str = "left", count: int = 1,
              interval_ms: int = 80) -> dict:
        pre = None
        if at is not None:
            pre = self.point(at[0], at[1])
            if not pre["ok"]:
                return {"ok": False, "reason": "point failed", "point": pre}
        r = self.bridge.mouse_click(btn=button, count=count, interval_ms=interval_ms)
        self._actions += count
        return {"ok": bool(r.get("ok", True)), "at": list(cursor_pos()),
                "button": button, "count": count, "point": pre, "raw": r}

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             button: str = "left") -> dict:
        a = self.point(x1, y1)
        if not a["ok"]:
            return {"ok": False, "reason": "point start failed", "point": a}
        self.bridge.mouse_btn(button, "down")
        self._actions += 1
        time.sleep(0.05)
        b = self.point(x2, y2)
        time.sleep(0.05)
        self.bridge.mouse_btn(button, "up")
        self._actions += 1
        return {"ok": a["ok"] and b["ok"], "from": [x1, y1], "to": [x2, y2],
                "button": button, "start": a, "end": b}

    def scroll(self, amount: int) -> dict:
        """amount > 0 = 向上滚（协议 wheel 正值）。"""
        r = self.bridge.mouse_scroll(int(amount))
        self._actions += 1
        return {"ok": bool(r.get("ok", True)), "amount": int(amount), "raw": r}

    def type_text(self, text: str, cps: int = 40) -> dict:
        resps = self.bridge.text(text, cps=cps)
        self._actions += len(text)
        bad = [r for r in resps if r and r.get("ok") is False]
        return {"ok": not bad, "chars": len(text), "cps": cps,
                "errors": bad[:3], "chunks": len(resps)}

    def key(self, combo) -> dict:
        """组合键：["ctrl","s"] 表示按下 ctrl+s 再松开。"""
        combo = norm_combo(combo)
        r = self.bridge.key_combo(combo)
        self._actions += 1
        return {"ok": bool(r.get("ok", True)), "combo": list(combo), "raw": r}

    def tap(self, name: str, hold_ms: int = 30) -> dict:
        if "+" in name:                      # alt+f4 这类直接当组合键处理
            return self.key(name)
        r = self.bridge.key_combo(norm_combo([name]), hold_ms=hold_ms)
        self._actions += 1
        return {"ok": bool(r.get("ok", True)), "key": name, "raw": r}

    def hold(self, keys) -> dict:
        keys = norm_combo(keys)
        r = self.bridge.key_down(keys)
        self._actions += 1
        return {"ok": bool(r.get("ok", True)), "held": list(keys), "raw": r}

    def release(self) -> dict:
        r = self.bridge.release_all()
        return {"ok": bool(r and r.get("ok", True)), "raw": r}

    def launch(self, cmd) -> dict:
        if isinstance(cmd, str):
            cmd = cmd.split()
        p = subprocess.Popen(cmd, shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "pid": p.pid, "cmd": cmd}

    def sleep(self, ms: int) -> dict:
        time.sleep(ms / 1000.0)
        return {"ok": True, "ms": int(ms)}

    def wait_change(self, timeout_ms: int = 3000, interval_ms: int = 180,
                    threshold: int = 0) -> dict:
        base = os.path.join(_HERE, "_wait_base.png")
        eyes.capture(base)
        t0 = time.monotonic()
        n = 0
        while (time.monotonic() - t0) * 1000 < timeout_ms:
            time.sleep(interval_ms / 1000.0)
            cur = os.path.join(_HERE, "_wait_cur.png")
            eyes.capture(cur)
            res = verify.diff_images(base, cur, threshold=threshold)
            n += 1
            changed = getattr(res, "changed", 0)
            if changed > 0:
                return {"ok": True, "changed": changed, "samples": n,
                        "elapsed_ms": int((time.monotonic() - t0) * 1000), "shot": cur}
        return {"ok": False, "changed": 0, "samples": n,
                "elapsed_ms": int((time.monotonic() - t0) * 1000), "reason": "timeout"}

    def info(self) -> dict:
        st = self.bridge.status()
        return {"ok": True, "port": self.port, "transport": self.transport_name,
                "transport_note": self.transport_note,
                "screen": [self.screen_w, self.screen_h],
                "cursor": list(cursor_pos()), "status": st, "actions": self._actions}


# --------------------------------------------------------------------------- #
# 脚本执行（一次连接跑一组动作）
# --------------------------------------------------------------------------- #
_OPS = {"look", "point", "locate", "move", "click", "drag", "drag_path",
        "scroll", "type", "key", "tap", "hold", "release", "sleep", "wins",
        "focus", "launch", "clip_get", "clip_set", "diff", "wait_change",
        "read_text", "info", "stop"}


def exec_step(b: Brain, step: dict) -> dict:
    op = step.get("op")
    if op not in _OPS:
        return {"ok": False, "op": op, "error": "unknown op"}

    if op == "look":
        return b.look(step.get("path"), step.get("region"))
    if op == "point":
        return b.point(step["x"], step["y"], tol=step.get("tol", 2))
    if op == "locate":
        return b.locate(step["x"], step["y"], tol=step.get("tol", 1))
    if op == "drag_path":
        return b.drag_path(step["points"], step.get("button", "left"),
                           guard=PointerGuard() if step.get("guard", True) else None)
    if op == "move":
        return b.move(step["dx"], step["dy"])
    if op == "click":
        at = None
        if "x" in step:
            at = (step["x"], step["y"])
        return b.click(at, step.get("button", "left"), step.get("count", 1))
    if op == "drag":
        return b.drag(step["x1"], step["y1"], step["x2"], step["y2"],
                      step.get("button", "left"))
    if op == "scroll":
        return b.scroll(step["amount"])
    if op == "type":
        return b.type_text(step["text"], step.get("cps", 40))
    if op == "key":
        return b.key(step["combo"])
    if op == "tap":
        return b.tap(step["key"], step.get("hold_ms", 30))
    if op == "hold":
        return b.hold(step["keys"])
    if op == "release":
        return b.release()
    if op == "sleep":
        return b.sleep(step.get("ms", 200))
    if op == "wins":
        return {"ok": True, "windows": list_windows()}
    if op == "focus":
        return focus_window(step["title"])
    if op == "launch":
        return b.launch(step["cmd"])
    if op == "clip_get":
        return {"ok": True, "text": clip_get()}
    if op == "clip_set":
        return {"ok": clip_set(step["text"])}
    if op == "read_text":
        return b.read_text(step.get("select_all", True),
                           step.get("restore_clip", True))
    if op == "diff":
        return b.diff(step["a"], step["b"], step.get("threshold", 0))
    if op == "wait_change":
        return b.wait_change(step.get("timeout_ms", 3000),
                             step.get("interval_ms", 180),
                             step.get("threshold", 0))
    if op == "info":
        return b.info()
    if op == "stop":
        return {"ok": True, "op": "stop"}
    return {"ok": False, "op": op, "error": "unreachable"}


def run_script(b: Brain, steps: list[dict], stop_on_fail: bool = True,
               out=sys.stdout) -> list[dict]:
    results = []
    for i, step in enumerate(steps, 1):
        op = step.get("op")
        t0 = time.perf_counter()
        try:
            res = exec_step(b, step)
        except (BridgeError, ProtocolError, KeyError, ValueError) as exc:
            res = {"ok": False, "op": op, "error": "%s: %s" % (type(exc).__name__, exc)}
        res["_ms"] = int((time.perf_counter() - t0) * 1000)
        res["_step"] = i
        results.append(res)
        out.write("[%02d] %-12s %s\n" % (i, op, _brief(res)))
        out.flush()
        if op == "stop":
            break
        if stop_on_fail and not res.get("ok"):
            out.write("     !! 失败，中止脚本（用 --keep-going 可继续）\n")
            break
    return results


def _brief(res: dict) -> str:
    keys = ("iterations", "error", "final", "cursor", "at", "chars", "combo",
            "key", "changed", "elapsed_ms", "shot", "width", "height", "text",
            "title", "reason", "error", "pid", "samples")
    parts = []
    for k in keys:
        if k in res and res[k] not in (None, "", []):
            v = res[k]
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            if isinstance(v, str) and len(v) > 40:
                v = v[:37] + "..."
            parts.append("%s=%s" % (k, v))
    return "OK  " + " ".join(parts) if res.get("ok") else "FAIL " + " ".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brain.py",
        description="大脑层：用 ESP32-P4 板子的 HID 键鼠 + 截屏闭环操作本机")
    p.add_argument("--port", default=os.environ.get("HID_PORT", "COM7"))
    p.add_argument("--transport", default=os.environ.get("HID_TRANSPORT", "uart"),
                   choices=["uart", "tcp"],
                   help="uart=直连 COM7；tcp=连本地 hub 的 TCP 透传（不抢串口）")
    p.add_argument("--host", default=os.environ.get("HID_HUB_HOST", "127.0.0.1"))
    p.add_argument("--tcp-port", type=int,
                   default=int(os.environ.get("HID_HUB_PORT", "8766")))
    p.add_argument("--no-fallback", action="store_true",
                   help="tcp 连不上时不回退 uart，直接报错")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("look", help="截屏 + 报告光标/前台窗口/窗口列表").add_argument("--region")
    sp = sub.add_parser("point", help="把光标移动到绝对坐标（闭环收敛）")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.add_argument("--tol", type=int, default=2)
    sp = sub.add_parser("locate", help="绝对定位（1px 闭环 + 抗打断检测）")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.add_argument("--tol", type=int, default=1)
    sp = sub.add_parser("drag-path", help="按折线连续拖拽（一次按下-移动-抬起）")
    sp.add_argument("points", help="x1,y1;x2,y2;... 或 JSON 数组文件路径")
    sp = sub.add_parser("move", help="相对移动")
    sp.add_argument("dx", type=int)
    sp.add_argument("dy", type=int)
    sp = sub.add_parser("click", help="点击（给坐标则先定位）")
    sp.add_argument("x", type=int, nargs="?")
    sp.add_argument("y", type=int, nargs="?")
    sp.add_argument("--button", default="left", choices=["left", "right", "middle"])
    sp.add_argument("--count", type=int, default=1)
    sp = sub.add_parser("drag", help="拖拽")
    sp.add_argument("x1", type=int)
    sp.add_argument("y1", type=int)
    sp.add_argument("x2", type=int)
    sp.add_argument("y2", type=int)
    sp = sub.add_parser("scroll", help="滚轮，正=向上")
    sp.add_argument("amount", type=int)
    sp = sub.add_parser("type", help="输入文本")
    sp.add_argument("text")
    sp.add_argument("--cps", type=int, default=40)
    sp = sub.add_parser("key", help="组合键，如 ctrl+s / win+r")
    sp.add_argument("combo")
    sp = sub.add_parser("tap", help="单击一个键")
    sp.add_argument("key")
    sp = sub.add_parser("wins", help="列出可见窗口")
    sp = sub.add_parser("focus", help="按标题子串切前台")
    sp.add_argument("title")
    sp = sub.add_parser("launch", help="启动本机进程（不走 HID）")
    sp.add_argument("argv", nargs="+", help="可执行文件 + 参数")
    sp = sub.add_parser("clip", help="读/写剪贴板")
    sp.add_argument("--set")
    sp = sub.add_parser("read-text", help="用 ctrl+a/ctrl+c 读前台窗口的文字")
    sp.add_argument("--no-select-all", action="store_true")
    sp = sub.add_parser("info", help="板子状态 + 屏幕/光标")
    sp = sub.add_parser("run", help="一次连接执行一组动作（JSON 脚本）")
    sp.add_argument("--script", required=True, help="JSON 文件，内容是动作数组")
    sp.add_argument("--keep-going", action="store_true", help="某步失败也继续")
    return p


def _load_steps(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("steps", [])
    if not isinstance(data, list):
        raise SystemExit("脚本必须是动作数组")
    return data


def _parse_points(spec: str) -> list[tuple[int, int]]:
    """解析 ``x1,y1;x2,y2;...`` 或 JSON 文件 ``[[x,y],...]``。"""
    spec = spec.strip()
    if spec.startswith("[") or os.path.isfile(spec):
        with open(spec, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [(int(p[0]), int(p[1])) for p in data]
    pts = []
    for chunk in spec.replace(";", " ").split():
        x, y = chunk.split(",")
        pts.append((int(float(x)), int(float(y))))
    return pts


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 需要板子的动作才建连接；纯本机动作（wins/focus/clip/launch）不必占用串口
    need_board = args.cmd not in ("wins", "focus", "clip", "launch")

    if not need_board:
        if args.cmd == "wins":
            res = {"ok": True, "windows": list_windows()}
        elif args.cmd == "focus":
            res = focus_window(args.title)
        elif args.cmd == "clip":
            res = ({"ok": clip_set(args.set), "text": args.set} if args.set is not None
                   else {"ok": True, "text": clip_get()})
        else:
            p = subprocess.Popen(args.argv, shell=False,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            res = {"ok": True, "pid": p.pid}
        print(json.dumps(res, ensure_ascii=False) if args.json else _pretty(res))
        return 0 if res.get("ok") else 1

    try:
        brain = Brain(port=args.port, transport=args.transport, host=args.host,
                      tcp_port=args.tcp_port, fallback_uart=not args.no_fallback)
    except Exception as exc:  # noqa: BLE001
        print("[PORT-ERROR] %s" % exc, file=sys.stderr)
        print("  hint: python pc/agent_bridge.py --list-ports", file=sys.stderr)
        print("        hub 在跑吗? python pc/hub.py（默认 127.0.0.1:8766）", file=sys.stderr)
        return 3

    try:
        if args.cmd == "look":
            res = brain.look(region=args.region)
        elif args.cmd == "point":
            res = brain.point(args.x, args.y, tol=args.tol)
        elif args.cmd == "locate":
            res = brain.locate(args.x, args.y, tol=args.tol)
        elif args.cmd == "drag-path":
            res = brain.drag_path(_parse_points(args.points), guard=PointerGuard())
        elif args.cmd == "move":
            res = brain.move(args.dx, args.dy)
        elif args.cmd == "click":
            at = (args.x, args.y) if args.x is not None and args.y is not None else None
            res = brain.click(at, args.button, args.count)
        elif args.cmd == "drag":
            res = brain.drag(args.x1, args.y1, args.x2, args.y2)
        elif args.cmd == "scroll":
            res = brain.scroll(args.amount)
        elif args.cmd == "type":
            res = brain.type_text(args.text, args.cps)
        elif args.cmd == "key":
            res = brain.key(args.combo)
        elif args.cmd == "tap":
            res = brain.tap(args.key)
        elif args.cmd == "read-text":
            res = brain.read_text(select_all=not args.no_select_all)
        elif args.cmd == "info":
            res = brain.info()
        elif args.cmd == "run":
            steps = _load_steps(args.script)
            results = run_script(brain, steps, stop_on_fail=not args.keep_going,
                                 out=sys.stderr if args.json else sys.stdout)
            failed = [r for r in results if not r.get("ok")]
            res = {"ok": not failed, "steps": len(results), "failed": len(failed),
                   "results": results}
        else:
            res = {"ok": False, "error": "unhandled cmd"}
    finally:
        brain.close()

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.cmd == "run":
        print("RESULT: %s  steps=%s failed=%s"
              % ("OK" if res.get("ok") else "FAIL", res.get("steps"), res.get("failed")))
    else:
        print(_pretty(res))
    return 0 if res.get("ok") else 1


def _pretty(res: dict) -> str:
    if "windows" in res and isinstance(res["windows"], list):
        lines = ["%s %-50s %s" % ("*" if w.get("foreground") else " ",
                                  w["title"][:50], w["rect"]) for w in res["windows"]]
        return "\n".join(lines) if "windows" in res and len(res) <= 2 else _dump(res)
    return _dump(res)


def _dump(res: dict) -> str:
    keep = {k: v for k, v in res.items() if k != "raw"}
    return json.dumps(keep, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
