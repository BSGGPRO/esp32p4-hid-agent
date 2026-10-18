# AGENTS.md — 给 AI 的接入文件（esp32p4-hid-agent）

> 这份文件只放**指针**，不是说明书。唯一真相是 [docs/ai-controller.md](docs/ai-controller.md)：
> 作业格式、26 个操作、实测耗时、排错全在里面。动手前先读它。

这块 ESP32-P4 板子在这台 Windows 上是一副**真实的 USB 键盘鼠标**：无驱动、无注入、不碰目标软件。
你负责看和判断，板子负责按键、移鼠标、截屏读字。

## 红线（违反任何一条，这次任务就算失败）

1. **禁止改固件**：`firmware/` 只读。不许编译、不许烧录刷写，不许改任何 `.c` / `.h` / `sdkconfig` /
   `partitions.csv`。要改先停下来问人——固件已经在板子上跑着。
2. **不动用户窗口**：只允许 `restore`（从最小化还原）和 `focus`（置前）；不许最大化、移动、缩放别人的窗口。
3. **全屏不发键鼠**：前台是全屏应用（游戏、全屏视频）时不发任何输入，除非那一步显式写 `"force": true`。
4. **不可逆先问人**：删除、发送、支付、关机、给真人发消息——先停下问人，别替人决定。
5. **别猜坐标**：坐标只能来自 `ocr` / `find` / `ui`；视觉模型报的坐标是编的，只用它答是/否。

## 三层用法（任选一层，命令都在仓库根执行）

- **A. MCP（最省事）**：`python tools/mcp_server.py` 是 stdio MCP 服务器，10 个工具：
  `board_caps` `board_services` `board_manual` `board_run_job` `board_ocr` `board_point`
  `board_click` `board_type` `board_key` `board_see`。客户端配置见 `.mcp.json`（Claude Code）、
  `.cursor/mcp.json`（Cursor）、`.vscode/mcp.json`（VS Code / Copilot）、`.gemini/settings.json`（Gemini CLI）。
  配置里的 `command` 是 `python`；若本机 `python` 是 Microsoft Store 占位符，改成真解释器路径，
  或改用 `tools\mcp-serve.cmd <解释器>`（先 `where python` 查一眼）。
- **B. 命令行**：`python tools/connector.py --selfcheck`；`python tools/agent.py --caps` /
  `--services` / `--manual`。人用的是窗口版 `python tools/agent_ui.py`。
- **C. 作业 JSON**：`echo '{"steps":[...]}' | python tools/connector.py`，或
  `python tools/agent.py job.json`。输出永远是 JSON；任何一步核对不上就整单失败并停手。

各 AI 该怎么接（MCP / 读 AGENTS.md / 只喂说明书）见 [docs/ai-compat.md](docs/ai-compat.md)。

## 自检与排错

- 开工前跑 `python tools/connector.py --selfcheck`（屏幕 / OCR / 板子 / 指针 / 视觉五项）。
- 三板斧：`python tools/agent.py --services`（hub / 视觉服务 / 板子在不在）、`--caps`（能力清单）、
  `--manual`（说明书全文，和 docs/ai-controller.md 同一份）。
- 出错先看 [docs/ai-controller.md](docs/ai-controller.md) **第 7 节「出错怎么办」**。
