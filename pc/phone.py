# -*- coding: utf-8 -*-
"""pc/phone.py — 把这块 P4 板子当「真 USB 鼠标」去操作手机（iPhone / 安卓）。

移植自 **ESP32 iPhone Agent by Henry Yuan**
(https://github.com/onehumanbeing/esp32-iphone-agent) 的 ``server/hidagent/{phone,vision}.py``
（MIT，见仓库根 NOTICE）。原版用 BLE 驱动 ESP32-S3；这里换成本机 P4 板子的
JSON-lines 协议（``docs/protocol.md`` §3.3 鼠标 / §3.2 键盘），**固件一行不改**。

接线（两个独立物理口，互不干扰）::

    iPhone  ←── USB-C（板子原生 USB 口，枚举成 HID 键鼠）
    PC      ←── CH340（板子串口，JSON 命令通道）

手机侧要打开「辅助触控 / AssistiveTouch」，屏幕上才会出现指针。
iPhone 15+ 直接插；Lightning 机型要原装/ MFi 的 Lightning 转 USB 相机转接头。

原理（相对指针）:
    手机只认「相对移动」，没有绝对坐标，也读不回指针位置。于是四件事：

    1. 标定 —— 归零后走已知步数，看指针落在屏幕哪里，算出跨整个屏幕宽/高
       需要多少鼠标单位（``span``）；
    2. 记账 —— 每次 ``move`` 后本地累加 ``pos``，避免每步都归零；
    3. 归零 —— 每 ``REHOME_EVERY`` 个手势大力甩到左上角，清掉累积漂移；
    4. 纠偏 —— 精确点击前微抖一下、在两帧截图里找指针，算误差补一下，
       并把误差记进线性拟合——越用越准。

坐标一律用屏幕比例 ``0..1``（原点左上），与镜像画面大小无关。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import time
from collections import deque

from PIL import Image, ImageChops, ImageGrab

#: 归零前要走的鼠标单位（远大于任何手机屏幕的 span）
HOME_UNITS = 4000
#: 手工单点标定走的对角步数（留在屏幕内，不会撞边）
CALIBRATION_MOVE = 200
#: 自动标定在两个已知距离上找指针
CALIBRATION_STOPS = (150, 350)
#: 每几个手势归零一次
REHOME_EVERY = 10
#: 手势结束前停一下，iOS 就不会把「拖」变成「甩」
HOLD = 0.25
#: 点击后停一下，让 iOS 认定这是一次完整轻点
TAP_GAP = 0.15
#: 微抖距离（找指针用）
NUDGE = 12
#: 允许的瞄准误差（屏幕比例）；超过就当场补一下
AIM_TOLERANCE = 0.008
#: 更大的误差仍然纠正，但不拿来学习（可能是误读）
MAX_MISS = 0.06
#: 镜像画面常见窗口标题（AirPlay 接收器 / 投屏软件的窗口）
MIRROR_TITLES = (
    "airserver", "lonelyscreen", "uxplay", "5kplayer", "reflector",
    "apowermirror", "airdroid", "airplay", "mirror", "投屏", "镜像",
)


class PhoneError(RuntimeError):
    """手机操作失败（标定缺失、找不到镜像窗口等）。"""


class ScreenError(PhoneError):
    """看不到手机屏幕（没有镜像窗口 / 没配 rect）。"""


# --------------------------------------------------------------------------- 窗口与截屏

def _win_rects() -> list[dict]:
    """可见顶层窗口：``[{"hwnd","title","rect","client"}]``，坐标是物理像素。

    ``rect`` 含标题栏边框（GetWindowRect）；``client`` 是客户区（视频真正显示的地方）。
    """
    try:
        u32 = ctypes.windll.user32
        u32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 — 非 Windows 或已设置
        return []
    out: list[dict] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _):
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            r = ctypes.wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left <= 120 or r.bottom - r.top <= 120:
                return True
            cr = ctypes.wintypes.RECT()
            u32.GetClientRect(hwnd, ctypes.byref(cr))
            pt = ctypes.wintypes.POINT(0, 0)
            u32.ClientToScreen(hwnd, ctypes.byref(pt))
            client = (pt.x, pt.y, pt.x + (cr.right - cr.left), pt.y + (cr.bottom - cr.top))
            out.append({"hwnd": int(hwnd), "title": buf.value,
                        "rect": (r.left, r.top, r.right, r.bottom), "client": client})
        except Exception:  # noqa: BLE001
            pass
        return True

    u32.EnumWindows(proc(_cb), 0)
    return out


def _fit_on_screen(hwnd: int, win: dict) -> dict:
    """把**我们自己拉起的**接收器窗口挪成完整可见（不碰别的窗口）。

    沿用仓库既有约定：只在「窗口是我们自己打开的时候」才动它（同 tools/draw-rage.py
    对画图窗口的处理）。别人的镜像窗口一律不动，只报错让人自己挪。
    """
    u32 = ctypes.windll.user32
    vs = (u32.GetSystemMetrics(76), u32.GetSystemMetrics(77),
          u32.GetSystemMetrics(76) + u32.GetSystemMetrics(78),
          u32.GetSystemMetrics(77) + u32.GetSystemMetrics(79))
    x1, y1, x2, y2 = win["client"]
    if x1 >= vs[0] and y1 >= vs[1] and x2 <= vs[2] and y2 <= vs[3]:
        return win
    if win["title"].strip().lower() != "airplay-windows":
        raise ScreenError(
            "镜像窗口“%s”超出屏幕（%s，屏幕 %s）。请把它拖成完整可见，或用 --rect 指定区域。"
            % (win["title"], list(win["client"]), list(vs)))
    w, h = x2 - x1, y2 - y1
    w, h = min(w, vs[2] - vs[0] - 20), min(h, vs[3] - vs[1] - 60)
    x, y = vs[0] + 10, vs[1] + 40
    u32.SetWindowPos(int(hwnd), 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)  # NOZORDER|NOACTIVATE
    # SDL 改窗口尺寸时 HWND 会重建，会有一小段「窗口不在」的间隙，所以轮询等它回来
    for _ in range(10):
        time.sleep(0.3)
        for w2 in _win_rects():
            if w2["title"].strip().lower() == win["title"].strip().lower():
                return {"title": w2["title"], "rect": w2["client"], "client": w2["client"],
                        "hwnd": w2["hwnd"]}
    return win


def find_mirror_window(titles=MIRROR_TITLES) -> dict | None:
    """按标题找手机镜像窗口（优先纯视频的 clean feed 窗口，其次面积最大的）。

    返回 ``{"title","rect","hwnd"}``；``rect`` 是**客户区** ``(x1,y1,x2,y2)`` 物理像素。
    """
    hits = [w for w in _win_rects() if any(t in w["title"].lower() for t in titles)]
    if not hits:
        return None
    clean = [w for w in hits if "clean feed" in w["title"].lower()]
    pool = clean or hits
    best = max(pool, key=lambda w: (w["client"][2] - w["client"][0]) * (w["client"][3] - w["client"][1]))
    area = (best["client"][2] - best["client"][0]) * (best["client"][3] - best["client"][1])
    if area < 200 * 200:
        return None
    return {"title": best["title"], "rect": best["client"], "client": best["client"],
            "hwnd": best["hwnd"]}


def trim_letterbox(img: Image.Image, thr: int = 20, need: float = 0.98) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """裁掉四周的纯黑边（竖屏手机塞进横窗口时的黑条）。

    从四条边往里收，直到某一行/列不再是 ``need`` 比例的近黑像素为止。
    裁出来不足原图 30% 就判定为「不是黑边」（比如屏幕本来就是黑的），原图返回。
    返回 ``(图, (dx1,dy1,dx2,dy2))``，偏移是相对原图的像素。
    """
    g = img.convert("L")
    w, h = g.size
    px = g.load()

    def row_black(y: int) -> bool:
        n = sum(1 for x in range(0, w, 3) if px[x, y] <= thr)
        return n >= need * len(range(0, w, 3))

    def col_black(x: int) -> bool:
        n = sum(1 for y in range(0, h, 3) if px[x, y] <= thr)
        return n >= need * len(range(0, h, 3))

    t = 0
    while t < h - 1 and row_black(t):
        t += 1
    b = h - 1
    while b > t and row_black(b):
        b -= 1
    l = 0
    while l < w - 1 and col_black(l):
        l += 1
    r = w - 1
    while r > l and col_black(r):
        r -= 1
    if (r - l + 1) * (b - t + 1) < 0.3 * w * h:
        return img, (0, 0, w, h)
    return img.crop((l, t, r + 1, b + 1)), (l, t, r + 1, b + 1)


class MirrorScreen:
    """手机镜像画面的取帧器。

    三种来源，按优先级：显式 ``rect`` → 按标题找窗口 → 报错（不瞎猜整屏）。
    """

    def __init__(self, rect=None, titles=MIRROR_TITLES, latency: float = 0.35):
        self._rect = tuple(rect) if rect else None
        self.titles = titles
        self.latency = float(latency)
        self.info: dict = {}
        self.last_box: tuple[int, int, int, int] | None = None
        self.trimmed = False

    # -- 定位 ---------------------------------------------------------------
    def rect(self) -> tuple[int, int, int, int]:
        if self._rect:
            return self._rect
        win = None
        for _ in range(8):  # 窗口刚被挪过/正在重建时会有短暂间隙
            win = find_mirror_window(self.titles)
            if win:
                break
            time.sleep(0.25)
        if not win:
            raise ScreenError(
                "找不到手机镜像窗口（标题里要有 %s 之一）。"
                "先在 PC 上开 AirPlay 接收器并把手机投上去，或用 --rect x1,y1,x2,y2 指定区域。"
                % "/".join(self.titles[:4]))
        win = _fit_on_screen(int(win["hwnd"]), win)
        self.info = win
        self._rect = win["rect"]
        return self._rect

    # -- 取帧 ---------------------------------------------------------------
    def frame(self) -> Image.Image:
        """截一帧手机画面（RGB），已裁掉黑边。"""
        try:
            import eyes

            eyes.enable_dpi_awareness()
        except Exception:  # noqa: BLE001 — 只是尽力
            pass
        x1, y1, x2, y2 = self.rect()
        raw = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("RGB")
        img, (dx1, dy1, dx2, dy2) = trim_letterbox(raw)
        self.last_box = (x1 + dx1, y1 + dy1, x1 + dx2, y1 + dy2)
        self.trimmed = (img.size[0] != raw.size[0] or img.size[1] != raw.size[1])
        return img

    def wait(self, extra: float = 0.0) -> None:
        """等镜像延迟（AirPlay 有 0.3~1 s 的缓冲）。"""
        time.sleep(self.latency + max(0.0, extra))

    def fresh(self, since: float | None = None) -> Image.Image:
        """等过延迟后取一帧。``since`` 给了就至少等到那个时间点之后。"""
        if since is not None:
            time.sleep(max(0.0, since + self.latency - time.time()))
        else:
            self.wait()
        return self.frame()


# --------------------------------------------------------------------------- 指针识别

def _gray(img: Image.Image) -> Image.Image:
    return img.convert("L")


def changed(before, after, ignore=(), threshold: float = 1.0) -> bool:
    """画面有没有变（含勾选这类小变化）。``ignore`` 里的点（比例）会被掩掉，
    这样找指针时的微抖不会被当成画面变化。"""
    if before is None or after is None:
        return True
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return True
    small = ImageChops.difference(a.resize((32, 64), Image.BILINEAR),
                                  b.resize((32, 64), Image.BILINEAR)).tobytes()
    if sum(small) / len(small) > threshold:
        return True
    w, h = a.size[0] // 2, a.size[1] // 2
    diff = ImageChops.difference(a.resize((w, h), Image.BILINEAR),
                                 b.resize((w, h), Image.BILINEAR)).tobytes()
    masks = [(px * w, py * h) for px, py in ignore]
    hits = 0
    for i, v in enumerate(diff):
        if v > 25:
            x, y = i % w, i // w
            if y > 0.06 * h and all((x - mx) ** 2 + (y - my) ** 2 > (0.05 * w) ** 2 for mx, my in masks):
                hits += 1
    return hits >= 6


def pointer(before: Image.Image, after: Image.Image, near: tuple[float, float], shift: float,
            reach: tuple[float, float] = (0.12, 0.08)) -> tuple[float, float] | None:
    """``before`` 里指针在哪：它在 ``after`` 里向右移了 ``shift``（屏幕比例）。

    两帧之间只有指针动了，所以 ``near`` 附近的差异像素就是指针两次的位置；
    质心比第一次位置偏右 ``shift/2``。看不清（没动 / 变化太大 / 不集中）返回 ``None``。
    """
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return None
    w, h = a.size
    rx, ry = reach
    left = max(0, int((near[0] - rx) * w))
    top = max(int(0.05 * h), int((near[1] - ry) * h))      # 跳过状态栏
    box = (left, top, min(w, int((near[0] + rx) * w)), min(h, int((near[1] + ry) * h)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    diff = ImageChops.difference(a.crop(box), b.crop(box)).tobytes()
    bw = box[2] - box[0]
    hits = [(i % bw, i // bw) for i, v in enumerate(diff) if v > 30]
    if not 15 <= len(hits) <= len(diff) // 4:                # 没动，或者动静远大于一个指针
        return None
    xs, ys = [x for x, _ in hits], [y for _, y in hits]
    if max(xs) - min(xs) > 0.15 * w or max(ys) - min(ys) > 0.07 * h:   # 不是一小团
        return None
    cx = sum(xs) / len(hits) + left
    cy = sum(ys) / len(hits) + top
    return cx / w - shift / 2, cy / h


# --------------------------------------------------------------------------- 手势引擎

def default_calibration_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "esp32p4-phone", "calibration.json")


class Phone:
    """手机手势引擎。``bridge`` 只需有 ``mouse_move`` / ``mouse_btn`` /
    ``mouse_scroll`` / ``text`` / ``key_combo`` / ``release_all`` / ``command`` 这些方法
    （``agent_bridge.AgentBridge`` 就是）。"""

    def __init__(self, bridge, screen: MirrorScreen | None = None,
                 calib_file: str | None = None):
        self.bridge = bridge
        self.screen = screen
        self._file = calib_file or default_calibration_path()
        saved = None
        if os.path.exists(self._file):
            try:
                saved = json.load(open(self._file, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                saved = None
        self.span = {"w": saved["w"], "h": saved["h"]} if saved and "w" in saved else None
        self._pos = None          # 指针在鼠标单位里的位置；None = 不知道
        self._moves = 0
        self._link = 0            # 换了链路就说明可能残留按键按下
        self._bridge_link = 0
        self._seen = {axis: deque(map(tuple, (saved or {}).get("seen", {}).get(axis, [])), maxlen=12)
                      for axis in ("x", "y")}

    # -- 状态 ---------------------------------------------------------------
    @property
    def calibrated(self) -> bool:
        return self.span is not None

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump({**self.span, "seen": {k: list(v) for k, v in self._seen.items()}}, f)

    def set_link(self, link: int) -> None:
        """告诉引擎「板子重连了」，下一次手势先释放按键再归零。"""
        self._link = int(link)

    # -- 原语 ---------------------------------------------------------------
    def _send_move(self, dx: int, dy: int) -> None:
        if dx or dy:
            self.bridge.mouse_move(int(dx), int(dy))

    def home(self) -> None:
        """大力甩到左上角（手机侧会夹住），指针位置归零。"""
        for _ in range(2):
            self._send_move(-HOME_UNITS, -HOME_UNITS)
        self._pos, self._moves = (0, 0), 0

    def press(self) -> None:
        self.bridge.mouse_btn("left", "down")

    def release(self) -> None:
        self.bridge.mouse_btn("left", "up")

    def click(self) -> None:
        self.bridge.mouse_btn("left", "click")

    def scroll(self, lines: int) -> None:
        self.bridge.mouse_scroll(int(lines))

    def type(self, text: str) -> None:
        self.bridge.text(text)

    def key(self, combo) -> None:
        from keymap import validate_keys

        keys = combo if isinstance(combo, (list, tuple)) else str(combo).replace(" ", "+").split("+")
        self.bridge.key_combo(validate_keys([k for k in keys if k]))

    # -- 标定 ---------------------------------------------------------------
    def start_calibration(self) -> bool:
        """自动标定：归零后走两个已知步数，看指针落点，拟合 span（比例 + 偏移）。

        成功返回 True；找不到指针返回 False（此时可让用户手点一下，再 ``finish_calibration``）。
        """
        if self.screen is None:
            raise ScreenError("自动标定需要能看到手机屏幕（MirrorScreen）")
        self._pos = None
        self.home()
        seen: list[tuple[int, float, float]] = []
        for units in CALIBRATION_STOPS:
            step = units - (seen[-1][0] if seen else 0)
            self._send_move(step, step)
            found = self._find_pointer()
            if not found:
                break
            seen.append((units, *found))
        if len(seen) < 2:
            self._pos = None
            self.home()
            self._send_move(CALIBRATION_MOVE, CALIBRATION_MOVE)
            return False
        (u1, x1, y1), (u2, x2, y2) = seen
        if abs(x2 - x1) < 1e-3 or abs(y2 - y1) < 1e-3:
            return False
        self.span = {"w": round((u2 - u1) / (x2 - x1)), "h": round((u2 - u1) / (y2 - y1))}
        for samples in self._seen.values():
            samples.clear()
        for units, x, y in seen:
            self._seen["x"].append((units / self.span["w"], x))
            self._seen["y"].append((units / self.span["h"], y))
        self._save()
        self._pos = None
        self.home()
        return True

    def finish_calibration(self, x: float, y: float) -> None:
        """手工单点标定：``x, y`` 是 ``start_calibration()`` 之后指针停在屏幕上的位置（比例）。"""
        if not (0.001 < x < 0.999 and 0.001 < y < 0.999):
            raise PhoneError("指针位置要在屏幕内（0..1），收到 %r,%r" % (x, y))
        self.span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        for samples in self._seen.values():
            samples.clear()
        self._save()
        self._pos = None
        self.home()

    def _find_pointer(self) -> tuple[float, float] | None:
        """微抖一下，在整屏范围找指针（span 还不知道时也能用）。"""
        assert self.screen is not None
        self.screen.wait()
        before = self.screen.frame()
        sent = time.time()
        self._send_move(NUDGE, 0)
        after = self.screen.fresh(sent)
        self._send_move(-NUDGE, 0)
        found = pointer(before, after, (0.5, 0.5), 0.025, reach=(0.5, 0.5))
        if found and not (0.03 < found[0] < 0.97):
            found = None
        return found

    # -- 移动 ---------------------------------------------------------------
    def _dx(self, x: float) -> int:
        return round(min(max(self._fix(x, "x"), 0.0), 1.0) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(self._fix(y, "y"), 0.0), 1.0) * self.span["h"])

    def _fix(self, t: float, axis: str) -> float:
        """要瞄准的屏幕比例：过观测落点的直线反解（抵消比例误差与固定偏移）。"""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc = sum(c for c, _ in pts) / n
        mf = sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._link != getattr(self, "_bridge_link", 0):
            # 新链路：上一次手势可能留下按下的键/按钮
            self.bridge.release_all()
            self._pos = None
            self._bridge_link = self._link
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            self.home()
        x, y = self._pos or (0, 0)
        self._pos = None                       # 移动成功前位置未知
        self._send_move(tx - x, ty - y)
        self._pos, self._moves = (tx, ty), self._moves + 1

    def point(self, x: float, y: float, aim: bool = False) -> None:
        if not self.calibrated:
            raise PhoneError("指针还没标定：先跑 iphone.py calibrate")
        self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            self._aim(x, y)

    def _aim(self, x: float, y: float) -> None:
        """闭环：看指针真实落点，补掉误差，并把误差记进拟合。"""
        assert self.screen is not None
        nudge = NUDGE if x < 0.9 else -NUDGE
        self.screen.wait()
        before = self.screen.frame()
        sent = time.time()
        self._send_move(nudge, 0)
        after = self.screen.fresh(sent)
        self._send_move(-nudge, 0)
        shift = nudge / self.span["w"]
        found = (pointer(before, after, (x, y), shift) or
                 pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))
        if not found:
            return
        fx, fy = found
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])
        self._send_move(dx, dy)
        self._pos = (self._pos[0] + dx, self._pos[1] + dy) if self._pos else None

    # -- 手势 ---------------------------------------------------------------
    def reset(self) -> None:
        """松手并归零，不管上一次手势留在什么状态。"""
        self._pos = None
        self.bridge.release_all()
        self.home()

    def tap(self, x: float, y: float, aim: bool = False) -> None:
        self.point(x, y, aim)
        self.click()
        time.sleep(TAP_GAP)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True) -> None:
        self.drag([(x1, y1), (x2, y2)], hold=hold)

    def drag(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        """按下 → 途经各点 → 抬起，一整笔；``hold`` 末尾先停一下，列表/滚轮才不会甩过头。"""
        if len(points) < 2:
            raise PhoneError("drag 至少两个点")
        self.point(*points[0])
        self.press()
        try:
            for x, y in points[1:]:
                self._move_to(self._dx(x), self._dy(y), rehome=False)   # 拖拽中途绝不归零
            if hold:
                time.sleep(HOLD)
        finally:
            self.release()

    def go_home(self) -> None:
        """底部上滑回主屏（末尾不能停，停了会开多任务）。"""
        self.swipe(0.5, 0.995, 0.5, 0.6, hold=False)

    def go_back(self) -> None:
        """左缘右滑返回。"""
        self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)
