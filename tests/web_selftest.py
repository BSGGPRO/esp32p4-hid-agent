#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web-* 自检：仪表盘静态页 + hub 同源接口（不碰真板子，不需要浏览器）。

覆盖：
  A. 前端静态检查
     - web/index.html、app.js、style.css 存在且被相对路径引用
     - 关键元素 id 齐全；app.js 里 getElementById/querySelector 用到的 id 都在 HTML 里
     - app.js 只轮询 api/board（1 s），页面只显示板子面板；本机面板已于 2026-10-18 按要求移除
     （/api/pc 接口与 hub 采样保留，供其它工具使用，但页面不再引用）
     - 无外网 CDN / 外部字体 / http(s) 资源
     - style.css 有手机竖屏媒体查询
     - 字段名与冻结 schema（v1.2 stats / PC 契约）逐项对照
  B. hub 进程（板子离线路径：--serial COM99 --no-lock）
     - GET / 200 且是仪表盘 HTML；/app.js、/style.css 200 且 Content-Type 正确
     - GET /api/board → ok:false, err:board_offline（页面会显示「板子离线」，不崩）
     - GET /api/pc → ok:true 且是真实数据（CPU 每核/总、内存、GPU、进程、开机时长）
     - GET /api/health → 板子 offline、pc_ready true、透传端口正确
     - OPTIONS /api/pc → 204 + Access-Control-Allow-Origin: *
     - TCP 透传端口可连（串口离线时立刻关闭连接，不会假装成功）
     - GET /api/shutdown（127.0.0.1）→ ok:true 且进程干净退出（exit 0）
     - --no-lock 不产生 .board.lock；不存在 404 JSON
  C. 解析器单测（纯函数，不碰硬件）
     - extract_stats 兼容 {"ok":true,"id":N,"stats":{...}} 与平铺字段两种固件实现

用法（仓库根目录）：
    python docs/evidence/web-selftest.py
