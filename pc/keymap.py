# -*- coding: utf-8 -*-
"""pc/keymap.py — protocol.md §4 键名表（唯一权威，两侧各实现一份）。

本模块把 protocol.md §4 逐字翻译成 Python 表，供两处使用：

1. ``agent_bridge`` 的 CLI 本地预校验 —— 用户敲错键名时立刻给友好报错，
   不必等固件回 ``bad_key``（发出去的报文一律是 §4 规范名）。
2. ``mock_p4`` 计算 HID 状态（modifier 字节 + 6 键槽位）。

§4 摘录 / excerpt
    修饰键（折叠进 modifier 字节）:
        leftctrl 0x01  leftshift 0x02  leftalt 0x04  leftgui 0x08
        rightctrl 0x10 rightshift 0x20 rightalt 0x40 rightgui 0x80
        别名 win/cmd/meta/super -> leftgui
    普通键（HID Usage ID, Keyboard/Keypad page 0x07）: 见下方 _build_keys()
    text 的 Shift 规则（US 布局）:
        A-Z -> shift + 小写键
        !@#$%^&*()_+{}|:"<>?~ -> shift + 对应基础键
"""

from __future__ import annotations

#: §3.2 非修饰键最多 6 个槽位
MAX_KEY_SLOTS = 6

#: §4 修饰键 -> modifier bit
MODIFIER_BITS: dict[str, int] = {
    "leftctrl": 0x01,
    "leftshift": 0x02,
    "leftalt": 0x04,
    "leftgui": 0x08,
    "rightctrl": 0x10,
    "rightshift": 0x20,
    "rightalt": 0x40,
    "rightgui": 0x80,
}

#: §4 别名统一: win / cmd / meta / super -> leftgui
MODIFIER_ALIASES: dict[str, str] = {
    "win": "leftgui",
    "cmd": "leftgui",
    "meta": "leftgui",
    "super": "leftgui",
}

#: §4 普通键里带符号别名的键: 符号 -> 规范名
SYMBOL_ALIASES: dict[str, str] = {
    "-": "minus",
    "=": "equal",
    "[": "leftbracket",
    "]": "rightbracket",
    "\\": "backslash",
    ";": "semicolon",
    "'": "quote",
    "`": "grave",
    ",": "comma",
    ".": "period",
    "/": "slash",
}


def _build_keys() -> dict[str, int]:
    """构造 §4「普通键」表（规范名 -> HID Usage ID）。"""
    keys: dict[str, int] = {}

    # a..z = 0x04-0x1D
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        keys[ch] = 0x04 + i
    # 1..9 = 0x1E-0x26, 0 = 0x27
    for i, ch in enumerate("123456789"):
        keys[ch] = 0x1E + i
    keys["0"] = 0x27

    keys.update({
        "enter": 0x28,
        "esc": 0x29,
        "backspace": 0x2A,
        "tab": 0x2B,
        "space": 0x2C,
        "minus": 0x2D,
        "equal": 0x2E,
        "leftbracket": 0x2F,
        "rightbracket": 0x30,
        "backslash": 0x31,
        "semicolon": 0x33,
        "quote": 0x34,
        "grave": 0x35,
        "comma": 0x36,
        "period": 0x37,
        "slash": 0x38,
        "capslock": 0x39,
        # f1..f12 = 0x3A-0x45
        "printscreen": 0x46,
        "scrolllock": 0x47,
        "pause": 0x48,
        "insert": 0x49,
        "home": 0x4A,
        "pageup": 0x4B,
        "delete": 0x4C,
        "end": 0x4D,
        "pagedown": 0x4E,
        "right": 0x4F,
        "left": 0x50,
        "down": 0x51,
        "up": 0x52,
        "numlock": 0x53,
        "kp_slash": 0x54,
        "kp_asterisk": 0x55,
        "kp_minus": 0x56,
        "kp_plus": 0x57,
        "kp_enter": 0x58,
        # kp_1..kp_9 = 0x59-0x61, kp_0 = 0x62
        "kp_period": 0x63,
        "application": 0x65,
        "power": 0x66,
        "kp_equal": 0x67,
        # f13..f24 = 0x68-0x73
    })
    for i in range(12):
        keys[f"f{i + 1}"] = 0x3A + i
    for i in range(9):
        keys[f"kp_{i + 1}"] = 0x59 + i
    keys["kp_0"] = 0x62
    for i in range(12):
        keys[f"f{i + 13}"] = 0x68 + i

    # 数值自检（防止打表手滑）
    assert keys["a"] == 0x04 and keys["z"] == 0x1D
    assert keys["1"] == 0x1E and keys["9"] == 0x26 and keys["0"] == 0x27
    assert keys["f1"] == 0x3A and keys["f12"] == 0x45 and keys["f13"] == 0x68 and keys["f24"] == 0x73
    assert keys["kp_1"] == 0x59 and keys["kp_9"] == 0x61 and keys["kp_0"] == 0x62
    return keys


