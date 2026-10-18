#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/mcp_server.py — 把这块 ESP32-P4 板子暴露成一个 MCP 服务器（stdio）。

传输：stdin/stdout，**每行一条 JSON-RPC 2.0 消息**（换行分隔，不是 Content-Length 头）。
stdout 只走 JSON-RPC；日志/杂音一律走 stderr。

工具（10 个，名字固定）：
    board_caps / board_services / board_manual / board_run_job
    board_ocr / board_point / board_click / board_type / board_key / board_see

执行逻辑一行没重写：全部翻译成 connector.run_job 的作业，
transport 用 agent._pick_transport 的语义（auto = 8766 通就走 tcp，否则串口）。

用法：
    python tools/mcp_server.py              # 给 MCP 客户端用（stdio）
    python tools/mcp_server.py --selftest   # 自己跑一遍，打印 JSON，退出码 0/1
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "pc")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SERVER_NAME = "esp32p4-hid-board"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2026-07-28"

MANUAL_NAME = "ai-controller.md"

# 每个工具的 description 都带这条：AI 只看 description 就会用。
REDLINES = (
    "红线（每次都守）：禁止改固件——firmware/ 只读，不编译、不烧录、不改任何源码或配置；"
    "别猜坐标——坐标只能来自 board_ocr / board_see / 作业里的 ocr|find|ui，视觉模型报的坐标不可信；"
    "不可逆或影响他人的动作（删除、发送、支付、关机）先停下来问人。"
)

INSTRUCTIONS = (
    "这块板子是手和眼：它按真实键鼠操作这台电脑。开工顺序：board_caps（看 ops 和铁律）→ "
    "board_ocr / board_see（先看清楚）→ board_run_job（执行）→ 再 board_ocr 核对。"
    "核对不上就停，绝不重试输入动作。\n" + REDLINES
)

# ---------------------------------------------------------------- 懒加载

def _agent():
    """只 import 标准库的 agent：caps / services / _pick_transport。"""
    import agent

    return agent


def _connector():
    """connector 是全部执行逻辑，第一次真正要动板子时才 import。"""
    import connector

    return connector


# ---------------------------------------------------------------- 参数校验

class ToolError(Exception):
    """参数不对：给 AI 看的中文说明（不是崩溃）。"""


def _need(args: dict, key: str) -> object:
    if key not in args or args[key] is None:
        raise ToolError("缺参数 %s。" % key)
    return args[key]


