# 验收报告：串口自动找修复（pc/brain.py）+ 单测（tests/brain_port.py）

- **验收人**：code-reviewer（老板角色，独立审查；未改任何交付代码）
- **日期**：2026-10-18 20:30（Asia/Shanghai）
- **仓库**：`C:\Users\lu'hao\Documents\deepseek-harness\default-workspace\esp32p4-hid-agent`
- **基线**：`git HEAD 86c17a0`；本轮工作树：`M pc/brain.py`、`?? tests/brain_port.py`
- **快照**：
  - `pc/brain.py` sha256 `cfe94eb9…56fc8`
  - `tests/brain_port.py` 6,377 B / 175 行，sha256 `aef5ecdd…ad5e9`

**一句话结论：通过（P0=0，P1=0）。** `_resolve_uart_port` 的自动/显式/回退/异常分支、`Brain` 的 auto 默认、uart 与 tcp 回退路径、测试不碰串口都核对无误；只有几个 P2 级边界与测试覆盖建议。

---

## 1. 验收范围与方法

1. `pc/brain.py`：`_resolve_uart_port(port)`、`Brain.__init__` 默认值、uart/回退/tcp 成功三条路径
2. `tests/brain_port.py`：15 项单测，是否覆盖契约、是否真不碰串口/不建 Brain
3. 回归面：`connector.py` 建 Brain 不传 port；显式传 port 的工具；tcp 成功不探测

方法：读完整 diff 与函数上下文 → 跑任务要求的三条命令 → 另写不落仓库的独立探针（`/tmp/brain_port_probe.py`）覆盖任务点名的边界（缺 `com` / 空 `com` / 非 dict / `None` 输入 / 空白）→ 逐行核对 `detect_port` 与所有 `Brain(...)` 调用方。**未打开真串口、未建 Brain、未起 hub。**

---

## 2. 实跑结果（命令 + 输出 + 退出码）

| 命令 | 结果 | 退出码 |
|---|---|---|
| `python tests/brain_port.py` | **15/15 PASS**，`全部通过` | **0** |
| `python tests/smoke.py` | **22/22 PASS**，`全部通过` | **0** |
| `python -c "import pc.brain; print(pc.brain._resolve_uart_port('auto'))"` | 打印 **`COM7`** | **0** |
| 独立探针 `/tmp/brain_port_probe.py` | **17/18 PASS**；唯一 FAIL = `com` 为纯空白时返回空白串（见 P2-1） | **1**（探针按“应回退”断言，代码现状不 strip） |

> 第三条命令实际会枚举一次本机串口（只读，安全）；本机当前找到/回退结果是 `COM7`。

---

## 3. 代码审查：`pc/brain.py`

### 3.1 `_resolve_uart_port(port)`（`pc/brain.py:346`）

```python
want = (port or "").strip()
if want and want.lower() != "auto":
    return want
try:
    found = detect_port()
    com = (found or {}).get("com")
    if com:
        return str(com)
except Exception:
    pass
return "COM7"
```

逐条核对：

| 输入 / 场景 | 期望 | 实测 |
|---|---|---|
| `"auto"` | detect → `["com"]` | 返回 detect 的 `COM5` ✅ |
| `"AUTO"` / `"  auto  "` | 大小写/空白不敏感 | 走 detect ✅ |
| `""` / `"  "` / `None` | 视为自动 | 走 detect ✅ |
| 显式 `"COM4"` / `" COM4 "` | 原样（去空白）返回 | `COM4` ✅ |
| 显式 `"com4"` | 原样返回、不探测 | `com4`（`UartTransport` 自己会 normalize）✅ |
| detect 返回 `{"com":"COM5"}` | COM5 | ✅ |
| detect 返回 `None` | 回退 COM7 | ✅ |
| detect 抛异常 | 回退 COM7、不炸 | ✅ |
| **detect 返回 `{}`（缺 com）** | **回退 COM7** | **COM7 ✅（任务特别点名的边界）** |
| **detect 返回 `{"com":""}`** | **回退 COM7** | **COM7 ✅** |
| detect 返回 `{"com":None}` | 回退 COM7 | ✅ |
| detect 返回 `{"com":0}` | 回退 COM7 | ✅ |
| detect 返回非 dict（`[]` / `"COM5"`） | 回退 COM7 | ✅（`.get` 抛 AttributeError 被 catch） |
| detect 返回 `{"com":"   "}` | 应回退 | ❌ 返回 `"   "`（P2-1） |
| detect 返回 `{"com":5}` | 应校验 | 返回 `"5"`（P2-3，低） |