退出码 0 = 全过，1 = 有失败。
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tests/ 的上一层就是仓库根
WEB = os.path.join(ROOT, "web")
HUB = os.path.join(ROOT, "pc", "hub.py")
PY = sys.executable

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  -- " + str(detail)) if detail and not ok else ""))
    return ok


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def http(method, url, timeout=8):
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def http_json(url, timeout=8):
    status, headers, body = http("GET", url, timeout)
    try:
        return status, headers, json.loads(body.decode("utf-8"))
    except Exception:
        return status, headers, None


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ===========================================================================
# A. 前端静态检查
# ===========================================================================
def check_frontend():
    html_path = os.path.join(WEB, "index.html")
    js_path = os.path.join(WEB, "app.js")
    css_path = os.path.join(WEB, "style.css")
    for path in (html_path, js_path, css_path):
        check("web/%s 存在" % os.path.basename(path), os.path.isfile(path), path)
    if not all(os.path.isfile(p) for p in (html_path, js_path, css_path)):
        return
    html = read(html_path)
    js = read(js_path)
    css = read(css_path)

    # -- 引用关系 --
    check("index.html 用相对路径引用 style.css", 'href="style.css"' in html)
    check("index.html 用相对路径引用 app.js", 'src="app.js"' in html)
    check("index.html 没有绝对路径引用（/app.js、/style.css）",
          '"/app.js"' not in html and '"/style.css"' not in html)

    # -- 关键 id --
    ids = set(re.findall(r'id="([^"]+)"', html))
    required_ids = [
        "board-panel", "board-status", "board-updated", "board-banner", "board-empty",
        "board-body", "board-uptime", "board-freq", "board-hid", "board-window", "board-temp",
        "cpu-gauge0", "cpu-gauge1", "cpu-val0", "cpu-val1", "cpu-chart",
        "heap-free", "heap-min", "heap-total", "heap-bar", "heap-pct",
        "psram-free", "psram-total", "psram-bar", "psram-pct",
        "sd-state", "sd-type", "sd-capacity", "sd-free", "sd-cid",
        "sd-write", "sd-read",
        "net-state", "net-ip", "net-link", "net-dhcp", "net-gw", "net-mac",
        "uart-rx-ok", "uart-rx-bad", "uart-tx-ok",
        "task-table", "task-tbody", "task-count",
        "pm-card", "pm-state", "pm-toggle", "pm-title", "pm-hint",
        "pm-gov", "pm-counts", "pm-cur", "pm-range", "pm-measured", "pm-selfcheck",
        "refresh-btn", "footer-time",
    ]
    missing = [i for i in required_ids if i not in ids]
    check("index.html 关键元素 id 齐全 (%d 个)" % len(required_ids), not missing, "缺: %s" % missing)

    # -- JS 引用的 id 必须存在（防止改名后静默失效）--
    used_ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", js))
    used_ids |= set(re.findall(r"querySelector\(\s*['\"]#([^\s'\"]+)['\"]\s*\)", js))
    used_ids |= set(re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)", js))
    dangling = sorted(i for i in used_ids if i not in ids)
    check("app.js 引用的 %d 个 id 都存在于 HTML" % len(used_ids), not dangling, "HTML 缺: %s" % dangling)

    # -- 轮询与接口 --
    check("app.js 板子接口用相对路径 api/board", "api/board" in js)
    check("app.js 不再引用本机接口 api/pc（本机面板已移除）", "api/pc" not in js)
    check("app.js 有 1 s 轮询常量", re.search(r"BOARD_POLL_MS\s*=\s*1000\b", js) is not None)
    check("app.js 用 setInterval 轮询板子", "setInterval(pollBoard" in js)
    check("app.js 不再轮询本机（无 pollPc）", "pollPc" not in js)
    check("app.js 有 fetch 超时（AbortController）", "AbortController" in js)
    check("app.js 有数据超时标灰（stale class）", "'stale'" in js or '"stale"' in js)
    check("app.js 对可选温度字段做缺失降级（temp_c/temp_warn）",
          "temp_c" in js and "temp_warn" in js and "num(stats.temp_c)" in js and "stats.temp_warn === true" in js)
    check("app.js 有断线/重连提示文案", "自动重连" in js)
    check("app.js 有板子离线原因映射", all(k in js for k in
          ("board_offline", "board_busy", "board_no_stats")))
    check("app.js 不再依赖跨域/手动 IP（无 localStorage）", "localStorage" not in js)

    # -- 冻结字段名对照 --
    board_fields = ["uptime_ms", "core0_pct", "core1_pct", "mhz", "window_ms",
                    "temp_c", "temp_warn",
                    "heap_free", "heap_min_free", "heap_total", "psram_free", "psram_total",
                    "stack_hwm", "present", "mounted", "capacity_mb", "free_mb", "cid",
                    "speed_write_kbps", "speed_read_kbps",
                    "link_mbps", "dhcp", "rx_ok", "rx_bad", "tx_ok", "proto"]
    missing_fields = [f for f in board_fields if f not in js]
    check("app.js 覆盖板子 v1.2 stats 字段 (%d 个)" % len(board_fields),
          not missing_fields, "缺: %s" % missing_fields)
    pc_fields = ["total_pct", "util_pct", "mem_used_mb", "mem_total_mb",
                 "detected", "cpu_pct", "mem_mb", "uptime_s"]
    leftover_pc = [f for f in pc_fields if f in js]
    check("app.js 不再含本机面板字段 (%d 个)" % len(pc_fields),
          not leftover_pc, "残留: %s" % leftover_pc)
    pm_fields = ["measured_mhz", "governor", "boosts", "downs"]
    missing_pm = [f for f in pm_fields if f not in js]
    check("app.js 覆盖 DFS 物理测频字段 (%d 个)" % len(pm_fields),
          not missing_pm, "缺: %s" % missing_pm)

    # -- 无外网依赖 --
    external_patterns = ["http://", "https://", "//cdn.", "fonts.googleapis", "unpkg.com",
                         "jsdelivr", "cdnjs", "@import url("]
    bad = [p for p in external_patterns if p in html or p in css]
    check("HTML/CSS 无外网资源（无 CDN/外部字体）", not bad, "命中: %s" % bad)
    check("app.js 无 http(s) 外网地址", "http://" not in js and "https://" not in js)
    check("index.html 无外部 <script src=http...>",
          re.search(r"<script[^>]+src=[\"']https?:", html) is None)

    # -- 手机自适应 --
    check("style.css 有媒体查询", "@media" in css)
    check("style.css 有手机断点（max-width）", re.search(r"@media\s*\(max-width", css) is not None)
    check("style.css 用流式网格（grid/flex + minmax/auto-fill）",
          "grid-template-columns" in css and ("minmax(" in css or "auto-fill" in css))
    check("style.css 有暗色配色变量", "--bg:" in css and "--panel:" in css)

    # -- JS 大体结构健全（粗略括号配平，排除字符串干扰的廉价检查）--
    stripped = re.sub(r"'(?:\\.|[^'\\])*'", "''", js)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    code_only = stripped
    pairs = {"{": "}", "(": ")", "[": "]"}
    balanced = True
    for opener, closer in pairs.items():
        if code_only.count(opener) != code_only.count(closer):
            balanced = False
    check("app.js 括号配平（粗略）", balanced,
          "{}=%d/%d ()=%d/%d []=%d/%d" % (code_only.count("{"), code_only.count("}"),
                                          code_only.count("("), code_only.count(")"),
                                          code_only.count("["), code_only.count("]")))