def _as_int(v, key: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ToolError("%s 必须是整数，收到 %r。" % (key, v))
    return int(v)


def _as_text(v, key: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ToolError("%s 必须是非空字符串，收到 %r。" % (key, v))
    return v


def _pick_transport(args: dict) -> str:
    want = args.get("transport") or "auto"
    if want not in ("auto", "tcp", "serial"):
        raise ToolError("transport 只能是 auto / tcp / serial，收到 %r。" % want)
    return _agent()._pick_transport(want)


def _run(steps: list[dict], args: dict) -> dict:
    """把一条或多条作业步骤交给 connector 执行，结果原样返回。"""
    transport = _pick_transport(args)
    res = _connector().run_job({"steps": steps}, transport=transport)
    res["transport"] = transport
    return res


def _job_from(args: dict):
    """board_run_job 的 job：对象或 JSON 字符串，两样都收。"""
    raw = _need(args, "job")
    if isinstance(raw, str):
        try:
            job = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolError("job 不是合法 JSON：第 %d 行第 %d 列，%s" % (e.lineno, e.colno, e.msg))
    else:
        job = raw
    if not isinstance(job, (dict, list)):
        raise ToolError('job 应该是 {"steps": [...]} 或 [...]，收到 %s。' % type(job).__name__)
    steps = job.get("steps") if isinstance(job, dict) else job
    if not isinstance(steps, list):
        raise ToolError('"steps" 必须是数组，收到 %s。' % type(steps).__name__)
    if not steps:
        raise ToolError('作业里没有 steps，没东西可执行。')
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not st.get("op"):
            raise ToolError("第 %d 步不是带 op 的对象：%r。" % (i, st))
    return job


# ---------------------------------------------------------------- 工具实现

def _tool_caps(args: dict) -> dict:
    return _agent().caps()


def _tool_services(args: dict) -> dict:
    return _agent().services()


def _tool_manual(args: dict) -> dict:
    base = getattr(sys, "_MEIPASS", None) or ROOT
    path = os.path.join(base, "docs", MANUAL_NAME)
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except OSError as e:
        raise ToolError("读不到说明书 %s（%s）。" % (path, e))
    return {"ok": True, "file": "docs/" + MANUAL_NAME, "chars": len(text), "markdown": text}


def _tool_run_job(args: dict) -> dict:
    job = _job_from(args)
    steps = job.get("steps") if isinstance(job, dict) else job
    return _run(steps, args)


def _tool_ocr(args: dict) -> dict:
    rect = _need(args, "rect")
    if (not isinstance(rect, (list, tuple)) or len(rect) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in rect)):
        raise ToolError("rect 必须是 4 个整数 [x1,y1,x2,y2]（物理像素），收到 %r。" % (rect,))
    step: dict = {"op": "ocr", "rect": [_as_int(v, "rect") for v in rect]}
    if args.get("as"):
        step["as"] = _as_text(args["as"], "as")
    res = _run([step], args)
    rec = (res.get("steps") or [{}])[0]
    res["rows"] = rec.get("rows")
    res["boxes"] = rec.get("boxes")      # 顶层也给一份：AI 常只看顶层，找不到坐标就只能猜
    return res


def _tool_point(args: dict) -> dict:
    step = {"op": "point",
            "x": _as_int(_need(args, "x"), "x"),
            "y": _as_int(_need(args, "y"), "y")}
    return _run([step], args)


def _tool_click(args: dict) -> dict:
    button = args.get("button") or "left"
    if button not in ("left", "right"):
        raise ToolError('button 只能是 left / right，收到 %r。' % button)
    text = args.get("text")
    if text:
        steps = [{"op": "find", "text": _as_text(text, "text"), "as": "_mcp_hit"},
                 {"op": "click", "on": "_mcp_hit", "button": button}]
    elif args.get("x") is not None and args.get("y") is not None:
        steps = [{"op": "click",
                  "x": _as_int(args["x"], "x"),
                  "y": _as_int(args["y"], "y"),
                  "button": button}]
    else:
        raise ToolError("board_click 需要 x + y（点坐标），或者 text（先在屏幕上找这行字再点它）。"
                        '例如 {"text": "下一步"} 或 {"x": 313, "y": 1022}。')
    return _run(steps, args)


def _tool_type(args: dict) -> dict:
    return _run([{"op": "type", "text": _as_text(_need(args, "text"), "text")}], args)


def _tool_key(args: dict) -> dict:
    return _run([{"op": "key", "combo": _as_text(_need(args, "combo"), "combo")}], args)


def _tool_see(args: dict) -> dict:
    q = args.get("question") or "屏幕上最显眼的是什么？"
    res = _run([{"op": "see", "q": _as_text(q, "question")}], args)
    rec = (res.get("steps") or [{}])[0]
    res["answer"] = rec.get("answer")
    return res


# ---------------------------------------------------------------- 工具清单

def _tool(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "description": description + "\n\n" + REDLINES, "inputSchema": schema}


_OBJ = "object"


TOOLS = [
    _tool("board_caps",
          "返回这块板子的能力清单：26 个操作（ops）、铁律（safety_rules）、hub/板子/视觉服务三处状态。"
          "开工前先调它，照它的 ops 拼 board_run_job 的作业。",
          {"type": _OBJ, "properties": {}, "additionalProperties": False}),
    _tool("board_services",
          "查三处通路在不在：hub http://127.0.0.1:8765、板子 TCP 127.0.0.1:8766、视觉服务 http://127.0.0.1:8771。"
          "板子那条不通就先别动键鼠。",
          {"type": _OBJ, "properties": {}, "additionalProperties": False}),
    _tool("board_manual",
          "返回 docs/ai-controller.md 全文（AI 控制器说明书）：作业格式、每个 op 的参数、变量 as=/on=、故障对照表。"
          "不确定某个 op 怎么用时读它。",
          {"type": _OBJ, "properties": {}, "additionalProperties": False}),
    _tool("board_run_job",
          "执行一段完整作业（connector 的 JSON 作业格式，见 board_manual 或 board_caps.ops）。"
          '例如 {"steps":[{"op":"title","exe":"msedge"}]}。job 可以给对象，也可以给 JSON 字符串。'
          "transport=auto：TCP 8766 通就走 TCP（hub 占着串口时也对），否则直连串口。",
          {"type": _OBJ, "properties": {
              "job": {"type": ["object", "string"],
                      "description": '作业：{"steps":[...]} 或直接 [...]，也可以是同样的 JSON 字符串。'},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto",
                            "description": "auto = 有 TCP 8766 走透传，否则串口。"}},
           "required": ["job"]}),
    _tool("board_ocr",
          "对一个矩形区域做 OCR，返回 rows（逐行文字）和 boxes（每行的屏幕坐标 "
          "[{text,x,y,w,h,cx,cy}]，cx/cy 就是能直接交给 board_click 的点）。"
          "rect 用物理像素 [x1,y1,x2,y2]。定位任何东西的第一步——先拿到真实坐标再点。",
          {"type": _OBJ, "properties": {
              "rect": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                       "description": "物理像素 [x1,y1,x2,y2]。"},
              "as": {"type": "string", "description": "可选：把这批文字行存进作业变量。"},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "required": ["rect"]}),
    _tool("board_point",
          "把光标闭环定位到 (x,y)：移动后自己纠偏到目标像素，比普通移动准。坐标要用 board_ocr 或 board_see 拿到的真实值。",
          {"type": _OBJ, "properties": {
              "x": {"type": "integer", "description": "屏幕 x（物理像素）"},
              "y": {"type": "integer", "description": "屏幕 y（物理像素）"},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "required": ["x", "y"]}),
    _tool("board_click",
          "点一下：给 x+y 就点坐标；给 text 就先在屏幕上找这行字、点它的中心（比蒙坐标可靠）。"
          "button 默认 left。",
          {"type": _OBJ, "properties": {
              "x": {"type": "integer", "description": "屏幕 x（物理像素）"},
              "y": {"type": "integer", "description": "屏幕 y（物理像素）"},
              "text": {"type": "string", "description": "要点的文字，先 find 再点它的中心。"},
              "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "anyOf": [{"required": ["x", "y"]}, {"required": ["text"]}]}),
    _tool("board_type",
          "敲一段文字（只支持能直接映射的键；中文要先用输入法或 recipe）。",
          {"type": _OBJ, "properties": {
              "text": {"type": "string", "description": "要敲的字。"},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "required": ["text"]}),
    _tool("board_key",
          '按一个组合键，例如 "ctrl+s"、"enter"、"alt+f4"。',
          {"type": _OBJ, "properties": {
              "combo": {"type": "string", "description": '组合键，如 "ctrl+s"。'},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "required": ["combo"]}),
    _tool("board_see",
          "截屏后问视觉模型一句话（它只答 20 字内），例如「现在前台是什么窗口」。"
          "只能用来认内容和判断是/否；它报的坐标是编的，要坐标就用 board_ocr。",
          {"type": _OBJ, "properties": {
              "question": {"type": "string", "description": "问题，一句话。"},
              "transport": {"type": "string", "enum": ["auto", "tcp", "serial"], "default": "auto"}},
           "required": []}),
]

TOOL_NAMES = [t["name"] for t in TOOLS]

_HANDLERS = {
    "board_caps": _tool_caps,
    "board_services": _tool_services,
    "board_manual": _tool_manual,
    "board_run_job": _tool_run_job,
    "board_ocr": _tool_ocr,
    "board_point": _tool_point,
    "board_click": _tool_click,
    "board_type": _tool_type,
    "board_key": _tool_key,
    "board_see": _tool_see,
}

# ---------------------------------------------------------------- 工具调用

def call_tool(name: str, args) -> tuple[bool, str]:
    """执行一个工具，返回 (isError, 给 AI 看的文本)。永不抛异常。"""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return True, "arguments 必须是 JSON 对象，收到 %s。\n原始参数：%s" % (
            type(args).__name__, json.dumps(args, ensure_ascii=False))
    if name not in _HANDLERS:
        return True, "没有这个工具：%r。可用：%s" % (name, ", ".join(TOOL_NAMES))
    try:
        payload = _HANDLERS[name](args)
    except ToolError as e:
        return True, "%s\n收到的参数：%s" % (e, json.dumps(args, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — 任何异常都变成 isError，绝不带崩进程
        return True, "工具 %s 执行出错：%s: %s\n收到的参数：%s" % (
            name, type(e).__name__, e, json.dumps(args, ensure_ascii=False))
    text = json.dumps(payload, ensure_ascii=False)
    ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
    if ok:
        return False, text
    why = payload.get("err") if isinstance(payload, dict) else None
    return True, "板子没执行成功：%s\n原始结果：%s" % (why or "作业失败（没说原因）", text)


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tools_call_result(name: str, args) -> dict:
    is_error, text = call_tool(name, args)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Server:
    """一条消息进、一条响应出（通知不响应）。"""

    def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return _error(None, -32600, "无效请求：一条消息必须是 JSON 对象。")
        has_id = "id" in msg
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            return None                     # 客户端发来的响应/杂音，不回应
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            if not has_id:
                return None
            proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
            return _result(mid, {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "initialized"):
            return None                     # 通知：不回应
        if method == "ping":
            return _result(mid, {}) if has_id else None
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS}) if has_id else None
        if method == "tools/call":
            out = _tools_call_result(params.get("name"), params.get("arguments"))
            return _result(mid, out) if has_id else None
        return _error(mid, -32601, "方法不存在：%s" % method) if has_id else None


# ---------------------------------------------------------------- 传输

_OUT = None          # 真正的 stdout（二进制）；协议只许走这里


def _setup_streams() -> None:
    """接管 stdout：协议消息走私有句柄，任何杂音（包括依赖里的 print）都掉进 stderr。"""
    global _OUT
    out = getattr(sys.stdout, "buffer", None)
    if out is None:
        raise SystemExit(3)             # 没有 stdout 就当不了 stdio 服务器
    _OUT = out
    try:
        if sys.stderr is not None:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 老 Python / 重定向的流，改不了就算了
        pass
    sys.stdout = sys.stderr             # 之后所有误写的 print 都去 stderr


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    _OUT.write(line.replace("\n", " ").encode("utf-8") + b"\n")
    _OUT.flush()


def serve() -> int:
    _setup_streams()
    srv = Server()
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue                    # 空行：跳过，不是错误
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            _send(_error(None, -32700, "解析失败：第 %d 列，%s" % (e.colno, e.msg)))
            continue
        try:
            resp = srv.handle(msg)
        except Exception as e:  # noqa: BLE001 — 处理器的任何意外都不许打断循环
            mid = msg.get("id") if isinstance(msg, dict) else None
            resp = _error(mid, -32603, "内部错误：%s: %s" % (type(e).__name__, e))
        if resp is not None:
            _send(resp)
    return 0


# ---------------------------------------------------------------- 自测

def _init_stdio_text() -> None:
    """自测经管道输出时强制 UTF-8：中文 Windows 控制台默认 cp936，会把 JSON 打花。"""
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
        except Exception:  # noqa: BLE001
            pass


def selftest() -> int:
    _init_stdio_text()
    srv = Server()
    cases = {
        "initialize_default": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        "initialize_echo": {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}},
        "initialized_notification": {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "ping": {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        "tools/list": {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        "tools/call board_caps": {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                  "params": {"name": "board_caps", "arguments": {}}},
        "unknown_method": {"jsonrpc": "2.0", "id": 6, "method": "no/such_method"},
        "unknown_tool": {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                         "params": {"name": "board_nope", "arguments": {}}},
        "bad_params": {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "board_click", "arguments": {}}},
    }
    out: dict = {}
    for label, msg in cases.items():
        out[label] = srv.handle(msg)

    caps_res = out["tools/call board_caps"]["result"]
    try:
        caps = json.loads(caps_res["content"][0]["text"])
    except Exception:  # noqa: BLE001
        caps = {}
    tools = out["tools/list"]["result"]["tools"]
    descriptions = [t["description"] for t in tools]
    one_line = all("\n" not in json.dumps(r, ensure_ascii=False) for r in out.values()
                   if r is not None)
    checks = {
        "initialize_回显默认协议版本": out["initialize_default"]["result"]["protocolVersion"]
        == DEFAULT_PROTOCOL,
        "initialize_回显客户端版本": out["initialize_echo"]["result"]["protocolVersion"]
        == "2025-06-18",
        "capabilities.tools_为空对象": out["initialize_default"]["result"]["capabilities"]
        == {"tools": {}},
        "serverInfo_正确": out["initialize_default"]["result"]["serverInfo"]
        == {"name": SERVER_NAME, "version": SERVER_VERSION},
        "notifications/initialized_不回应": out["initialized_notification"] is None,
        "ping_返回空对象": out["ping"]["result"] == {},
        "工具数=10": len(tools) == 10 and [t["name"] for t in tools] == TOOL_NAMES,
        "每个description都带红线": all(
            "firmware/" in d and "board_ocr" in d and "先" in d and "问人" in d
            for d in descriptions),
        "board_caps_成功": caps_res["isError"] is False and bool(caps.get("ops"))
        and bool(caps.get("safety_rules")),
        "未知方法=-32601": out["unknown_method"]["error"]["code"] == -32601,
        "未知工具=isError": out["unknown_tool"]["result"]["isError"] is True,
        "缺参数=isError": out["bad_params"]["result"]["isError"] is True,
        "每条响应都是单行JSON": one_line,
    }
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "checks": checks, "tool_count": len(tools),
                      "tools": TOOL_NAMES,
                      "caps_ops": caps.get("op_count"),
                      "sample_board_caps": caps_res["content"][0]["text"][:400]},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if "--selftest" in argv[1:]:
        return selftest()
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