> **关键边界结论：缺 `com` 或 `com==""` 都不会拿空口当有效口，会回退 `COM7`**。纯空白是唯一没被拦的变体；`detect_port` 真实产物来自 Win32 端口名，不会是空白串，因此列 P2 防御性修复，不是 P0/P1。

### 3.2 `Brain.__init__` 默认值与路径（`pc/brain.py:366`）

- 默认 `port="auto"`（`inspect.signature` 实测 PASS）。
- `self.port = port` 先赋值；**只有 uart / tcp 回退才调用 `_resolve_uart_port` 并写回**：
  - `transport=="tcp"` 成功：直接 `TcpTransport.open()`，**不调用 `_resolve_uart_port`**，`self.port` 保持 `"auto"`（`info()` 会显示 `"auto"`，P2-6）。
  - `tcp` 失败且 `fallback_uart=True`：`self.port = _resolve_uart_port(port)`，再 `UartTransport(port=self.port)`（`:385-389`）。
  - 非 tcp（uart）：`self.port = _resolve_uart_port(port)`，再建 `UartTransport(port=self.port)`（`:391-392`）。
- `fallback_uart=False` 时 tcp 失败直接 `raise`，不探测（符合“只回退才找口”）。
- 解析在 `AgentBridge` 创建前完成，`self.port`/`transport_note` 一致。

> 结论：**tcp 成功路径不触发串口探测**（静态可证：成功分支没有 `_resolve` 调用）；uart 与回退都解析；解析结果写回 `self.port`。

### 3.3 调用方回归

| 调用方 | 传 port？ | 结果 |
|---|---|---|
| `tools/connector.py:653` / `:858` | **不传**（`Brain(transport=transport)`） | 吃到 auto 默认 ✅ |
| `tools/agent.py` / 窗口版（走 connector） | 不传 | 吃到 auto ✅ |
| `tools/draw-rage.py`、`demo-mslogo`、`demo-smiley`、`verify-*` | 传 `args.port`（默认 COM7） | 显式路径，不探测，行为不变 ✅ |
| `tools/home-calibrate.py:233` | **写死 `port="COM7"`** | 行为不变（但 UART 模式吃不到 auto，P2-4） |
| `pc/brain.py` CLI `--port`（`:858`） | 默认 `COM7` | 行为不变（CLI 直跑不吃 auto，P2-5） |

> 任务要求“显式传 port 的工具行为不变”成立；代价是这些工具换 COM 号仍需手动改，自动找只覆盖不传 port 的链路（connector/agent/窗口版/draw-image 等）。

---

## 4. 测试审查：`tests/brain_port.py`

### 4.1 覆盖（15 项全过）

- import + `_resolve_uart_port` 存在
- `"auto"` + `{"com":"COM5"}` → COM5；确认 detect 被调
- `"auto"` + `None` → COM7；确认探测过
- detect 抛异常 → COM7，不炸
- 显式 `"COM4"` → 原样；**detect 0 次**
- `"AUTO"` / `"auto"` / `""` / `"  auto  "` → 自动
- 显式 `"com4"` → 原样/大写可用；detect 0 次

### 4.2 是否真不碰串口/不建 Brain

逐行核对：

- 只 `import brain` / `import transport_uart`；模块级没有打开串口（`Brain` 没被实例化）。
- `DetectStub` 只替换 `brain.detect_port`（以及 `transport_uart.detect_port`），`_resolve_uart_port` 不会触碰真实端口。
- 测试里没有 `Brain(`、`.open()`、`.start()`、`SerialLink`/`UartTransport` 实例化（grep 为 0）。
- 运行 <1s，无串口占用。

> 结论：**测试不会在 CI/别人机器上碰真串口或建 Brain**，符合要求。

### 4.3 测试缺口（P2）

