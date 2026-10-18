#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP stdio 服务器协议冒烟：subprocess 起 tools/mcp_server.py，自己实现极简客户端。

跑法：`python tests/mcp_smoke.py`（CI 上也是这一条）。
不需要板子、不需要 mcp 库：一行一条 JSON-RPC 2.0，读 stdout 逐行 json.loads。
每条读回复最多等 20 秒（超时判 FAIL），整体 85 秒硬上限——CI 不会卡死。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SERVER = ROOT / "tools" / "mcp_server.py"

PER_MSG_TIMEOUT = 20.0          # 每条回复最多等多久
TOTAL_BUDGET = 85.0             # 整轮测试最多多久（要求 < 90 秒）
REQ_VERSION = "2025-06-18"      # 客户端要的 MCP 协议版本

EXPECTED_TOOLS = {
    "board_caps", "board_services", "board_manual", "board_run_job", "board_ocr",
    "board_point", "board_click", "board_type", "board_key", "board_see",
}

T0 = time.time()

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


class Abort(Exception):
    """协议没建立（或服务器死了），后面的项没法测。"""


class Client:
    """极简 MCP stdio 客户端：写一行 JSON，读一行 JSON。"""

    def __init__(self, cmd: list[str], cwd: str) -> None:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env)
        self.q: queue.Queue = queue.Queue()
        self.bad_lines: list[str] = []      # 不是单行合法 JSON 的 stdout 行
        self.msgs: list = []                # 收到的每条合法 JSON
        self.err: list[str] = []            # stderr 尾巴，失败时贴出来
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # -- IO
    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.p.stdin.write(line.encode("utf-8"))
        self.p.stdin.flush()

    def send_raw(self, line: str) -> None:
        self.p.stdin.write((line + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def _read_out(self) -> None:
        try:
            for raw in iter(self.p.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    self.bad_lines.append("<空行>")
                    self.q.put(("bad", "<空行>"))
                    continue
                try:
                    msg = json.loads(text)
                except Exception as e:  # noqa: BLE001
                    self.bad_lines.append(text[:200])
                    self.q.put(("bad", "%s（%s）" % (text[:200], e)))
                    continue
                self.msgs.append(msg)
                self.q.put(("msg", msg))
        except Exception:  # noqa: BLE001 — 管道被关是收尾，不是测试失败
            pass
        finally:
            self.q.put(("eof", None))

    def _read_err(self) -> None:
        try:
            for raw in iter(self.p.stderr.readline, b""):
                self.err.append(raw.decode("utf-8", "replace").rstrip())
                if len(self.err) > 200:
                    del self.err[:100]
        except Exception:  # noqa: BLE001
            pass

    # -- 读回复
    def expect_any(self, timeout: float = PER_MSG_TIMEOUT):
        """等下一条消息。返回 (kind, value)，kind ∈ msg/timeout/eof/bad。"""
        left = min(timeout, TOTAL_BUDGET - (time.time() - T0))
        if left <= 0:
            return "timeout", "总时长超过 %.0f 秒" % TOTAL_BUDGET
        try:
            return self.q.get(timeout=left)
        except queue.Empty:
            return "timeout", "等了 %.0f 秒没有输出" % timeout

    def expect(self, rid, timeout: float = PER_MSG_TIMEOUT):
        """等 id==rid 的响应；其他消息（通知等）记下来跳过。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            kind, val = self.expect_any(max(0.0, left))
            if kind != "msg":
                return kind, val
            if isinstance(val, dict) and val.get("id") == rid \
                    and ("result" in val or "error" in val):
                return "ok", val

    # -- 状态
    def alive(self) -> bool:
        return self.p.poll() is None

    def stderr_tail(self) -> str:
        return " / ".join(self.err[-3:])[:300]

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.p.kill()
                try:
                    self.p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass


def result_text(res: dict) -> str:
    """MCP tools/call 结果里的文本（content 里所有 text 段拼起来）。"""
    parts = []
    for item in res.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def main() -> int:
    check("tools/mcp_server.py 存在", SERVER.is_file(), str(SERVER))
    if not SERVER.is_file():
        return finish()

    try:
        client = Client([sys.executable, str(SERVER)], str(ROOT))
    except Exception as e:  # noqa: BLE001
        check("服务器能启动", False, "%s: %s" % (type(e).__name__, e))
        return finish()

    try:
        time.sleep(0.4)
        check("服务器进程活着", client.alive(), client.stderr_tail())
        if not client.alive():
            raise Abort("服务器一启动就退出了")

        # 1) initialize：协议版本回显 / capabilities.tools / serverInfo.name
        try:
            client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": REQ_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "mcp-smoke", "version": "1.0"}}})
            kind, msg = client.expect(1)
            check("initialize 有响应", kind == "ok",
                  "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind != "ok":
                raise Abort("initialize 没有响应")

            res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            check("initialize 响应带 jsonrpc 2.0", msg.get("jsonrpc") == "2.0",
                  repr(msg)[:200])
            check("initialize 回显 protocolVersion", res.get("protocolVersion") == REQ_VERSION,
                  "发出=%r 回来=%r" % (REQ_VERSION, res.get("protocolVersion")))
            caps = res.get("capabilities")
            check("initialize 有 capabilities.tools",
                  isinstance(caps, dict) and isinstance(caps.get("tools"), dict),
                  repr(caps)[:200])
            name = (res.get("serverInfo") or {}).get("name") if isinstance(res.get("serverInfo"), dict) else None
            check("serverInfo.name 正确",
                  isinstance(name, str) and bool(name.strip())
                  and bool(re.search(r"esp32p4|hid", name, re.I)), repr(name)[:120])

            # 2) notifications/initialized：通知不许有任何输出
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            kind, val = client.expect_any(1.5)
            check("notifications/initialized 没有输出", kind == "timeout",
                  "kind=%s 值=%r" % (kind, val))

            # 3) tools/list
            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            kind, msg = client.expect(2)
            check("tools/list 有响应", kind == "ok",
                  "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind != "ok":
                raise Abort("tools/list 没有响应")

            result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            tools = result.get("tools")
            check("tools/list 返回工具列表", isinstance(tools, list), repr(tools)[:200])
            tools = tools if isinstance(tools, list) else []
            names = [t.get("name") for t in tools if isinstance(t, dict)]
            check("工具恰好 10 个", len(tools) == 10, "实际 %d 个: %s" % (len(tools), names))
            check("工具名集合固定", set(names) == EXPECTED_TOOLS,
                  "缺=%s 多=%s" % (sorted(EXPECTED_TOOLS - set(names)),
                                   sorted(set(names) - EXPECTED_TOOLS)))
            check("每个工具都有 description",
                  all(isinstance(t, dict) and isinstance(t.get("description"), str)
                      and t["description"].strip() for t in tools),
                  repr([t.get("name") for t in tools if isinstance(t, dict)
                        and not (isinstance(t.get("description"), str) and t["description"].strip())]))
            check("每个工具都有 inputSchema",
                  all(isinstance(t, dict) and isinstance(t.get("inputSchema"), dict) for t in tools),
                  repr([t.get("name") for t in tools if isinstance(t, dict)
                        and not isinstance(t.get("inputSchema"), dict)]))
            no_firmware = [t.get("name") for t in tools
                           if isinstance(t, dict) and "固件" not in (t.get("description") or "")]
            check("每个 description 都有『固件』红线", not no_firmware,
                  "缺『固件』: %s" % no_firmware)

            # 4) board_caps：JSON、op_count == 26
            client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "board_caps", "arguments": {}}})
            kind, msg = client.expect(3)
            check("board_caps 有响应", kind == "ok", "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind == "ok":
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                check("board_caps isError=false", res.get("isError") is False, repr(res)[:200])
                text = result_text(res)
                caps = None
                try:
                    caps = json.loads(text)
                    check("board_caps text 能 json.loads", True)
                except Exception as e:  # noqa: BLE001
                    check("board_caps text 能 json.loads", False,
                          "%s: %s | 文本=%r" % (type(e).__name__, e, text[:160]))
                check("board_caps op_count == 26",
                      isinstance(caps, dict) and caps.get("op_count") == 26,
                      "op_count=%r" % (caps.get("op_count") if isinstance(caps, dict) else type(caps).__name__))

            # 5) board_manual：含『禁止改固件』
            client.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "board_manual", "arguments": {}}})
            kind, msg = client.expect(4)
            check("board_manual 有响应", kind == "ok", "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind == "ok":
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                text = result_text(res)
                check("board_manual 含『禁止改固件』", "禁止改固件" in text,
                      "文本=%r" % text[:160])

            # 6) 未知工具：错误但不许死
            client.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "__no_such_tool__", "arguments": {}}})
            kind, msg = client.expect(5)
            check("未知工具返回响应", kind == "ok", "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind == "ok":
                has_rpc_error = isinstance(msg.get("error"), dict)
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                check("未知工具返回错误", has_rpc_error or res.get("isError") is True,
                      repr(msg)[:200])
                check("未知工具后进程还活着", client.alive(), client.stderr_tail())

            # 7) 坏 JSON 行：必须有 error/isError 回复，进程不死，之后 ping 还能回
            client.send_raw("{oops")
            kind, msg = client.expect_any(PER_MSG_TIMEOUT)
            check("坏 JSON 行有回复", kind == "msg",
                  "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind == "msg":
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                check("坏 JSON 行回复是错误",
                      isinstance(msg.get("error"), dict) or res.get("isError") is True,
                      repr(msg)[:200])
                check("坏 JSON 后进程还活着", client.alive(), client.stderr_tail())

            client.send({"jsonrpc": "2.0", "id": 6, "method": "ping"})
            kind, msg = client.expect(6)
            check("坏 JSON 之后 ping 仍能回", kind == "ok",
                  "%s: %r %s" % (kind, msg, client.stderr_tail()))
            if kind == "ok":
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                check("ping 返回正常（不是 error）", isinstance(msg.get("result"), dict)
                      and msg.get("error") is None, repr(msg)[:200])

            check("stdout 每行都是单行合法 JSON", not client.bad_lines,
                  "坏行: %s" % client.bad_lines[:3])
            check("所有响应都带 jsonrpc 2.0",
                  all(isinstance(m, dict) and m.get("jsonrpc") == "2.0" for m in client.msgs),
                  repr([m for m in client.msgs if not (isinstance(m, dict) and m.get("jsonrpc") == "2.0")])[:200])
            check("服务器进程一直活着", client.alive(), client.stderr_tail())
        except Abort as e:
            print("SKIP  协议没建立（%s），其余检查跳过" % e)
        except (BrokenPipeError, OSError) as e:
            # 服务器中途死了：写 stdin 会断管，别让异常把汇总也吃了
            check("服务器管道可写（没有中途退出）", False,
                  "%s: %s %s" % (type(e).__name__, e, client.stderr_tail()))
            print("SKIP  服务器中途退出，其余检查跳过")
    finally:
        client.close()

    return finish()


def finish() -> int:
    print("-" * 60)
    if failures:
        print("失败 %d 项: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
