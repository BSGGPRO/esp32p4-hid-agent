# -*- coding: utf-8 -*-
"""pc/pointer_profile.py — 指针标定档案：换设备自动识别 + 应用增益表。

背景
----
ESP32-P4 是**相对**鼠标，Windows「提高指针精确度」还会按速度非线性放大位移，
所以「发多少 count 对应多少像素」随设备/缩放/系统设置变化。
`tools/home-calibrate.py` 会在目标设备上跑一遍 CNC 式四角归零，把结论写进
profile（按屏幕签名分 key）。本模块负责：

* ``screen_signature()`` —— 当前设备签名（分辨率+虚拟桌面+DPI 模式）；
* ``get()`` / ``save()`` —— 读档案；
* ``ensure()`` —— 有档案就返回，没有就明确告诉调用方去跑标定（**不偷偷接管鼠标**）；
* ``apply_to_brain()`` —— 把档案里的增益表注入 ``brain._GAIN_TABLE``。

用法::

    from pointer_profile import ensure, apply_to_brain
    prof = ensure()              # None = 这台设备还没标定过
    if prof:
        apply_to_brain()

    # CLI：看看当前设备有没有档案
    python pc/pointer_profile.py status
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import eyes  # noqa: E402
except Exception:  # noqa: BLE001
    eyes = None

PROFILE_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "dsh")
PROFILE_PATH = os.path.join(PROFILE_DIR, "pointer-profile.json")


def screen_info() -> dict:
    """主屏/虚拟桌面/DPI 模式（先开 DPI 感知，拿到物理像素而不是缩放后的逻辑像素）。"""
    if eyes is None:
        return {}
    enable = getattr(eyes, "enable_dpi_awareness", None)
    if enable is not None:
        try:
            enable()
        except Exception:  # noqa: BLE001
            pass
    fn = getattr(eyes, "screen_metrics", None) or getattr(eyes, "screen_info", None)
    if fn is None:
        return {}
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return {}


def screen_signature(info: dict | None = None) -> str:
    info = info if info is not None else screen_info()
    pw, ph = (info.get("primary") or (0, 0))
    vw, vh = (info.get("virtual") or (pw, ph))
    return "%dx%d|virtual=%sx%s|dpi=%s" % (pw, ph, vw, vh, info.get("dpi_mode", "unknown"))


def load(path: str | None = None) -> dict:
    path = path or PROFILE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save(data: dict, path: str | None = None) -> str:
    path = path or PROFILE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def get(sig: str | None = None, path: str | None = None) -> dict | None:
    data = load(path)
    sig = sig or screen_signature()
    prof = data.get(sig)
    if isinstance(prof, dict):
        prof = dict(prof)
        prof["signature"] = sig
        return prof
    return None


def ensure(verbose: bool = True, path: str | None = None) -> dict | None:
    """当前设备有档案就返回；没有返回 None（提示去跑标定，不自动动鼠标）。"""
    prof = get(path=path)
    if prof is None and verbose:
        print("[pointer_profile] 这台设备还没有标定档案（签名 %s）。\n"
              "  先跑一次：python tools/home-calibrate.py --transport tcp --apply"
              % screen_signature(), file=sys.stderr)
    return prof


def apply_to_brain(brain_module=None, path: str | None = None) -> list | None:
    """把档案里的增益表注入 brain；没有档案/没有增益表则返回 None。"""
    prof = get(path=path)
    if not prof or not prof.get("gain_table"):
        return None
    if brain_module is None:
        import brain as brain_module  # noqa: F811
    table = brain_module.set_gain_table([(m, g) for m, g in prof["gain_table"]])
    return table


def main() -> int:
    data = load()
    sig = screen_signature()
    print("profile 文件 :", PROFILE_PATH, "(存在)" if os.path.exists(PROFILE_PATH) else "(不存在)")
    print("屏幕签名     :", sig)
    if not data:
        print("档案内容     : 空")
        return 1
    prof = data.get(sig)
    print("已知设备数   :", len([k for k in data if not k.startswith("_")]))
    if prof:
        print("当前设备     : 已标定 @", prof.get("ts"))
        print("  桌面       :", prof.get("desktop"))
        print("  增益表     :", prof.get("gain_table"))
        checks = prof.get("checks") or []
        if checks:
            print("  落点最差   :", max(c["err_px"] for c in checks), "px")
        return 0
    print("当前设备     : 未标定（签名不匹配）。已有签名的设备：")
    for k in data:
        if not k.startswith("_"):
            print("   -", k)
    return 2


if __name__ == "__main__":
    sys.exit(main())