1. **没有覆盖任务点名的边界**：`detect_port` 返回 `{}`（缺 `com`）、`{"com": ""}`、`None` 输入、`{"com": "   "}`；本轮我用独立探针补了，代码除了空白都正确。
2. 没有断言 `Brain.__init__` 的 `port` 默认是 `"auto"`（inspect 可测）。
3. 没有测 tcp 成功路径不调用 `_resolve_uart_port`（需要打桩 `TcpTransport`/`AgentBridge`，稍重；建议至少加静态断言或抽出可测函数）。
4. `DetectStub` 没测“返回非 dict / com 非字符串”的情况。

---

## 5. 分级清单

### P0（必须改，阻塞）—— **0 项**

未发现会导致连错口、崩溃、或让自动找修复失效的 P0。

### P1（建议改，不阻塞签发）—— **0 项**

核心逻辑正确；以下均为 P2。

### P2（可后续处理）

1. **`_resolve_uart_port` 对纯空白 `com` 不设防**：`{"com":"   "}` 会返回空白串，`UartTransport` 打开时才报错；建议 `com = str(com).strip()`，空则回退。真实 `detect_port` 不会产出空白，故低风险。
2. **单测补边界**：`{}` / `{"com":""}` / `None` 输入 / `{"com":"   "}`；以及 `Brain.__init__` 默认 `"auto"` 的断言（任务点名的边界目前只能靠本轮独立探针保证）。
3. **`com` 非 str（如 `5`）会被 `str()` 成 `"5"`**：建议校验类型/格式（`^COM\d+$` 或交给 `UartTransport` 前判空），低风险。
4. **`tools/home-calibrate.py:233` 写死 `port="COM7"` 且无 `--port`**：UART 模式换 COM 号仍连不上；默认 `--transport tcp` 时不受影响。建议改成 `port="auto"` 或加 `--port`（任务要求“显式工具不变”，故只列建议）。
5. **`pc/brain.py` CLI `--port` 默认 `COM7`**（`:858`）：直接跑 `python pc/brain.py ...` 不会自动找口；若希望 CLI 也受益，把默认改成 `auto`。显式工具按任务要求保持不变。
6. **tcp 成功时 `self.port` 仍是 `"auto"`**：`info()` 会报 `port:"auto"`；不影响 TCP 使用，但排障时容易误读。可在 tcp 成功时改成 `"tcp"` 或 `"-"`。
7. **测试没有断言 tcp 成功路径不探测**；建议加一个轻量打桩用例，锁住“省启动”这个契约。

---

## 6. 未验证（照实列原因）

| 项 | 原因 |
|---|---|
| 真板子换到别的 COM 后自动找到它 | 没有拔插/改用户板子的 COM 号；用 `detect_port` 打桩验证逻辑，未做真实换口。 |
| 真实 UART `Brain` 用 auto 解析出的口打开 | 按任务“不起真串口”未启动；只验证了 `_resolve_uart_port` 与构造代码。 |
| tcp 成功路径实测不探测 | 只做了静态核对（成功分支无 `_resolve` 调用）；没有构造真 `Brain`（会开 TCP/串口）。 |
| `detect_port` 在无板/多串口机器上的真实选择 | 本机 import 时打印 `COM7`；未制造“无板/多口”环境。 |
| CI/别的机器 | 本机 Python 3.12 Windows 实跑；未在 CI 或别人机器复跑。 |

---

## 7. 结论

**通过。**

- **P0：0。**
- **P1：0。**
- `_resolve_uart_port`：auto/空/大小写/空白/None、显式原样、detect 返回 `None`/异常/缺 `com`/空 `com`/非 dict，**全部正确回退或返回**；唯一 P2 是纯空白 `com` 未 strip。
- `Brain`：默认 `"auto"`；uart 与 tcp 回退解析并写回 `self.port`；tcp 成功不探测。
- `connector.py` 不传 port → 吃到 auto；显式 `--port` 的工具（含 home-calibrate/CLI 默认 COM7）行为不变。
- `tests/brain_port.py` 15/15 通过，真不碰串口/不建 Brain；缺任务点名的几个边界用例，列 P2。
- `tests/smoke.py` 22/22 通过；`import pc.brain` + resolve `auto` 实测打印 `COM7`。

复现命令：

```bash
python tests/brain_port.py
python tests/smoke.py
python -c "import pc.brain; print(pc.brain._resolve_uart_port('auto'))"
```