# ===========================================================================
# B. hub 进程自检（板子离线路径）
# ===========================================================================
def wait_http(url, timeout=25.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, _headers, _body = http("GET", url, timeout=2)
            if status < 500:
                return True
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.2)
    print("      hub 未就绪，最后错误：%r" % (last,))
    return False


def check_hub():
    http_port = free_port()
    tcp_port = free_port()
    base = "http://127.0.0.1:%d" % http_port
    lock_path = os.path.join(ROOT, ".board.lock")
    lock_before = os.path.exists(lock_path)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [PY, HUB, "--serial", "COM99", "--no-lock", "--no-firewall",
         "--port", str(http_port), "--tcp-port", str(tcp_port), "--quiet"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env)
    try:
        ready = wait_http(base + "/api/health")
        check("hub 启动并在端口 %d 上响应" % http_port, ready)
        if not ready:
            return

        # -- 静态页 --
        status, headers, body = http("GET", base + "/")
        text = body.decode("utf-8", "replace")
        check("GET / 200 text/html", status == 200 and "text/html" in headers.get("Content-Type", ""),
              "status=%s ctype=%s" % (status, headers.get("Content-Type")))
        check("GET / 返回仪表盘 HTML（只有 board-panel，本机面板已移除）",
              'id="board-panel"' in text and 'id="pc-panel"' not in text
              and 'id="pm-toggle"' in text, "len=%d" % len(text))

        status, headers, body = http("GET", base + "/app.js")
        check("GET /app.js 200 且 Content-Type 是 javascript",
              status == 200 and "javascript" in headers.get("Content-Type", ""),
              "status=%s ctype=%s" % (status, headers.get("Content-Type")))
        status, headers, body = http("GET", base + "/style.css")
        check("GET /style.css 200 且 Content-Type 是 css",
              status == 200 and "text/css" in headers.get("Content-Type", ""),
              "status=%s ctype=%s" % (status, headers.get("Content-Type")))
        status, headers, body = http("GET", base + "/no-such-file")
        check("未知路径返回 404 JSON", status == 404, "status=%s" % status)

        # -- /api/board（离线）--
        status, headers, board = http_json(base + "/api/board")
        check("GET /api/board 200（板子离线时也返回 JSON）", status == 200, "status=%s" % status)
        check("离线时 /api/board ok=false 且 err=board_offline",
              isinstance(board, dict) and board.get("ok") is False
              and board.get("err") == "board_offline",
              "board=%r" % (board,))
        check("离线详情可读（含 COM99 / 端口不存在）",
              isinstance(board, dict)
              and ("COM99" in (board.get("port") or "") or "端口不存在" in (board.get("detail") or "")),
              "detail=%r" % (board.get("detail") if isinstance(board, dict) else None))

        # -- /api/pc（真实数据）--
        # 等本机采样就绪：CPU 每核 + 进程 + 至少一个进程 CPU%（PowerShell 差值需要两个采样点）
        pc = None
        deadline = time.time() + 25
        while time.time() < deadline:
            status, headers, pc = http_json(base + "/api/pc")
            procs = (pc or {}).get("processes") or []
            if (status == 200 and isinstance(pc, dict) and pc.get("ok") is True
                    and (pc.get("cpu") or {}).get("cores") and procs
                    and any(p.get("cpu_pct") is not None for p in procs)):
                break
            time.sleep(0.4)
        check("GET /api/pc 200 且 ok=true", status == 200 and isinstance(pc, dict)
              and pc.get("ok") is True, "status=%s" % status)
        if isinstance(pc, dict) and pc.get("ok"):
            cpu = pc.get("cpu") or {}
            cores = cpu.get("cores") or []
            total = cpu.get("total_pct")
            check("本机 CPU 每核数据真实（%d 核，值 0-100）" % len(cores),
                  len(cores) >= 1 and all(isinstance(v, (int, float)) and 0 <= v <= 100 for v in cores),
                  "cores=%r" % (cores[:6],))
            check("本机 CPU 总占用 0-100", isinstance(total, (int, float)) and 0 <= total <= 100,
                  "total=%r" % (total,))
            check("本机 CPU 主频 > 0", isinstance(cpu.get("mhz"), int) and cpu.get("mhz") > 0,
                  "mhz=%r" % (cpu.get("mhz"),))
            mem = pc.get("mem") or {}
            check("本机内存总量 > 0 且占用率 0-100",
                  isinstance(mem.get("total_mb"), int) and mem["total_mb"] > 0
                  and isinstance(mem.get("pct"), (int, float)) and 0 <= mem["pct"] <= 100,
                  "mem=%r" % (mem,))
            gpus = pc.get("gpu") or []
            check("本机 GPU 有真实数据（nvidia-smi）", len(gpus) >= 1,
                  "gpu=%r" % (gpus,))
            if gpus:
                g0 = gpus[0]
                check("GPU 字段完整（name/util/temp/mem）",
                      bool(g0.get("name")) and "util_pct" in g0 and "temp_c" in g0
                      and "mem_used_mb" in g0 and "mem_total_mb" in g0, "gpu0=%r" % (g0,))
            procs = pc.get("processes") or []
            check("本机进程列表非空且字段完整 (%d 个)" % len(procs),
                  len(procs) >= 1
                  and all(isinstance(p.get("name"), str) and isinstance(p.get("pid"), int)
                          for p in procs), "sample=%r" % (procs[:2],))
            check("进程有 CPU% 采样值（PowerShell 差值）",
                  any(p.get("cpu_pct") is not None for p in procs),
                  "sample=%r" % (procs[:2],))
            npu = pc.get("npu") or {}
            check("NPU 字段存在且为布尔（探测不到=未检测）",
                  isinstance(npu.get("detected"), bool), "npu=%r" % (npu,))
            check("开机时长 > 0", isinstance(pc.get("uptime_s"), int) and pc["uptime_s"] > 0,
                  "uptime_s=%r" % (pc.get("uptime_s"),))

        # -- /api/health --
        status, headers, health = http_json(base + "/api/health")
        check("GET /api/health 200 且 ok=true", status == 200 and isinstance(health, dict)
              and health.get("ok") is True, "status=%s" % status)
        if isinstance(health, dict):
            b = health.get("board") or {}
            check("health 里板子状态=offline 且串口未开",
                  b.get("state") in ("offline", "init", "busy") and b.get("serial_open") is False,
                  "board=%r" % (b,))
            check("health 里本机采样已就绪", health.get("pc_ready") is True)
            check("health 里透传端口与实际一致",
                  health.get("tcp_passthrough_port") == tcp_port,
                  "got=%r want=%r" % (health.get("tcp_passthrough_port"), tcp_port))

        # -- CORS / OPTIONS --
        status, headers, body = http("OPTIONS", base + "/api/pc")
        check("OPTIONS /api/pc 204 + CORS *",
              status == 204 and headers.get("Access-Control-Allow-Origin") == "*",
              "status=%s acao=%s" % (status, headers.get("Access-Control-Allow-Origin")))
        status, headers, body = http("GET", base + "/api/pc")
        check("GET /api/pc 带 Access-Control-Allow-Origin: *",
              headers.get("Access-Control-Allow-Origin") == "*",
              "acao=%s" % headers.get("Access-Control-Allow-Origin"))

        # -- TCP 透传端口（串口离线时应立即关闭，不假装成功）--
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=3)
        sock.settimeout(3)
        try:
            data = sock.recv(64)
            passthrough_ok = data == b""
        except socket.timeout:
            passthrough_ok = False
        finally:
            sock.close()
        check("透传端口 %d 可连；串口离线时立即断开（不假装有板子）" % tcp_port, passthrough_ok)

        # -- 解析器单测 --
        check_parser()

        # -- 优雅关闭 --
        status, headers, bye = http_json(base + "/api/shutdown")
        check("GET /api/shutdown 200 ok=true", status == 200 and isinstance(bye, dict)
              and bye.get("ok") is True, "status=%s body=%r" % (status, bye))
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            rc = None
        check("/api/shutdown 后进程退出码 0", rc == 0, "rc=%r" % (rc,))
        # --no-lock 不动板子锁：只看我们自己的 hub 进程有没有留下锁。
        # 测试期间别的进程（live hub / fw-core / draw）拿锁、放锁都属正常，不算失败。
        lock_after = os.path.exists(lock_path)
        lock_pid = None
        if lock_after:
            try:
                with open(lock_path, "r", encoding="utf-8") as f:
                    lock_pid = (json.load(f) or {}).get("pid")
            except Exception:
                lock_pid = None
        check("--no-lock 的 hub 没有留下 .board.lock（被别人持有不算）",
              not (lock_after and lock_pid == proc.pid),
              "before=%s after=%s lock_pid=%r hub_pid=%r"
              % (lock_before, lock_after, lock_pid, proc.pid))
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        out = ""
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            pass
        if proc.returncode not in (0, None):
            print("      hub 输出：\n" + "\n".join(out.strip().splitlines()[-15:]))


