#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接入文件体检：MCP 配置 + 各 AI 的指令文件，必须在能用的状态。

跑法：`python tests/agents_files.py`（CI 上也是这一条）。
这些文件由接入部创建；本地开发时最多等 15 分钟（每 30 秒看一眼），
CI 里（有 CI 环境变量）不等待，缺文件直接 FAIL——不让流水线干等。
等待时长可用 AGENTS_WAIT_SECONDS 覆盖（0 = 不等）。

检查：文件存在 / JSON 能 parse 且顶层只有 mcpServers|servers（配置就是配置，
不放 _note 之类说明文字）/ MCP 配置指向的脚本真实存在 /
指令文件含『禁止改固件』红线并指向 docs/ai-controller.md / 没有本机绝对路径。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Windows runner 的控制台默认 cp1252，打不出中文会抛 UnicodeEncodeError——测试先崩在 print 上
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + extra) if extra and not cond else ""))
    if not cond:
        failures.append(name)


def skip(name: str, why: str) -> None:
    print("SKIP  " + name + "  " + why)


# 接入部要交付的文件（顺序就是报告顺序）
FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".mcp.json",
    ".cursor/mcp.json",
    ".vscode/mcp.json",
    ".gemini/settings.json",
    ".clinerules",
    ".windsurfrules",
    ".aider.conf.yml",
    ".github/copilot-instructions.md",
]
MCP_JSON = [".mcp.json", ".cursor/mcp.json", ".vscode/mcp.json", ".gemini/settings.json"]
CONFIG_KEYS = {"mcpServers", "servers"}     # 各客户端认的顶层键就这两个，其余一律算未知键
INSTRUCTIONS = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".clinerules", ".windsurfrules",
                ".aider.conf.yml", ".github/copilot-instructions.md"]

DOC = ROOT / "docs" / "ai-controller.md"
DOC_RE = re.compile(r"docs[\\/]+ai-controller\.md", re.I)
# 本机绝对路径：C:\Users\... / /home/xxx/ / /Users/xxx/；URL 的 :// 不算
ABS_RE = re.compile(r"(?:[A-Za-z]:(?!//)[\\/]|/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+)")

WAIT = int(os.environ.get("AGENTS_WAIT_SECONDS", "0" if os.environ.get("CI") else "900"))
EVERY = 30


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")


def wait_for_files() -> list[str]:
    """等文件出现；返回仍然缺的。"""
    deadline = time.time() + WAIT
    announced = False
    while True:
        missing = [f for f in FILES if not (ROOT / f).is_file()]
        if not missing or time.time() >= deadline:
            return missing
        if not announced:
            print("等待接入文件出现（最多 %d 秒，每 %d 秒看一眼）：%s"
                  % (WAIT, EVERY, ", ".join(missing)))
            announced = True
        time.sleep(max(0.1, min(EVERY, deadline - time.time())))


# ---------------------------------------------------------------- MCP 配置

def iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(k)
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


PREFIXES = ("${workspaceFolder}/", "${workspaceFolder}\\", "${workspaceFolder}",
            "${workspaceRoot}/", "${workspaceRoot}\\", "%CD%\\", "%CD%/")
PY_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+\.py")
MODULE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*mcp_server$")


def script_refs(data) -> list[str]:
    """从配置里抠出脚本路径（相对仓库根）。"""
    refs: list[str] = []
    for s in iter_strings(data):
        s2 = s
        for p in PREFIXES:
            s2 = s2.replace(p, "")
        found = [m.group(0) for m in PY_TOKEN.finditer(s2)]
        if found:
            refs += found
        else:
            for tok in s2.split():
                if MODULE_TOKEN.match(tok):           # python -m tools.mcp_server 这种
                    refs.append(tok.replace(".", "/") + ".py")
    out: list[str] = []
    for r in refs:
        r = r.replace("\\", "/")
        if r.startswith("./"):
            r = r[2:]
        if r not in out:
            out.append(r)
    return out


def resolve_ref(tok: str) -> Path:
    p = Path(tok)
    return p if p.is_absolute() else (ROOT / p)


def check_mcp_config(rel: str) -> None:
    path = ROOT / rel
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        check("%s 是合法 JSON" % rel, True)
    except Exception as e:  # noqa: BLE001
        check("%s 是合法 JSON" % rel, False, "%s: %s" % (type(e).__name__, e))
        skip("%s 的顶层键 / 脚本路径" % rel, "JSON 没 parse 出来")
        return

    if isinstance(data, dict):
        unknown = sorted(set(data) - CONFIG_KEYS)
        has_servers = any(isinstance(data.get(k), dict) for k in CONFIG_KEYS)
    else:
        unknown, has_servers = [], False
    check("%s 顶层只有 mcpServers / servers" % rel, not unknown and has_servers,
          "未知顶层键: %s（配置就纯配置，说明文字放指令文件）" % unknown
          if unknown else "没有 mcpServers / servers 对象")

    refs = script_refs(data)
    check("%s 指向 mcp_server.py" % rel, any(r.endswith("mcp_server.py") for r in refs),
          "配置里找到的脚本: %s" % (refs or "无"))
    missing = [r for r in refs if not resolve_ref(r).is_file()]
    check("%s 引用的脚本都存在" % rel, bool(refs) and not missing,
          "找不到: %s" % missing)


# ---------------------------------------------------------------- 指令文件

def has_redline(text: str) -> bool:
    if "禁止改固件" in text:
        return True
    low = text.lower()
    return "firmware" in low and any(w in low for w in ("只读", "read-only", "read only", "readonly"))


def check_instruction(rel: str) -> None:
    text = read_text(rel)
    check("%s 含『禁止改固件』红线" % rel, has_redline(text))
    check("%s 指向 docs/ai-controller.md" % rel, bool(DOC_RE.search(text)),
          "没找到 docs/ai-controller.md 字样")


def main() -> int:
    missing = wait_for_files()
    for rel in FILES:
        check("存在 %s" % rel, (ROOT / rel).is_file(),
              "等了 %d 秒仍没有" % WAIT if rel in missing else "")

    for rel in MCP_JSON:
        if (ROOT / rel).is_file():
            check_mcp_config(rel)
        else:
            skip("%s 的 JSON / 脚本检查" % rel, "文件不存在")

    for rel in INSTRUCTIONS:
        if (ROOT / rel).is_file():
            check_instruction(rel)
        else:
            skip("%s 的红线 / 说明书检查" % rel, "文件不存在")

    check("docs/ai-controller.md 存在", DOC.is_file(), str(DOC))

    offenders = []
    for rel in FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        m = ABS_RE.search(text)
        if m:
            offenders.append("%s: %s" % (rel, text[max(0, m.start() - 15):m.end() + 25].replace("\n", " ")))
    check("接入文件没有本机绝对路径（只能相对路径或 %CD%）", not offenders,
          " | ".join(offenders)[:500])

    print("-" * 60)
    if failures:
        print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
