# 验收报告：MCP 服务器 / 接入文件 / 「给 AI 用」面板

- **验收人**：code-reviewer（本轮以「老板」角色做独立验收，未改任何交付代码）
- **日期**：2026-10-18 18:15（Asia/Shanghai）
- **仓库**：`C:\Users\lu'hao\Documents\deepseek-harness\default-workspace\esp32p4-hid-agent`
- **基线**：`git HEAD 0c23d2e`；工作树含本轮未提交改动（新文件已 staged，见 §1.2）
- **解释器**：Python `3.12.14`，`C:\Users\lu'hao\dsh-vision\Scripts\python.exe`
- **结论**：**通过**
- **P0：0**；**P1：发现 3 项，验收期间已全部修复并复验（§6），无遗留**；**P2：见 §7**；**未验证：见 §8（照实列原因，未写成“通过”）**

---

## 1. 验收范围、方法与快照

### 1.1 范围

本轮交付：

- `tools/mcp_server.py`（stdio MCP 服务器，10 个工具，一行一条 JSON-RPC 2.0）
- 11 个接入文件：`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、`.mcp.json`、`.cursor/mcp.json`、
  `.vscode/mcp.json`、`.gemini/settings.json`、`.clinerules`、`.windsurfrules`、
  `.aider.conf.yml`、`.github/copilot-instructions.md`
- `tools/agent_ui.py` 新增「给 AI 用…」面板（Toplevel）
- `docs/ai-compat.md`、`tests/mcp_smoke.py`、`tests/agents_files.py`、`tools/mcp-serve.cmd`
- 两个交付 EXE（Lead 打包，验收在仓库外副本上跑）

### 1.2 验收时的文件快照（sha256）

```
b77db015...d1292d1  tools/mcp_server.py
368ac5cd...69a1b    tools/agent_ui.py
4865f08e...be2ce06  tools/connector.py
0e3b7d47...d32bcd09 tools/agent.py
4b1ae2e0...77a38323 tests/mcp_smoke.py
c8467094...abbb0cf114 tests/agents_files.py
03e43ef7...5cb0aa   tests/smoke.py
91b3c7e6...4da829de tests/ui_smoke.py
dd806383...acd07156 tests/ui_io.py
0e0fde67...861382c  tests/links.py
afd830bc...2b13b1   docs/ai-controller.md
6d22ced3...75f095   docs/ai-compat.md
1d095ea2...55a503   AGENTS.md
2ee6e7bf...c392bb   README.md
78b729bd...aa39fd   README.en.md
1d79fc15...50e56b *.mcp.json / .cursor/mcp.json / .gemini/settings.json
2d36805b...a80742 *.vscode/mcp.json
a8b3e821...b575f71  C:\Users\lu'hao\Documents\deepseek-harness\esp32p4-agent.exe（29,303,356 B）
8e71fe82...891bd14  C:\Users\lu'hao\Documents\deepseek-harness\esp32p4-agent-ui.exe（32,333,639 B）
```

`git status` 显示本轮文件均为未提交状态（`AGENTS.md`/`tools/mcp_server.py` 等为 `AM`，`tests/*.py` 为 `A`，`README*.md`/`tools/connector.py` 为 `M`）；验收未做 commit/push。

### 1.3 方法

所有结论来自**实跑**：测试套件、独立实现的 MCP stdio 客户端探针、真实 stdio 会话、源码/EXE 两种形态、
只读 OCR（不碰键鼠）、对 11 个接入文件逐行阅读与静态检查、全仓库红线 grep、联网核对文档来源。
跑不了的项在 §8 标「未验证 + 原因」。

---

## 2. 结果总表

| 验收项 | 结果 | 关键证据 |
|---|---|---|
| 6 套测试 | ✅ 全过（exit 0） | smoke 22 / ui_smoke 31 / ui_io 13 / mcp_smoke 32 / agents_files 43；links 14 文件 |
| MCP 手工协议探针 | ✅ 43/43 | initialize/list/caps/manual/坏 JSON/未知工具/通知静默/单行 JSON/进程存活 |
| MCP 10 个工具 | ✅ | 名字集合精确匹配；10/10 description 含「固件」红线 |
| board_caps / board_manual | ✅ | op_count=26、safety_rules=7；说明书 9,950 字符与 `docs/ai-controller.md` 逐行一致 |
| board_ocr 坐标（本轮改动） | ✅ 只读实跑 | 经 MCP：rows=9、boxes=9，顶层 + `steps[0]` 双份；首框 `S (124,29,27,16,cx137,cy37)` |
| 11 个接入文件 | ✅ | 4 份 JSON 合法且顶层只有 `mcpServers`/`servers`；7 份指令文件都有「禁止改固件」+ docs 指针；0 处本机绝对路径 |
| 「给 AI 用」面板 | ✅ | 8 个目标；MCP JSON 合法；源码运行 command=当前 venv 解释器；冻结态 4 种路径情形正确；`btn_ai` 在 |
| 两个交付 EXE | ✅（仓库外拷件） | agent：`--version 1.0.0` / `--caps` 26+7 / `--manual` UTF-8；ui：`--selftest` 6/6 exit 0 |
| 红线 grep | ✅ | AI 接入文件中 0 处烧录/编译指令；README 中英已加「只给人类」；代码无最大化；仅 draw-rage 预存在移动（P2） |
| 一致性 | ✅ | 10 个工具名在 5 处一致；命令全部真入口；说明书 3.1 存在；3 条一手来源联网核对通过 |

---

## 3. 六套测试（本机实测）

命令（仓库根，解释器用绝对路径）：

```bash
python tests/smoke.py && python tests/links.py && python tests/ui_smoke.py \
  && python tests/ui_io.py && python tests/mcp_smoke.py && python tests/agents_files.py
```

| 套件 | PASS 数 | 退出码 | 关键输出 |
|---|---|---|---|
| `tests/smoke.py` | 22 | 0 | `全部通过`；新增第 22 项「connector 输出是 UTF-8（不是 cp936）」PASS |
| `tests/links.py` | — | 0 | `链接全部有效（14 个文件，按 git 跟踪文件核对）` |
| `tests/ui_smoke.py` | 31 | 0 | `全部通过`；含 App 构建、save_manual 无 BOM、run_job_text 边界 |
| `tests/ui_io.py` | 13 | 0 | `全部通过`；含 stdout=None、权限失败、换行、无 BOM |
| `tests/mcp_smoke.py` | 32 | 0 | `全部通过`；协议超时 20s/总预算 85s 未触发 |
| `tests/agents_files.py` | 43 | 0 | `全部通过`；11 文件 + 4 JSON + 7 指令 + 0 绝对路径 |

`agents_files.py` 在 CI 环境变量缺失时会等文件最多 900s；本次文件齐全，立即完成，无等待。
`.github/workflows/ci.yml` 已包含 `python tests/mcp_smoke.py`（第 41 行）与 `python tests/agents_files.py`（第 46 行）。

---

## 4. MCP 协议手工探针（不是只跑测试）

独立实现极简 stdio 客户端（`%TEMP%\mcp_accept_probe.py`，不在仓库内），进程：
`python tools/mcp_server.py`，cwd=仓库根，逐行发送/读取， 每条 20s 超时。

**结果：43/43 PASS，exit 0；startup(initialize) 0.04s；收到 19 条响应；stderr 0 行；stdin 关闭后进程退出码 0。**

关键断言（全部实测）：

- `initialize`：`jsonrpc=2.0`、回显客户端 `protocolVersion=2025-06-18`、`capabilities.tools` 存在、
  `serverInfo.name=esp32p4-hid-board`、`instructions` 含红线
- `notifications/initialized`：1.2s 内 **0 输出**（没有回 `null`）
- `tools/list`：恰好 10 个，名字集合 == `{board_caps, board_services, board_manual, board_run_job, board_ocr,
  board_point, board_click, board_type, board_key, board_see}`；每个有 description/inputSchema；
  **10/10 description 含「固件」红线**；无占位符
- `tools/call board_caps`：`isError=false`，text 可 `json.loads`，`op_count=26`，`safety_rules` 7 条含「禁止改固件」
- `tools/call board_manual`：`isError=false`，含「禁止改固件」，text 9,950 字符（与 `docs/ai-controller.md` 一致）
- `tools/call board_services`：三键齐全 `hub/board_tcp/vision`
- 坏 JSON `{oops`：返回 `-32700`、id=null；**之后 ping 仍正常**（进程没死）
- 未知工具：`isError=true`（中文列出可用工具）；未知方法：`-32601`
- `tools/call board_ocr` 缺 `rect`：`isError=true`，未触板；`board_run_job` 坏 JSON/空作业：`isError=true`
- `transport=bogus`：在碰板前被拦，`isError=true`
- 空行：忽略；随后 ping 正常
- JSON 数组（batch）：返回 `-32600`，进程存活（MCP 不定义批量语义，可接受）
- 无 id 的 `tools/call`：执行但不回包（下一个响应是后续 ping 的 id）
- stdout 每行都是单行合法 JSON；所有响应 `jsonrpc=2.0`；stderr 无协议 JSON 泄漏
- `python tools/mcp_server.py --selftest`：**exit 0**，13 项全 true，`tool_count=10`、`caps_ops=26`

---

## 5. 接入文件与文档（逐份读过）

11 个文件（含新增 `.gemini/settings.json`）全部存在；行数：
`AGENTS.md 38`、`CLAUDE.md 13`、`GEMINI.md 18`、`.clinerules 7`、`.windsurfrules 7`、
`.aider.conf.yml 6`、`.github/copilot-instructions.md 16`（均为指针，不复制说明书）。

| 检查 | 结果 |
|---|---|
| JSON 合法性 / 顶层键 | 4 份全合法；`.mcp.json`/`.cursor`/`.gemini` 顶层 `mcpServers`，`.vscode` 顶层 `servers` |
| MCP 配置指向脚本真实存在 | 4/4：`tools/mcp_server.py` 按仓库根解析存在 |
| 「禁止改固件」 | 7/7 指令文件都有 |
| 指向 `docs/ai-controller.md` | 7/7 都有，文件存在 |
| 本机绝对路径 | **0 处**（grep `[A-Za-z]:[\\/]|/home/|/Users/` 无命中） |
| 命令真实性 | `connector.py --selfcheck`、`agent.py --caps/--services/--manual`、`agent.py job.json`、`mcp_server.py`、`agent_ui.py` 均为真入口（逐一实跑或读入口代码） |

文档一致性：

- `docs/ai-controller.md` 新增 **3.1 用 MCP 接**；10 个工具名/参数与 `mcp_server.TOOLS` 对得上；
  `docs/ai-compat.md` 里「细节见说明书 3.1 节」现在指向真实存在的 3.1。
- `board_manual` 返回的 markdown 与 `docs/ai-controller.md` **9,950 字符、逐行相同**（忽略行尾空白）。
- 26 个 op / 7 条 safety_rules 在 README、README.en、smoke、agent、connector 之间一致。

来源核对（web_fetch，非猜测）：

- MCP 首页原文：「AI assistants like Claude and ChatGPT, development tools like Visual Studio Code,
  Cursor, MCPJam, and many others all support MCP」——与 `ai-compat.md` 引用一致；页面版本 **2026-07-28**
  正是服务器默认协议版本（不是编的）。
- `agents.md`：「used by over 60k open-source projects」；工具列表（Codex/Jules/Aider/.../Windsurf/Augment）
  与文档一致；FAQ 给出 `.aider.conf.yml` 的 `read: AGENTS.md` 和 Gemini 的 `{"context":{"fileName":"AGENTS.md"}}`，
  仓库写法与之相同。
- Gemini CLI 官方文档：`gemini mcp add [options] <name> <command> [args...]`，stdio `mcpServers` 配置，
  与 `ai-compat.md`/`GEMINI.md` 一致。

---

## 6. 验收期间发现并已修复的 P1（逐条复验）

### P1-1 本机 `python` 是 Microsoft Store 占位符 → 4 份配置在本机起不来（已缓解 + 文档写明）

- 证据：`where python` → `...\WindowsApps\python.exe`；`cmd /c python tools\mcp_server.py < nul` →
  `Python was not found; run without arguments to install from the Microsoft Store...`；`py` 也不存在。
- 现状：4 份配置**故意不改**成硬编码路径（保持可移植）；README 中/英、`docs/ai-compat.md`、`AGENTS.md`
  都已加「`where python` 先看一眼；是 Store 占位符就换真解释器 / 用 `tools\mcp-serve.cmd <解释器>`」。
- 复验：`tools\mcp-serve.cmd C:\...\dsh-vision\Scripts\python.exe < nul` → exit 0；
  不带参数 → stderr 报 Store 占位符、stdout 干净（exit 49）。
- 源码运行窗口版时，面板 MCP JSON 的 `command` 已等于**当前解释器**
  (`C:\Users\lu'hao\dsh-vision\Scripts\python.exe`)，不受 PATH 坑影响。
- 结论：**配置已放好 ≠ 开箱即通，本机需要改 `command`**；真实客户端实连见 §8。

### P1-2 「给 AI 用」面板命令类文案给了错命令（已修复）

- 修复前：`tools/agent_ui.py:151` 为 `python tools/connector.py --services`；
  `connector.py` 只认 `--selfcheck`（`connector.py:1130`），实测 `connector.py --services` →
  `{"ok": false, "err": "stdin 里没有作业 JSON"}`，退出码 0。
- 修复后：`python tools/agent.py --services`。源码探针复验：Codex/Cursor 两段文案均含
  `agent.py --services`、不再含 `connector.py --services`；面板 8 个目标 JSON 全部合法。

### P1-3 `connector.py` 管道输出是 cp936/GBK（已修复）

- 修复前：`echo '{"steps":[{"op":"ocr",...}]}' | python tools/connector.py > out.json`，
  bytes 为 `\xb4\xcb\xb5\xe7\xc4\xd4`（GBK「此电脑」），UTF-8 解码失败；`--selfcheck` 同样。
- 修复后：`connector.py` 的 `main()` 开头调用 `_init_stdio()`；cmd 重定向实测：
  `tools\connector.py < job.json > out.json` → 621 B、UTF-8 解码通过、`ok=true`、`rows=6`、`boxes=6`；
  `--selfcheck` → 1,547 B UTF-8、5 项、exit 0。
- 回归测试：`tests/smoke.py` 新增「connector 输出是 UTF-8（不是 cp936）」并 PASS（22/22）。

### P2 修复复验（本轮一并处理）

- **boxes 位置**：`mcp_server._tool_ocr` 现在把 `rows` 和 `boxes` 都提到顶层（`steps[0]` 里仍保留）。
  经真实 stdio MCP 会话调用 `board_ocr [0,0,600,200]`：`isError=false`、`rows=9`、`boxes=9`、
  首框 `{'text':'S','x':124,'y':29,'w':27,'h':16,'cx':137,'cy':37}`。
- **README 从零跑通的烧录命令**：中/英均在命令块上方加了引用块：
  「第 1 步的 `build`/`flash` 只给人类。AI 一律不得执行（见红线：禁止改固件）」，并说明 2～4 步 AI 可跑。

---

## 7. P2（不阻塞签发，建议后续处理）

1. **真实客户端解释器前提**：4 份配置固定 `"command": "python"`；本机是 Store 占位符，文档已写明改法。
   冻结态 EXE 的面板仍生成 `python`（源码运行才自动填当前解释器）。建议在客户端侧固定解释器，
   或让 EXE 也探测 `tools\mcp-serve.cmd`。
2. **EXE 在 DSH 工作区树内运行会被 onefile 拦截**：在仓库目录树里跑 `dist\esp32p4-agent*.exe` 复现
   `Could not create temporary directory!`（exit 127）；复制到 `Documents\deepseek-harness\` 等仓库外
   即正常（本次全部 EXE 验收都在仓库外副本完成）。建议 README 加一句「把 EXE 拷出仓库再双击」，
   这是本机 DSH 沙箱限制，不是应用逻辑。
3. **`tools/draw-rage.py:207` 会 `SetWindowPos` 移动 Paint 窗口**：预存在、有注释、仅在重叠且
   改善面积 ≥10% 时触发、等尺寸移动；不在 MCP/UI/connector 路径上，非本轮交付；按 Lead 决定不改。
   查遍 `tools/`、`pc/`：没有最大化 API；`connector.py` 只用 `ShowWindow(h,9)/ShowWindow(h,5)`（还原/显示）。
4. **`docs/ai-compat.md` 部分 ✅ 行没有逐行一手链接**（Claude Code/Desktop、VS Code、Cursor、ChatGPT），
   已有 MCP 首页/agents.md/Gemini 文档作为来源；建议逐行补链接，维持「每条结论带来源」的承诺。
5. **MCP 配置没有 `cwd`**：Claude Code 项目级一般以项目根为 cwd、Cursor/VS Code 以工作区为 cwd，
   Gemini 支持 `cwd` 字段；若客户端从别处启动，相对 `tools/mcp_server.py` 可能找不到。
   建议注释说明或加 `cwd`/`${workspaceFolder}`（VS Code 支持）。
6. **MCP 协议边角**（标准客户端不影响，记录）：
   - `initialize` 原样回显任意 `protocolVersion`（含不支持的字符串），未回退到服务器支持版本；
   - JSON 数组批量 → `-32600`（MCP 不定义批量，可接受）；
   - 无 `id` 的 `tools/call` 会执行副作用但不回包；
   - 没有 stdout（如 `pythonw`）时 `_setup_streams` 直接 `SystemExit(3)`。
7. **测试覆盖缺口**：没有测试「MCP 配置能真正拉起客户端」和「面板文案里的命令/参数可执行」——
   本轮 P1-1/P1-2 正是这类漏网。建议 `tests/agents_files.py` 加 `shutil.which(command)` 预检、
   `tests/ui_smoke.py` 加 `ai_payload` 断言（命令存在于 `connector.py`/`agent.py` 的真参数表）。
8. **深层人类文档**（`docs/bringup-runbook.md`、`docs/field-guide.md`、`docs/hardware-facts.md`）仍有
   `idf.py build/flash`、`esptool write_flash` 命令；它们是硬件从零部署文档，主入口 README 已加
   「AI 不得执行」，风险低；若要绝对严格，可在这三处也加同一句。

---

## 8. 未验证项（照实列，未写成“通过”）

| 项 | 原因 |
|---|---|
| 真实 MCP 客户端实连：Claude Code / Claude Desktop / Cursor / VS Code+Copilot / Gemini CLI / ChatGPT | 本机 `python` 是 Store 占位符（见 P1-1），客户端未安装/未授权；协议层已用独立 stdio 客户端 43 项验证，配置文件 schema 按各家官方文档核对。**保留“本机 python 不在 PATH”的结论**，文档已写明改法。 |
| 真实键鼠动作：`board_point` / `board_click` / `board_type` / `board_key`、`board_run_job` 的输入类作业 | 会物理移动/点击/敲击验收机，可能影响用户；按验收纪律不真跑。只验证了参数校验/错误路径（均 `isError=true`，未触板）。 |
| `board_see` / 视觉大模型 | 会按需加载 VLM（显存/模型），本轮未跑；OCR 已只读跑通（见 §6）。 |
| UI EXE 真实双击弹窗 | 只跑 `--selftest`（进程内建窗，7 个控件含 `btn_ai`，6/6）与源码 `App()` 构建；未在桌面实际双击观察（Lead 称 WMI 启动，未采信）。 |
| EXE 在仓库目录树内运行 | 本环境被 DSH/onefile 拦截并复现（见 P2-2）；仓库外副本正常，未在非 DSH 的普通桌面复核。 |
| 各客户端对配置 schema 的真实解析 | JSON 合法、键名按官方写法；未逐一安装客户端做端到端连接。 |
| CI 流水线 | 本机无 CI 环境，未真跑；已用与 CI 相同的命令本地跑完 6 套。 |

---

## 9. 红线抽查（全仓库）

- **AI 接入文件中的烧录/编译可执行指令**：`grep -nE "idf\.cmd (build|.*flash)|idf\.py (build|flash)|esptool.*(write_flash|flash_id)"` 对 11 个接入文件 → **0 处**。
- **README 主入口**：中/英命令块上方均有「第 1 步 build/flash 只给人类，AI 一律不得执行」引用块（README.md:119、README.en.md:127 附近）。
- **窗口最大化/移动**：`tools/`、`pc/` 中无 `SW_MAXIMIZE/SW_SHOWMAXIMIZED/state('zoomed')`；
  `connector.py` 仅 `ShowWindow(h,9)`（还原）、`ShowWindow(h,5)`（显示）。唯一移动是 `tools/draw-rage.py:207`
  的 `SetWindowPos`（预存在、有注释、只在画图窗口与聊天窗口重叠时、等尺寸、非交付路径），见 P2-3。
- **固件目录**：验收全程只读 `firmware/`，未编译/未烧录/未改任何 `.c/.h/sdkconfig/partitions.csv`。

---

## 10. 一致性核对

- **工具名**：`board_caps board_services board_manual board_run_job board_ocr board_point board_click
  board_type board_key board_see` —— `mcp_server.py`、`AGENTS.md`、`CLAUDE.md`、`docs/ai-compat.md`、
  `README.md`、`docs/ai-controller.md` §3.1 六处集合一致；`GEMINI.md` 用 `board_*` 泛称并指向配置。
- **命令**：AGENTS/README/ai-compat 的 `connector.py --selfcheck`、`agent.py --caps/--services/--manual`、
  `agent.py job.json`、`mcp_server.py`、`agent_ui.py` 全部对应真入口；`agent.py` 失败作业 exit 1、成功 exit 0，
  与说明书一致。面板命令类文案在 P1-2 修复后与 `agent.py` 一致。
- **说明书 3.1**：存在，列出 10 个工具及参数；`ai-compat.md` 的「3.1 节」指针有效。
- **打包件与源码**：`connector.py` mtime 18:04、`agent_ui.py` 18:05，EXE 构建于 18:07/18:08；
  EXE `--manual` 含 `3.1` 与 `boxes`、UTF-8；`--caps` 26/7；`--selftest` 6/6，说明打包用的是修复后代码。

---

## 11. 打包件实测（仓库外副本）

```text
C:\Users\lu'hao\Documents\deepseek-harness\esp32p4-agent.exe
  --version → 1.0.0                         exit 0
  --caps    → op_count 26 / safety_rules 7  exit 0
  --manual  → 含 3.1、boxes（2 处）、禁止改固件；UTF-8  exit 0
C:\Users\lu'hao\Documents\deepseek-harness\esp32p4-agent-ui.exe
  --selftest → exit 0，6/6 ok：
    说明书 9950 字符 / 含禁止改固件 / 保存 14874 B /
    op_count 26 / 窗口 7 控件（含 btn_ai）/ 给 AI 用不出现临时目录路径
```

补充：交付的 UI EXE 单独放在 `Documents\deepseek-harness\`（旁边没有仓库）时，`--selftest` 明确报
「8 个目标，身边无仓库（会给出替代说明）」，不产生假路径；把仓库放在旁边时，冻结态路径查找
（EXE 旁边 → 旁边的 `esp32p4-hid-agent/` → 当前目录）四种情形均按预期工作。

---

## 12. 签字

**通过。**

本轮核心交付（MCP 协议、10 个工具、11 个接入文件、面板、文档、两套新测试、两个 EXE）在最终状态下
可独立复现、无 P0、无遗留 P1。验收期间发现的 3 个 P1 已修复并用证据复验（§6）；剩余 P2 为
可移植性/文档/边角行为，不阻塞；未验证项已按事实列出（§8），没有把未跑的项目写成“通过”。

复现入口：

```bash
python tests/smoke.py && python tests/links.py && python tests/ui_smoke.py \
  && python tests/ui_io.py && python tests/mcp_smoke.py && python tests/agents_files.py
python tools/mcp_server.py --selftest
python tools/agent_ui.py --selftest
```
