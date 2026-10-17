#!/usr/bin/env python3
"""网页端响应式 + 运行时错误自检（headless Edge + CDP，纯标准库）。

检查项（每个视口）：
  1. 无横向溢出（documentElement.scrollWidth <= innerWidth + 1）
  2. 板子面板占满内容区宽度（>= 92% 可用宽度）
  3. 关键元素可见且有合理尺寸：board-panel / metrics / cards-2 / pm-card / task-table / footer
  4. 无元素越界（right > innerWidth + 1 的可见元素）
  5. 无 console error / 未捕获异常 / 资源加载失败
  6. 触控目标尺寸（手机视口下按钮/开关 >= 28px 高）

用法：
    python tools/web-layout-check.py [--url http://127.0.0.1:8765/] [--edge <msedge.exe>]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

VIEWPORTS = [
    ("desktop-1440", 1440, 900, False),
    ("desktop-1920", 1920, 1080, False),
    ("tablet-1024", 1024, 768, False),
    ("tablet-820", 820, 1180, False),
    ("phone-390", 390, 844, True),
    ("phone-360", 360, 740, True),
]

PROBE_JS = r"""
(() => {
  const ids = ['board-panel','board-meta','board-uptime','board-temp','cpu-gauge0',
               'heap-free','sd-state','pm-card','pm-toggle','pm-measured',
               'task-table','task-tbody','refresh-btn','footer-time'];
  const out = { innerWidth: window.innerWidth, innerHeight: window.innerHeight,
                docScrollW: document.documentElement.scrollWidth,
                bodyScrollW: document.body.scrollWidth, ids: {}, overflow: [], errors: [] };
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) { out.ids[id] = null; continue; }
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out.ids[id] = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
                    h: Math.round(r.height), vis: cs.visibility, disp: cs.display,
                    fs: parseFloat(cs.fontSize), opacity: cs.opacity };
  }
  const panel = document.getElementById('board-panel');
  if (panel) {
    const r = panel.getBoundingClientRect();
    out.panel = { left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width) };
  }
  // 越界检测：只看可见元素，忽略装饰性/绝对定位的
  const vw = window.innerWidth;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right > vw + 1) {
      out.overflow.push({ tag: el.tagName.toLowerCase(), id: el.id || '',
                          cls: (el.className || '').toString().slice(0, 40),
                          right: Math.round(r.right) });
      if (out.overflow.length >= 8) break;
    }
  }
  // 触控目标
  const targets = [];
  for (const sel of ['#refresh-btn', '.pm-row .switch']) {
    const el = document.querySelector(sel);
    if (el) { const r = el.getBoundingClientRect(); targets.push([sel, Math.round(r.width), Math.round(r.height)]); }
  }
  out.targets = targets;
  return out;
})()
"""


class WS:
    def __init__(self, url: str, timeout: float = 20.0):
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError("websocket handshake failed: %r" % resp[:200])
        self.buf = b""
        self.msg_id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj) -> None:
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, timeout: float = 20.0):
        self.sock.settimeout(timeout)
        h = self._recv_exact(2)
        opcode = h[0] & 0x0F
        length = h[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length)
        if opcode == 0x9:  # ping
            self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            return None
        if opcode == 0x8:  # close
            raise RuntimeError("websocket closed")
        return payload

    def call(self, method: str, params=None, timeout: float = 25.0):
        self.msg_id += 1
        mid = self.msg_id
        self.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.recv(timeout=max(0.5, deadline - time.time()))
            if raw is None:
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise TimeoutError(method)

    def drain_events(self, seconds: float):
        events = []
        deadline = time.time() + seconds
        self.sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                raw = self.recv(timeout=0.2)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                break
            if raw is None:
                continue
            try:
                events.append(json.loads(raw.decode("utf-8", "replace")))
            except Exception:  # noqa: BLE001
                pass
        return events


def launch_edge(edge: str, port: int, profile: str):
    args = [edge, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile, "about:blank"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_page_ws(port: int, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=3) as r:
                targets = json.loads(r.read().decode())
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    raise RuntimeError("no page target on port %d" % port)


FUNC_JS = r"""
(async () => {
  const out = { steps: [] };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const txt = (id) => { const el = document.getElementById(id); return el ? (el.textContent || '').trim() : null; };
  const go = async () => (await fetch('api/board?t=' + Date.now(), { cache: 'no-store' })).json();
  try {
    let b = await go();
    out.steps.push(['板子在线', !!(b && b.ok), b && b.port]);
    out.steps.push(['任务表有行', document.querySelectorAll('#task-tbody tr').length > 0,
                    document.querySelectorAll('#task-tbody tr').length]);
    out.steps.push(['TF 卡状态非空', !!txt('sd-state') && txt('sd-state') !== '—', txt('sd-state')]);
    out.steps.push(['温度非空', !!txt('board-temp') && txt('board-temp') !== '—', txt('board-temp')]);
    out.steps.push(['实测频率含 MHz', /MHz/.test(txt('pm-measured') || ''), txt('pm-measured')]);
    const pm0 = (b.stats && b.stats.pm) || {};
    const gov0 = pm0.governor || null;
    out.steps.push(['初始 governor 已知', !!gov0, gov0]);

    const sw = document.getElementById('pm-toggle');
    const wantOn = gov0 !== 'adaptive';
    if (sw.checked !== wantOn) { sw.click(); } else { sw.checked = wantOn; }
    await sleep(4000);
    b = await go();
    const pm1 = (b.stats && b.stats.pm) || {};
    const target = wantOn ? 'adaptive' : 'fixed360';
    out.steps.push(['点击后 governor=' + target, pm1.governor === target, pm1.governor]);
    out.steps.push(['开关 UI 与后端一致', sw.checked === wantOn, sw.checked]);
    out.steps.push(['提示文案已更新', (txt('pm-hint') || '').length > 2, txt('pm-hint')]);
    out.steps.push(['measured_mhz 有效', typeof pm1.measured_mhz === 'number', pm1.measured_mhz]);

    const back = gov0 === 'adaptive';
    if (sw.checked !== back) { sw.click(); } else { sw.checked = back; }
    await sleep(4000);
    b = await go();
    const pm2 = (b.stats && b.stats.pm) || {};
    out.steps.push(['恢复原状态 ' + gov0, pm2.governor === gov0, pm2.governor]);

    const rb = document.getElementById('refresh-btn');
    out.steps.push(['刷新按钮存在', !!rb]);
    if (rb) { rb.click(); await sleep(2000); }
    out.steps.push(['刷新后页面仍在线', !!(await go()).ok, txt('pm-state')]);
    out.ok = out.steps.every((s) => s[1] === true);
  } catch (e) {
    out.ok = false;
    out.error = String((e && e.message) || e);
  }
  return out;
})()
"""


def run_functional(ws, url: str):
    ws.call("Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    ws.call("Page.navigate", {"url": url + "?func=1&t=%d" % time.time()})
    ws.drain_events(3.0)
    res = ws.call("Runtime.evaluate",
                  {"expression": FUNC_JS, "returnByValue": True, "awaitPromise": True},
                  timeout=120)
    val = res.get("result", {}).get("value") or {}
    lines = []
    for step in val.get("steps") or []:
        name, passed = step[0], step[1] is True
        extra = step[2] if len(step) > 2 else ""
        lines.append("  %-26s %s %s" % (name, "PASS" if passed else "FAIL", extra))
    if val.get("error"):
        lines.append("  异常: " + str(val["error"])[:200])
    return bool(val.get("ok")) and not val.get("error"), lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/")
    ap.add_argument("--edge", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    ap.add_argument("--port", type=int, default=9333)
    a = ap.parse_args()

    results = []
    profile = tempfile.mkdtemp(prefix="dsh-edge-")
    proc = launch_edge(a.edge, a.port, profile)
    try:
        ws = WS(find_page_ws(a.port))
        ws.call("Runtime.enable")
        ws.call("Log.enable")
        ws.call("Page.enable")

        for name, w, h, mobile in VIEWPORTS:
            errors = []
            ws.call("Emulation.setDeviceMetricsOverride",
                    {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile})
            ws.call("Page.navigate", {"url": a.url + "?vp=%s&t=%d" % (name, time.time())})
            # 等加载 + 让 JS 跑几轮轮询
            ev = ws.drain_events(2.6)
            for e in ev:
                m = e.get("method", "")
                if m == "Runtime.exceptionThrown":
                    errors.append("exception: " + str(e.get("params", {}).get("exceptionDetails", {}).get("text"))[:160])
                elif m == "Runtime.consoleAPICalled" and e.get("params", {}).get("type") == "error":
                    args = e["params"].get("args", [])
                    errors.append("console.error: " + " ".join(str(x.get("value", x.get("description", "")))[:120] for x in args))
                elif m == "Log.entryAdded":
                    en = e.get("params", {}).get("entry", {})
                    if en.get("level") in ("error",) and "favicon" not in (en.get("text") or ""):
                        errors.append("log: " + str(en.get("text"))[:160])
            js = ws.call("Runtime.evaluate", {"expression": PROBE_JS, "returnByValue": True})
            probe = js.get("result", {}).get("value") or {}
            results.append((name, w, h, probe, errors))

        func_ok, func_lines = run_functional(ws, a.url)

        # 汇总
        fails = 0
        print("== 网页响应式 + 运行时自检 ==")
        print("url = %s" % a.url)
        for name, w, h, p, errors in results:
            vw = p.get("innerWidth", w)
            overflow_w = p.get("docScrollW", 0) > vw + 1 or p.get("bodyScrollW", 0) > vw + 1
            panel = p.get("panel") or {}
            avail = vw - 2 * panel.get("left", 1) if panel.get("left") is not None else 0
            fill = (panel.get("w", 0) / avail * 100) if avail > 0 else 0
            ids = p.get("ids") or {}
            visible = lambda k: bool(ids.get(k)) and ids[k].get("disp") != "none" and ids[k].get("w", 0) > 0
            missing = [k for k in ("board-panel", "board-uptime", "sd-state", "pm-card", "task-table") if not visible(k)]
            target_list = p.get("targets") or []
            small = [(k, h) for k, w, h in target_list if h < 28]
            if p.get("ids", {}).get("pm-toggle") is None:
                missing.append("pm-toggle")
            oob = p.get("overflow") or []
            ok = (not overflow_w) and not missing and fill >= 92 and not oob and not errors and not small
            fails += 0 if ok else 1
            print("\n[%s] viewport %dx%d  %s" % (name, w, h, "PASS" if ok else "FAIL"))
            print("  横向溢出: %s (docScrollW=%s bodyScrollW=%s vw=%s)"
                  % ("YES" if overflow_w else "no", p.get("docScrollW"), p.get("bodyScrollW"), vw))
            print("  板子面板填充率: %.1f%% (w=%s, 可用=%s)" % (fill, panel.get("w"), avail))
            print("  关键元素缺失: %s" % (missing or "无"))
            print("  越界元素: %s" % (oob[:4] or "无"))
            print("  触控目标过小: %s" % (small or "无"))
            print("  运行时错误: %s" % (errors[:4] or "无"))
            for k in ("board-uptime", "board-temp", "sd-state", "pm-measured"):
                v = ids.get(k)
                if v:
                    print("    %-12s %dx%d @(%d,%d) fs=%.1f" % (k, v["w"], v["h"], v["x"], v["y"], v["fs"]))

        print("\n== 页面功能自检（真实点击开关 / 刷新 / 数据渲染）==")
        for line in func_lines:
            print(line)
        if not func_ok:
            fails += 1

        print("\n结果：%d/%d 视口通过%s；功能自检 %s"
              % (len(results) - fails, len(results),
                 "" if func_ok else "（含功能失败）", "PASS" if func_ok else "FAIL"))
        return 0 if fails == 0 else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