#: §4 普通键表（规范名 -> HID Usage ID）
KEYS: dict[str, int] = _build_keys()

#: 本次实现依据的协议版本
PROTO_VERSION = "1.0"


class KeymapError(ValueError):
    """键名/参数本地校验失败。"""


class BadKeyError(KeymapError):
    """键名不在 §4 表内（对应协议 err ``bad_key``）。"""

    def __init__(self, name: str):
        super().__init__(f"bad_key: {name!r} 不在 protocol.md §4 键名表内")
        self.name = name
        self.err = "bad_key"


class BadArgError(KeymapError):
    """参数非法（对应协议 err ``bad_arg``）。"""

    def __init__(self, message: str):
        super().__init__(f"bad_arg: {message}")
        self.err = "bad_arg"


class UnsupportedCharError(KeymapError):
    """text 含非 ASCII 可打印字符（对应协议 err ``unsupported_char``）。

    ``index`` 为 **0-based 字符索引**（与 mock/firmware 约定一致）。
    """

    def __init__(self, index: int, ch: str):
        super().__init__(
            f"unsupported_char: 位置 {index} 的字符 {ch!r} (U+{ord(ch):04X}) 不在 ASCII 0x20-0x7E 内"
        )
        self.index = index
        self.ch = ch
        self.err = "unsupported_char"


def _lookup_name(name: str) -> str:
    """返回规范名；不在表内返回 ""。"""
    if not isinstance(name, str):
        raise BadKeyError(repr(name))
    n = name.strip().lower()
    if n in MODIFIER_BITS:
        return n
    if n in MODIFIER_ALIASES:
        return MODIFIER_ALIASES[n]
    if n in KEYS:
        return n
    if n in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[n]
    return ""


def normalize_key(name: str) -> str:
    """键名 -> §4 规范名（大小写不敏感、别名折叠）。未知键名抛 BadKeyError。"""
    canon = _lookup_name(name)
    if not canon:
        raise BadKeyError(name)
    return canon


def canonical_name(name: str) -> str:
    return normalize_key(name)


def is_valid_key(name: str) -> bool:
    try:
        return bool(_lookup_name(name))
    except BadKeyError:
        return False


def is_modifier(name: str) -> bool:
    return _lookup_name(name) in MODIFIER_BITS


def modifier_bit(name: str) -> int:
    return MODIFIER_BITS[normalize_key(name)]


def usage_id(name: str) -> int:
    canon = normalize_key(name)
    if canon in MODIFIER_BITS:
        raise BadArgError(f"{canon!r} 是修饰键，没有 HID usage id（折叠进 modifier 字节）")
    return KEYS[canon]


def split_keys(names) -> tuple[int, list[int], list[str]]:
    """把键名数组拆成 (modifier_bits, [usage_id...], [规范名...])。

    - 未知键名 -> BadKeyError（第一个非法键名）
    - 非修饰键 > 6 个 -> BadArgError（§3.2 键槽溢出）
    """
    if isinstance(names, str):
        names = [names]
    if not names:
        raise BadArgError("keys 不能为空")
    modifier = 0
    usages: list[int] = []
    canon_names: list[str] = []
    for name in names:
        canon = normalize_key(name)  # raises BadKeyError
        canon_names.append(canon)
        if canon in MODIFIER_BITS:
            modifier |= MODIFIER_BITS[canon]
        else:
            usages.append(KEYS[canon])
    if len(usages) > MAX_KEY_SLOTS:
        raise BadArgError(f"非修饰键最多 {MAX_KEY_SLOTS} 个，收到 {len(usages)} 个（键槽溢出）")
    return modifier, usages, canon_names


