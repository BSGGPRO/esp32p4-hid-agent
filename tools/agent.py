#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/agent.py — 板子控制器：打包成 esp32p4-agent.exe 的那一层。

它只做四件事，执行逻辑全部复用 tools/connector.py：

    esp32p4-agent.exe --manual        打印完整说明书（内嵌 docs/ai-controller.md）
    esp32p4-agent.exe --caps          机器可读的能力清单（操作、参数、规矩、服务状态）
    esp32p4-agent.exe --services      hub / 视觉服务 / 板子 三处在不在
    esp32p4-agent.exe --selfcheck     屏幕 / OCR / 板子 / 指针 / 视觉 五项
    type job.json | esp32p4-agent.exe 执行一份作业（也可以 esp32p4-agent.exe job.json）

退出码与 connector 一致：0 成功，1 作业失败，2 输入有问题。

打包方式（见 tools/build-agent.cmd）不再要求对方装 Python；不打包时直接
`python tools/agent.py ...` 行为完全一样。
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import urllib.request

VERSION = "1.0.0"
HUBS = "http://127.0.0.1:8765"
BOARD_TCP = ("127.0.0.1", 8766)
VISION = "http://127.0.0.1:8771"

# 26 个操作。这份清单和 docs/ai-controller.md、README 三者由 tests/smoke.py 对着代码
# 自动核对——加操作忘了改文档，CI 会红。
OPS = (
    "restore", "focus", "title", "ui",
    "click", "move", "point", "drag", "drag_path", "hold", "scroll", "key", "type",
    "look", "ocr", "find", "diff", "wait_change", "read_text",
    "see", "check", "vision",
    "clip", "launch", "wait",
    "recipe",
)

RULES = [
    "禁止改固件：firmware/ 只读。不许编译、烧录、改任何固件源码或配置；要改先问人。",
    "不动用户窗口：只能 restore/focus，不许最大化、移动、缩放。",
    "全屏应用（游戏/视频）不投键鼠，除非该步显式写 force=true。",
    "不可逆或影响他人的动作（删除、发送、支付、关机）先停下问人。",
    "动手前先看（ocr/find/ui），动完核对（title/ocr）；核对不上就停，不重试输入动作。",
    "坐标只从 ocr/find/ui 拿；视觉模型报的坐标不可信。",
    "串口同时只能一个客户端；先跑 --services。",
]

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _init_stdio() -> None:
    """中文 Windows 的 stdout 是 cp936：说明书里的 ▶ 之类字符会直接抛 UnicodeEncodeError。
    管道输出（AI 读它）统一 UTF-8；终端输出保留控制台编码，只把编不了的字符换成 ?。"""
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            tty = s.isatty()
        except Exception:  # noqa: BLE001
            tty = False
        try:
            s.reconfigure(encoding=(s.encoding or "utf-8") if tty else "utf-8",
                          errors="replace")
        except Exception:  # noqa: BLE001 — 老 Python / 被重定向的流，改了也不亏
            pass


def _manual_path() -> str:
    """打包后说明书跟着 EXE 走；没打包就用仓库里的同一份文件。"""
    base = getattr(sys, "_MEIPASS", None) or ROOT
    return os.path.join(base, "docs", "ai-controller.md")


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _get(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 服务不在是常态，不是异常现场
        return None


def services() -> dict:
    hub = _get(HUBS + "/api/board")
    vis = _get(VISION + "/health")
    return {
        "hub": {"url": HUBS, "up": hub is not None,
                "port": (hub or {}).get("port"), "serial_open": (hub or {}).get("serial_open")},
        "board_tcp": {"addr": "%s:%d" % BOARD_TCP, "up": _tcp_open(*BOARD_TCP)},
        "vision": {"url": VISION, "up": vis is not None,
                   "ocr": (vis or {}).get("ocr"), "vlm_ready": (vis or {}).get("ready")},
    }


def caps() -> dict:
    return {
        "name": "esp32p4-agent",
        "version": VERSION,
        "manual": "--manual（完整说明书，也是这个程序的全部知识）",
        "transport": "auto = 有 hub 走 TCP 8766，没有就直连串口",
        "ops": list(OPS),
        "op_count": len(OPS),
        "safety_rules": RULES,
        "services": services(),
        "example": '{"steps":[{"op":"ocr","rect":[0,0,600,200]}]}',
    }


def _load_connector():
    """connector 就是全部执行逻辑；这里只负责把它的路径找对（打包前后都一样）。"""
    for p in (HERE, os.path.join(ROOT, "tools")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import connector

    return connector


def _pick_transport(want: str) -> str:
    if want in ("tcp", "serial"):
        return want
    # auto：hub 在就用 TCP 透传（它已经占着串口了），否则自己开串口
    return "tcp" if _tcp_open(*BOARD_TCP) else "serial"


def usage() -> str:
    return (
        "esp32p4-agent %s —— 用 ESP32-P4 板子当键鼠的控制器\n\n"
        "  esp32p4-agent --manual          完整说明书（先读这个）\n"
        "  esp32p4-agent --caps            能力清单 JSON\n"
        "  esp32p4-agent --services        hub / 视觉服务 / 板子 状态\n"
        "  esp32p4-agent --selfcheck       五项自检\n"
        "  type job.json | esp32p4-agent   执行作业\n"
        "  esp32p4-agent job.json          同上，从文件读\n\n"
        "退出码：0 成功 / 1 作业失败 / 2 输入有问题\n" % VERSION
    )


def main(argv: list[str]) -> int:
    _init_stdio()
    args = [a for a in argv[1:] if not a.startswith("--transport")]
    want = "auto"
    for i, a in enumerate(argv):
        if a == "--transport" and i + 1 < len(argv):
            want = argv[i + 1]
        elif a.startswith("--transport="):
            want = a.split("=", 1)[1]
    flags = [a for a in args if a.startswith("-")]
    rest = [a for a in args if not a.startswith("-")]

    if "--version" in flags:
        print(VERSION)
        return 0
    if "--manual" in flags:
        path = _manual_path()
        if not os.path.exists(path):
            print(usage(), file=sys.stderr)
            print("（找不到说明书文件：%s）" % path, file=sys.stderr)
            return 2
        with io.open(path, encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    if "--caps" in flags:
        print(json.dumps(caps(), ensure_ascii=False, indent=2))
        return 0
    if "--services" in flags:
        print(json.dumps(services(), ensure_ascii=False, indent=2))
        return 0
    if "-h" in flags or "--help" in flags:
        print(usage())
        return 0

    transport = _pick_transport(want)
    os.environ["CONNECTOR_TRANSPORT"] = transport
    conn = _load_connector()

    if "--selfcheck" in flags:
        return int(conn.main())

    # 作业：命令行给了文件就读文件，否则读 stdin
    if rest:
        with io.open(rest[0], encoding="utf-8-sig") as f:
            raw = f.read()
    else:
        if sys.stdin is None or sys.stdin.isatty():
            print(usage(), file=sys.stderr)
            print("提示：没有作业输入。完整用法看 --manual", file=sys.stderr)
            return 2
        raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({"ok": False, "err": "stdin 里没有作业 JSON",
                          "hint": "--manual 看用法"}, ensure_ascii=False))
        return 2

    sys.stdin = io.StringIO(raw)      # connector.main() 从 stdin 读，这是唯一的交接点
    try:
        return int(conn.main())
    finally:
        sys.stdin = sys.__stdin__


if __name__ == "__main__":
    sys.exit(main(sys.argv))