# ===========================================================================
# D. 透传链路压测（假串口 + 真 TcpBridge，不碰硬件）
# ===========================================================================
class _FakeSerial:
    """假串口：收到请求行就按协议回一行响应（stats 响应故意做长，验证长行）。"""

    def __init__(self):
        import queue
        self.rx = queue.Queue()
        self.open = True

    def read(self, max_bytes=None):
        import queue
        try:
            return self.rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data: bytes):
        import json
        import threading
        import time
        for raw in data.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if req.get("cmd") == "ping":
                resp = {"ok": True, "id": req.get("id"), "evt": "pong", "proto": "1.2"}
            else:
                resp = {"ok": True, "id": req.get("id"), "t": 1, "stats": {
                    "uptime_ms": 1234,
                    "cpu": {"core0_pct": 1.0, "core1_pct": 2.0, "mhz": 360, "window_ms": 1000},
                    "mem": {"heap_free": 1, "heap_min_free": 1, "heap_total": 2,
                            "psram_free": 1, "psram_total": 2},
                    "tasks": [{"name": "main", "prio": 1, "state": "blocked", "stack_hwm": 1024}],
                    "sd": {"present": False, "mounted": False, "err": "card_init:ESP_ERR_TIMEOUT",
                           "fs": "unknown", "capacity_mb": 0, "free_mb": None, "cid": "",
                           "type": "unknown", "speed_write_kbps": None, "speed_read_kbps": None},
                    "net": {"up": False, "dhcp": False, "ip": None, "netmask": None, "gw": None,
                            "mac": "80:F1:B2:D3:9C:E6", "link_mbps": 0},
                    "hid": {"mounted": True},
                    "uart": {"rx_ok": 1, "rx_bad": 0, "tx_ok": 1},
                    "proto": "v1.2"}}
            payload = (json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

            def _later(p=payload):
                time.sleep(0.01)
                self.rx.put(p)
            threading.Thread(target=_later, daemon=True).start()

    def flush_input(self):
        pass

    def close(self):
        self.open = False


def _read_lines(sock, want, timeout=6.0):
    import json
    import socket
    import time
    buf = b""
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline and len(lines) < want:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            raw = raw.strip()
            if raw.startswith(b"{"):
                try:
                    lines.append(json.loads(raw.decode("utf-8")))
                except json.JSONDecodeError:
                    pass
    return lines


def check_passthrough_fake_serial():
    """回归测试：8766 透传在多轮连接/恶意断开下必须稳定（旧版会被 poll 锁饿死）。"""
    import socket
    import threading
    import time

    sys.path.insert(0, os.path.join(ROOT, "pc"))
    try:
        import hub
    except Exception as exc:  # noqa: BLE001
        check("import pc/hub.py（透传测试）", False, "导入失败: %r" % (exc,))
        return
    hub.STOP.clear()
    board = hub.BoardLink("COM_FAKE", who="selftest:fake", use_lock=False)
    fake = _FakeSerial()
    board._serial = fake
    board.serial_open = True
    board._lock_held = True
    board._read_stop.clear()
    board._reader = threading.Thread(target=board._reader_loop, name="fake-serial", daemon=True)
    board._reader.start()
    board._thread = threading.Thread(target=board._manager, name="fake-board", daemon=True)
    board._thread.start()
    bridge = hub.TcpBridge(board, "127.0.0.1", 0)
    bridge.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not bridge.bound_port:
            time.sleep(0.05)
        check("假串口 hub 的 TcpBridge 能监听随机端口", bool(bridge.bound_port),
              "bound_port=%r" % (bridge.bound_port,))
        if not bridge.bound_port:
            return
        port = bridge.bound_port

        ok_rounds = 0
        for i in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.settimeout(0.2)
            sock.sendall(b'{"cmd":"ping","id":%d}\n{"cmd":"stats","id":%d}\n' % (100 + i, 200 + i))
            lines = _read_lines(sock, 2)
            sock.close()
            got_ids = [ln.get("id") for ln in lines]
            if got_ids == [100 + i, 200 + i] or got_ids == [200 + i, 100 + i]:
                ok_rounds += 1
        check("透传 3 轮 ping+stats 都能收到响应（stats 为 >512B 长行）", ok_rounds == 3,
              "ok_rounds=%d" % ok_rounds)

        # 恶意客户端：连上就断，不能影响 accept 循环
        for _ in range(3):
            socket.create_connection(("127.0.0.1", port), timeout=3).close()
        time.sleep(0.5)
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.settimeout(0.2)
        sock.sendall(b'{"cmd":"ping","id":999}\n')
        lines = _read_lines(sock, 1)
        sock.close()
        check("连上即断的恶意客户端不影响后续连接（accept 循环没被占死）",
              bool(lines) and lines[0].get("id") == 999, "lines=%r" % (lines,))

        # 同一时刻只允许一个透传客户端
        c1 = socket.create_connection(("127.0.0.1", port), timeout=3)
        deadline = time.time() + 3
        while time.time() < deadline and not board.passthrough:
            time.sleep(0.05)
        c2 = socket.create_connection(("127.0.0.1", port), timeout=3)
        c2.settimeout(2.0)
        c2_closed = False
        try:
            c2_closed = c2.recv(16) == b""
        except OSError:
            c2_closed = True
        finally:
            c2.close()
        c1.close()
        check("第二个透传客户端被拒绝（一次只服务一个）", c2_closed)

        # 断开后 stats 轮询必须自动恢复
        deadline = time.time() + 5
        resumed = False
        last = None
        while time.time() < deadline:
            last = board.snapshot()
            if last.get("ok") is True and board.passthrough is False:
                resumed = True
                break
            time.sleep(0.2)
        check("透传结束后 stats 轮询自动恢复（/api/board 回到 ok=true）", resumed,
              "last=%r" % (last,))
    finally:
        bridge.stop()
        board.stop()
        try:
            bridge.join(timeout=3)
        except RuntimeError:
            pass


# ===========================================================================
# C. 解析器单测（不碰串口）
# ===========================================================================
def check_parser():
    sys.path.insert(0, os.path.join(ROOT, "pc"))
    try:
        import hub  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        check("import pc/hub.py", False, "导入失败: %r" % (exc,))
        return
    check("import pc/hub.py", True)
    nested = hub.extract_stats({"ok": True, "id": 7, "t": 123,
                                "stats": {"uptime_ms": 5, "cpu": {"core0_pct": 1.0}}})
    check("extract_stats 支持嵌套 stats 字段",
          nested == {"uptime_ms": 5, "cpu": {"core0_pct": 1.0}}, "got=%r" % (nested,))
    flat = hub.extract_stats({"ok": True, "id": 8, "uptime_ms": 9,
                              "cpu": {"core0_pct": 2.0}, "mem": {}})
    check("extract_stats 支持平铺字段（无 stats 包装）",
          isinstance(flat, dict) and flat.get("uptime_ms") == 9
          and "id" not in flat and "ok" not in flat, "got=%r" % (flat,))
    check("extract_stats 对无统计字段的响应返回 None",
          hub.extract_stats({"ok": True, "id": 9}) is None)
    check("extract_stats 对错误响应返回 None",
          hub.extract_stats({"ok": False, "id": 10, "err": "unknown_cmd"}) is None)


# ===========================================================================
# 入口
# ===========================================================================
def main():
    print("=" * 78)
    print(" web 仪表盘自检（静态检查 + hub 离线路径 + 解析器 + 透传压测；不碰真板子）")
    print(" ROOT: %s" % ROOT)
    print("=" * 78)
    check_frontend()
    print("-" * 78)
    check_hub()
    print("-" * 78)
    check_passthrough_fake_serial()

    failed = [r for r in RESULTS if not r[1]]
    print("-" * 78)
    print("结果：%d/%d 通过，%d 失败" % (len(RESULTS) - len(failed), len(RESULTS), len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAIL: %s  %s" % (name, detail))
        print("== WEB SELFTEST FAIL ==")
        return 1
    print("== WEB SELFTEST PASS ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