def validate_keys(names) -> list[str]:
    """CLI 预校验；返回规范名列表。"""
    return split_keys(names)[2]


def check_key_or_fail(name: str) -> str:
    return normalize_key(name)


# --------------------------------------------------------------------------
# §4 text 的 Shift 规则（US 布局）
# --------------------------------------------------------------------------

#: 需要 Shift 的字符 -> 基础键规范名
SHIFT_CHARS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal",
    "{": "leftbracket", "}": "rightbracket", "|": "backslash",
    ":": "semicolon", '"': "quote",
    "<": "comma", ">": "period", "?": "slash", "~": "grave",
}

#: 不需要 Shift 的符号 -> 规范名
PLAIN_SYMBOLS: dict[str, str] = dict(SYMBOL_ALIASES)


def text_to_keys(ch: str) -> tuple[str, bool] | None:
    """单个字符 -> (规范键名, 是否需要 Shift)；不可输入返回 None。

    覆盖全部 95 个 ASCII 可打印字符（0x20-0x7E）。
    """
    if len(ch) != 1:
        return None
    code = ord(ch)
    if code < 0x20 or code > 0x7E:
        return None
    if ch == " ":
        return ("space", False)
    if "a" <= ch <= "z":
        return (ch, False)
    if "A" <= ch <= "Z":
        return (ch.lower(), True)
    if "0" <= ch <= "9":
        return (ch, False)
    if ch in SHIFT_CHARS:
        return (SHIFT_CHARS[ch], True)
    if ch in PLAIN_SYMBOLS:
        return (PLAIN_SYMBOLS[ch], False)
    return None  # pragma: no cover - 95 chars 全覆盖，理论不可达


def validate_text(text: str) -> None:
    """校验 text 仅含 ASCII 0x20-0x7E；否则抛 UnsupportedCharError（0-based 索引）。"""
    if not isinstance(text, str):
        raise BadArgError(f"text 必须是字符串，收到 {type(text).__name__}")
    for i, ch in enumerate(text):
        if text_to_keys(ch) is None:
            raise UnsupportedCharError(i, ch)


def text_report(text: str) -> dict:
    """统计 text 的按键构成（供 selftest/调试）。"""
    shifted = 0
    plain = 0
    for ch in text:
        pair = text_to_keys(ch)
        if pair is None:
            raise UnsupportedCharError(0, ch)
        if pair[1]:
            shifted += 1
        else:
            plain += 1
    return {"chars": len(text), "shifted": shifted, "plain": plain}


def key_names() -> list[str]:
    """全部合法键名（含别名），已排序，供 --list-keys。"""
    names = sorted(set(MODIFIER_BITS) | set(MODIFIER_ALIASES) | set(KEYS) | set(SYMBOL_ALIASES))
    return names


def format_key_table() -> str:
    """人类可读的 §4 表（--list-keys 输出）。"""
    lines = []
    lines.append("== protocol.md §4 修饰键 / modifiers (folded into modifier byte) ==")
    for name, bit in MODIFIER_BITS.items():
        alias = ",".join(sorted(a for a, c in MODIFIER_ALIASES.items() if c == name))
        lines.append(f"  {name:<14} 0x{bit:02X}" + (f"   别名: {alias}" if alias else ""))
    lines.append("")
    lines.append("== §4 普通键 / normal keys (HID Usage ID, page 0x07) ==")
    items = list(KEYS.items())
    for i in range(0, len(items), 4):
        row = items[i:i + 4]
        lines.append("  " + "".join(f"{n:<13}0x{v:02X}  " for n, v in row).rstrip())
    lines.append("")
    lines.append("== §4 text Shift 规则 (US layout) ==")
    lines.append("  A-Z -> leftshift + 小写键; " + " ".join(sorted(SHIFT_CHARS)) + " -> leftshift + 基础键")
    lines.append("")
    lines.append(f"共 {len(KEYS)} 个普通键 + {len(MODIFIER_BITS)} 个修饰键（另含符号别名 {len(SYMBOL_ALIASES)} 个）。")
    return "\n".join(lines)