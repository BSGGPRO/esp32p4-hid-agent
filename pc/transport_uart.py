# -*- coding: utf-8 -*-
"""pc/transport_uart.py — ctypes 直调 Win32 串口（**不用 pyserial**）。

协议依据: docs/protocol.md v1.0
    §1  通道 UART0 / CH340C，115200 8N1 无流控，UTF-8 JSON 一行一帧（≤512B）
    §6.4 端口拔掉时不得抛未捕获异常
    §7  ping 往返 < 30 ms -> 读取必须低延迟且不丢字节

使用的 Win32 API（kernel32，全部通过 ctypes，无第三方包）:
    CreateFileW / GetCommState / SetCommState / SetCommTimeouts
    ReadFile / WriteFile / CloseHandle / ClearCommError / PurgeComm

读取策略:
    SetCommTimeouts 设为「立即返回」模式
        ReadIntervalTimeout = MAXDWORD, ReadTotalTimeoutMultiplier = MAXDWORD,
        ReadTotalTimeoutConstant = 0
    于是 ReadFile 有数据就返回、无数据立即返回 0；再由 Python 侧按 deadline
    轮询（1 ms 粒度）。这样 readline(timeout) 的超时语义是精确的，且不会
    因为驱动缓冲而阻塞。

端口枚举:
    注册表 HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM 取「设备路径 -> COM 名」，
    再扫 HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB 与 \\BTHENUM 的
    ``Device Parameters\\PortName`` / ``FriendlyName`` 做识别，
    从而把蓝牙虚拟口（COM4/COM5 那类）和真板子的 CH340 区分开。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from transport_base import (
    MAX_LINE_BYTES,
    LineBuffer,
    PortDisconnected,
    PortNotFound,
    Transport,
    TransportError,
)

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# Win32 常量
# --------------------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAXDWORD = 0xFFFFFFFF

NOPARITY = 0
ONESTOPBIT = 0

PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008

ERROR_OPERATION_ABORTED = 995
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_INVALID_HANDLE = 6
ERROR_ACCESS_DENIED = 5
ERROR_BAD_COMMAND = 22
ERROR_SEM_TIMEOUT = 121
ERROR_GEN_FAILURE = 31

#: 需要 Shift 的 DCB 位（见下方 DCB 注释）
_DCB_FLAGS = 0x00000001 | (0x01 << 4) | (0x01 << 12)  # fBinary | fDtrControl=ENABLE | fRtsControl=ENABLE


class DCB(ctypes.Structure):
    """Win32 ``DCB``（28 字节）。位域用 ``Flags`` 一个 DWORD 手工布置。

    位布局::

        bit0   fBinary = 1
        bit4-5 fDtrControl = 1 (DTR_CONTROL_ENABLE)
        bit12-13 fRtsControl = 1 (RTS_CONTROL_ENABLE)
        其余全 0：无校验、无流控（fOutxCtsFlow/fOutxDsrFlow/fOutX/fInX 均为 0）
    """

    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", ctypes.c_byte),
        ("Parity", ctypes.c_byte),
        ("StopBits", ctypes.c_byte),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


class COMSTAT(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("cbInQue", wintypes.DWORD),
        ("cbOutQue", wintypes.DWORD),
    ]


def _load_kernel32():
    if not IS_WINDOWS:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    k32.GetCommState.restype = wintypes.BOOL
    k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    k32.SetCommState.restype = wintypes.BOOL
    k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
    k32.SetCommTimeouts.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    k32.ReadFile.restype = wintypes.BOOL
    k32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    k32.WriteFile.restype = wintypes.BOOL
    k32.ClearCommError.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(COMSTAT)]
    k32.ClearCommError.restype = wintypes.BOOL
    k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.PurgeComm.restype = wintypes.BOOL
    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.FlushFileBuffers.restype = wintypes.BOOL
    return k32


_K32 = _load_kernel32()


# --------------------------------------------------------------------------
# 端口名规范化 / 注册表枚举
# --------------------------------------------------------------------------
def normalize_port_name(name: str) -> str:
    """``"COM7"`` / ``"com7"`` / ``"\\\\\\\\.\\\\COM7"`` -> ``"\\\\\\\\.\\\\COM7"``。

    Windows 上 COM10+ 必须带 ``\\\\.\\`` 前缀，这里统一加。
    """
    n = str(name).strip().strip('"')
    if not n:
        raise PortNotFound("串口名为空 / empty port name")
    if n.upper().startswith("\\\\.\\"):
        return n
    return "\\\\.\\" + n.upper() if n.upper().startswith("COM") else "\\\\.\\" + n


def _short_name(port: str) -> str:
    n = str(port).strip()
    if n.upper().startswith("\\\\.\\"):
        n = n[4:]
    return n.upper()


def _registry_value(root, path: str, value_name: str | None):
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            name = value_name if value_name is not None else ""
            data, _ = winreg.QueryValueEx(key, name)
            return data
    except OSError:
        return None


def _subkeys(root, path: str):
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            i = 0
            while True:
                try:
                    yield winreg.EnumKey(key, i)
                except OSError:
                    return
                i += 1
    except OSError:
        return


#: 已知 USB-UART 桥 / 板载 USB 的 VID:PID -> (说明, 类别)
KNOWN_VIDPID: dict[tuple[str, str], tuple[str, str]] = {
    ("1a86", "7523"): ("CH340/CH341 (QinHeng)", "uart_bridge"),
    ("1a86", "5523"): ("CH341 (QinHeng)", "uart_bridge"),
    ("1a86", "55d4"): ("CH9102/CH9102F (QinHeng)", "uart_bridge"),
    ("10c4", "ea60"): ("CP2102/CP2104 (Silicon Labs)", "uart_bridge"),
    ("10c4", "ea70"): ("CP2105 (Silicon Labs)", "uart_bridge"),
    ("0403", "6001"): ("FT232R (FTDI)", "uart_bridge"),
    ("0403", "6015"): ("FT231X (FTDI)", "uart_bridge"),
    ("2341", "0043"): ("Arduino Uno", "uart_bridge"),
    ("303a", "1001"): ("ESP32 USB-Serial-JTAG (原生 USB)", "native_usb"),
    ("303a", "4005"): ("JC-ESP32P4-M3 P4 HID Bridge (原生 USB HID，非串口)", "hid_only"),
}

#: 类别 -> 中文说明 / advice
_KIND_LABEL = {
    "uart_bridge": "USB-UART 桥 (可能是板子)",
    "native_usb": "ESP32 原生 USB 串口",
    "hid_only": "USB HID 设备（不是串口）",
    "unknown_usb": "USB 串口（未知型号）",
    "bluetooth": "蓝牙虚拟串口（不是板子）",
    "legacy": "传统/板载串口",
}


def _scan_enum_branch(branch: str) -> dict[str, dict]:
    """扫 ``Enum\\<branch>\\<device>\\<instance>\\Device Parameters\\PortName``。"""
    import winreg

    out: dict[str, dict] = {}
    base = r"SYSTEM\CurrentControlSet\Enum" + "\\" + branch
    for dev in _subkeys(winreg.HKEY_LOCAL_MACHINE, base):
        dev_path = base + "\\" + dev
        for inst in _subkeys(winreg.HKEY_LOCAL_MACHINE, dev_path):
            inst_path = dev_path + "\\" + inst
            port = _registry_value(winreg.HKEY_LOCAL_MACHINE, inst_path + r"\Device Parameters", "PortName")
            if not port:
                continue
            friendly = (
                _registry_value(winreg.HKEY_LOCAL_MACHINE, inst_path, "FriendlyName")
                or _registry_value(winreg.HKEY_LOCAL_MACHINE, inst_path, "DeviceDesc")
                or ""
            )
            vidpid = None
            if dev.upper().startswith("VID_"):
                parts = dev.split("&")
                if len(parts) >= 2 and parts[0].upper().startswith("VID_") and parts[1].upper().startswith("PID_"):
                    vidpid = (parts[0][4:].lower(), parts[1][4:].lower())
            out[_short_name(port)] = {
                "com": _short_name(port),
                "friendly": friendly,
                "vidpid": vidpid,
                "bus": branch,
                "instance_id": dev,
            }
    return out


def list_ports() -> list[dict]:
    """枚举本机串口并分类（纯读注册表，**不打开任何端口**，无硬件也能跑）。

    返回每项::

        {com, device, bus, vidpid, vidpid_text, friendly, kind, kind_label,
         is_bluetooth, likely_target, advice, rank}
    """
    result: list[dict] = []
    if not IS_WINDOWS:
        return result

    import winreg

    serialcomm: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                serialcomm.append((str(name), str(data)))
                i += 1
    except OSError:
        serialcomm = []

    usb_info = _scan_enum_branch("USB")
    bth_info = _scan_enum_branch("BTHENUM")

    for device_path, com in serialcomm:
        com = _short_name(com)
        dev_upper = device_path.upper()
        is_bt = dev_upper.startswith(r"\DEVICE\BTHMODEM") or com in bth_info
        info = usb_info.get(com, {})
        vidpid = info.get("vidpid")
        friendly = info.get("friendly") or (bth_info.get(com, {}) or {}).get("friendly") or ""

        if is_bt:
            kind = "bluetooth"
        elif vidpid and vidpid in KNOWN_VIDPID:
            kind = KNOWN_VIDPID[vidpid][1]
        elif vidpid:
            kind = "unknown_usb"
        elif dev_upper.startswith(r"\DEVICE\VCP") or dev_upper.startswith(r"\DEVICE\USBSER"):
            kind = "unknown_usb"
        else:
            kind = "legacy"

        if vidpid and vidpid in KNOWN_VIDPID:
            model = KNOWN_VIDPID[vidpid][0]
        elif kind == "bluetooth":
            model = "Bluetooth 虚拟串口"
        elif kind == "unknown_usb":
            model = "USB 串口（未知 VID:PID）"
        else:
            model = "板载/传统串口"

        # 目标排序：CH340 最优先（协议 §1 明确板子用 CH340C）
        rank = 9
        if not is_bt:
            if vidpid == ("1a86", "7523"):
                rank = 0
            elif vidpid in (("1a86", "55d4"), ("1a86", "5523")):
                rank = 1
            elif kind == "uart_bridge":
                rank = 2
            elif kind == "native_usb":
                rank = 3
            elif kind == "unknown_usb":
                rank = 5
            else:
                rank = 7

        advice = ""
        if is_bt:
            advice = "跳过：蓝牙虚拟口，不是开发板"
        elif rank == 0:
            advice = "★ 最可能是开发板（CH340C）"
        elif rank == 1:
            advice = "可能是开发板（QinHeng USB-UART）"
        elif rank == 2:
            advice = "可能是开发板（USB-UART 桥）"
        elif kind == "native_usb":
            advice = "ESP32 原生 USB 串口（也可作候选）"
        elif kind == "hid_only":
            advice = "不是串口：这是 HID 设备本身"
        else:
            advice = "不确定"

        result.append({
            "com": com,
            "device": device_path,
            "bus": info.get("bus") or ("BTHENUM" if is_bt else ""),
            "vidpid": vidpid,
            "vidpid_text": f"{vidpid[0].upper()}:{vidpid[1].upper()}" if vidpid else "-",
            "friendly": friendly,
            "kind": kind,
            "kind_label": _KIND_LABEL.get(kind, kind),
            "model": model,
            "is_bluetooth": is_bt,
            "likely_target": rank <= 2,
            "advice": advice,
            "rank": rank,
        })

    result.sort(key=lambda p: (p["rank"], p["com"]))
    return result


def detect_port(candidates: list[dict] | None = None) -> dict | None:
    """自动挑一个最像开发板的串口；挑不到返回 ``None``。"""
    ports = candidates if candidates is not None else list_ports()
    for p in ports:
        if p["likely_target"]:
            return p
    for p in ports:
        if p["rank"] <= 3:
            return p
    return None


def format_port_table(ports: list[dict], lang: str = "both") -> str:
    """把 ``list_ports()`` 结果排版成中英对照表格。"""
    if not ports:
        return (
            "未发现任何串口 / no serial port found.\n"
            "  * 请插好开发板并确认 CH340 驱动已安装（设备管理器应出现 USB-SERIAL CH340）。\n"
            "  * Plug the board in and make sure the CH340 driver is installed.\n"
            "  * 也可用 --port 显式指定，例如: --port COM7"
        )
    lines = []
    lines.append(f"{'COM':<7}{'型号/Model':<40}{'VID:PID':<12}{'建议/Advice':<34}总线/Bus")
    lines.append("-" * 110)
    for p in ports:
        model = p["model"]
        if p["friendly"]:
            model = f"{model}"
        lines.append(
            f"{p['com']:<7}{model[:38]:<40}{p['vidpid_text']:<12}{p['advice'][:32]:<34}{p['bus'] or '-'}"
        )
        if p["friendly"]:
            lines.append(f"{'':<7}  friendly: {p['friendly']}")
    lines.append("-" * 110)
    bt = [p["com"] for p in ports if p["is_bluetooth"]]
    if bt:
        lines.append(
            f"提示/Note: {', '.join(bt)} 是蓝牙虚拟串口，开发板不在这里 / Bluetooth virtual ports, not the board."
        )
    target = detect_port(ports)
    if target:
        lines.append(
            f"自动选择/Would auto-select: {target['com']}  ({target['advice']})"
        )
    else:
        lines.append("没有可自动识别的开发板串口 / no likely board port; 请用 --port 手动指定。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# UART 传输实现
# --------------------------------------------------------------------------
class UartTransport(Transport):
    """ctypes 直调 Win32 串口。115200 8N1 无流控（protocol.md §1）。"""

    name = "uart"
    implemented = True

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.5,
                 read_chunk: int = 1024, poll_interval: float = 0.001):
        super().__init__(timeout=timeout)
        if not IS_WINDOWS:
            raise TransportError("UartTransport 仅支持 Windows（本机为 %s）" % sys.platform)
        if _K32 is None:  # pragma: no cover
            raise TransportError("无法加载 kernel32.dll")
        self.port = _short_name(port)
        self.port_path = normalize_port_name(port)
        self.baud = int(baud)
        self.read_chunk = int(read_chunk)
        self.poll_interval = float(poll_interval)
        self._handle = None
        self._saw_data = False

    # -- open / close -------------------------------------------------------
    def open(self) -> None:
        if self._open:
            return
        if ctypes.sizeof(DCB) != 28:  # pragma: no cover
            raise TransportError(f"DCB 结构尺寸异常: {ctypes.sizeof(DCB)} != 28")
        handle = _K32.CreateFileW(
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
            raise PortNotFound(self._explain_open_error(err))
        self._handle = handle
        try:
            self._configure()
        except Exception:
            self.close()
            raise
        self._buffer.clear()
        self._open = True
        self._saw_data = False
        try:
            # 丢掉打开前残留在驱动缓冲里的上电乱码（协议 §1：乱码会静默丢弃，
            # 这里只是减少上层无效解析）
            _K32.PurgeComm(self._handle, PURGE_RXCLEAR | PURGE_TXCLEAR)
        except Exception:  # pragma: no cover
            pass

    def _explain_open_error(self, err: int) -> str:
        hints = {
            ERROR_FILE_NOT_FOUND: "端口不存在（COM 号错？板子没插？）",
            ERROR_PATH_NOT_FOUND: "端口路径不存在",
            ERROR_ACCESS_DENIED: "端口被占用（串口监视器/其他程序打开着？）",
            ERROR_BAD_COMMAND: "设备未就绪",
        }
        hint = hints.get(err, "未知错误")
        return (
            f"无法打开串口 {self.port} ({self.port_path}): WinError {err} - {hint}\n"
            f"  * 检查是否插板、COM 号是否为 {self.port}\n"
            f"  * 可用 --list-ports 列出本机串口并识别板子\n"
            f"  * 若串口被其他程序占用请先关闭它"
        )

    def _configure(self) -> None:
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not _K32.GetCommState(self._handle, ctypes.byref(dcb)):
            raise TransportError(f"GetCommState 失败 WinError {ctypes.get_last_error()}")
        dcb.BaudRate = self.baud
        dcb.ByteSize = 8
        dcb.Parity = NOPARITY
        dcb.StopBits = ONESTOPBIT
        dcb.Flags = _DCB_FLAGS
        dcb.XonLim = 2048
        dcb.XoffLim = 512
        if not _K32.SetCommState(self._handle, ctypes.byref(dcb)):
            raise TransportError(f"SetCommState 失败 WinError {ctypes.get_last_error()}")

        timeouts = COMMTIMEOUTS()
        timeouts.ReadIntervalTimeout = MAXDWORD          # 立即返回模式
        timeouts.ReadTotalTimeoutMultiplier = MAXDWORD
        timeouts.ReadTotalTimeoutConstant = 0
        timeouts.WriteTotalTimeoutMultiplier = 0
        timeouts.WriteTotalTimeoutConstant = 3000
        if not _K32.SetCommTimeouts(self._handle, ctypes.byref(timeouts)):
            raise TransportError(f"SetCommTimeouts 失败 WinError {ctypes.get_last_error()}")

    def close(self) -> None:
        """幂等关闭；绝不抛异常（§6.4）。"""
        handle, self._handle = self._handle, None
        self._open = False
        if handle and handle != INVALID_HANDLE_VALUE:
            try:
                _K32.CloseHandle(handle)
            except Exception:  # pragma: no cover
                pass

    # -- I/O ---------------------------------------------------------------
    def write(self, data: bytes) -> int:
        if not self._open or not self._handle:
            raise PortDisconnected(f"串口 {self.port} 未打开，无法写入")
        view = memoryview(bytes(data))
        total = 0
        while total < len(view):
            chunk = view[total:]
            written = wintypes.DWORD(0)
            ok = _K32.WriteFile(self._handle, bytes(chunk), len(chunk), ctypes.byref(written), None)
            if not ok:
                err = ctypes.get_last_error()
                self.stats["io_errors"] += 1
                raise PortDisconnected(
                    f"写串口 {self.port} 失败 WinError {err}（设备被拔出？）/ write failed"
                )
            if written.value == 0:
                self.stats["io_errors"] += 1
                raise PortDisconnected(f"写串口 {self.port} 返回 0 字节 / zero-length write")
            total += written.value
        self.stats["bytes_tx"] += total
        return total

    def _pump(self) -> int:
        """读一次硬件缓冲（立即返回），把字节喂给行缓冲，返回读到的字节数。"""
        if not self._open or not self._handle:
            return 0
        buf = ctypes.create_string_buffer(self.read_chunk)
        got = wintypes.DWORD(0)
        ok = _K32.ReadFile(self._handle, buf, self.read_chunk, ctypes.byref(got), None)
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_OPERATION_ABORTED, ERROR_SEM_TIMEOUT):
                return 0
            self.stats["io_errors"] += 1
            if err in (ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE, ERROR_BAD_COMMAND,
                       ERROR_FILE_NOT_FOUND, ERROR_GEN_FAILURE):
                raise PortDisconnected(
                    f"读串口 {self.port} 失败 WinError {err}（设备被拔出？）/ device disconnected"
                )
            raise TransportError(f"读串口 {self.port} 失败 WinError {err}")
        n = got.value
        if n:
            data = buf.raw[:n]
            self.stats["bytes_rx"] += n
            self._buffer.feed(data)
            self.stats["lines_rx"] = self._buffer.lines
            self.stats["overflow"] = self._buffer.overflow
            self._saw_data = True
        return n

    def readline(self, timeout: float | None = None) -> str | None:
        """读一行；``timeout`` 秒内没有完整行则返回 ``None``。

        已缓冲的完整行会在读取前先返回（不受 timeout 影响）。
        """
        line = self._buffer.next_line()
        if line is not None:
            return line
        limit = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            self._pump()
            line = self._buffer.next_line()
            if line is not None:
                return line
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval)

    def readline_nonblocking(self) -> str | None:
        self._pump()
        return self._buffer.next_line()

    def drain(self, timeout: float | None = None) -> bool:
        """§6.4：等 TX 真正发完再关句柄，避免丢掉最后一条 release_all。"""
        if not self._open or not self._handle:
            return False
        try:
            return bool(_K32.FlushFileBuffers(self._handle))
        except Exception:  # pragma: no cover
            return False

    def bytes_in_queue(self) -> int:
        if not self._open or not self._handle:
            return 0
        stat = COMSTAT()
        errors = wintypes.DWORD(0)
        if not _K32.ClearCommError(self._handle, ctypes.byref(errors), ctypes.byref(stat)):
            return 0
        return int(stat.cbInQue)

    def describe(self) -> dict:
        d = super().describe()
        d.update({"port": self.port, "port_path": self.port_path, "baud": self.baud,
                  "framing": "8N1", "flow": "none"})
        return d


def open_uart(port: str, baud: int = 115200, timeout: float = 1.5) -> UartTransport:
    t = UartTransport(port=port, baud=baud, timeout=timeout)
    t.open()
    return t


__all__ = [
    "UartTransport", "list_ports", "detect_port", "format_port_table",
    "normalize_port_name", "open_uart", "DCB", "COMMTIMEOUTS", "KNOWN_VIDPID",
    "MAX_LINE_BYTES",
]