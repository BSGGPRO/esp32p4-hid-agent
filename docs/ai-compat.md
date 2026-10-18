# 哪些 AI 能用这块板子 · 兼容性矩阵

> 写在 2026-10-18。生态变得快，所以**每条结论都带来源**；没有来源的一律标「未验证」，
> 不猜、不吹。下面所有"你只需做一个动作"的命令，都在**仓库根目录**执行。

这块 ESP32-P4 板子 = 这台 Windows 上的一副**真实 USB 键盘鼠标**（无驱动、无注入，不碰目标软件）。
所以能不能用，跟"哪个 AI 更聪明"无关，只跟**这个 AI 有没有办法执行动作**有关。按能力分三层：

| 层 | AI 能干什么 | 你能用的东西 | 适合谁 |
|---|---|---|---|
| **A. MCP** | AI 直接把这些当工具调（最省事，参数由协议描述约束） | `python tools/mcp_server.py` | 支持 MCP 的客户端 |
| **B. 能跑命令** | AI 在终端里跑命令、读输出 | `python tools/connector.py` / `tools/agent.py` + `AGENTS.md` | 编码代理（Codex、Aider、Jules…） |
| **C. 只会读文字** | AI 只能读你贴/拖给它的内容 | [docs/ai-controller.md](ai-controller.md) 这份说明书 | 聊天窗口类（豆包、Kimi…） |

三层跑的是**同一套执行层**（`tools/connector.py`，26 个操作），所以第 1 节那七条硬规矩在哪儿都不打折。

---

## A 层：支持 MCP 的（AI 直接调板子）

MCP（Model Context Protocol）是官方点名的通用插座，原文：“AI assistants like Claude and ChatGPT,
development tools like Visual Studio Code, Cursor, MCPJam, and many others all support MCP”
（[modelcontextprotocol.io 首页](https://modelcontextprotocol.io/)）。

| AI / 工具 | 状态 | 你只需做一个动作 |
|---|---|---|
| Claude Code | ✅ 已核实支持 MCP | `claude mcp add esp32p4-hid-board -- python tools/mcp_server.py`（仓库里还有项目级 `.mcp.json`，进项目自动生效） |
| Claude Desktop | ✅ 已核实支持 MCP | 在 Claude Desktop 的 MCP 配置里加同一个 `command`/`args`（配置文件位置随版本变化，按它自己界面里的 "Developer / MCP" 指引加） |
| ChatGPT | ✅ 官方页面点名支持 MCP | 在 ChatGPT 的连接器/开发者设置里加这个 MCP server（网页版能力与订阅有关，未逐一验证） |
| VS Code + Copilot | ✅ 已核实支持 MCP | 仓库内 `.vscode/mcp.json` 已写好，打开项目后在 Copilot 里启用 `esp32p4-hid-board` |
| Cursor | ✅ 已核实支持 MCP | 仓库内 `.cursor/mcp.json` 已写好，重启 Cursor 即可 |
| Gemini CLI | ✅ 已核实（[官方文档](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)） | `gemini mcp add esp32p4-hid-board python tools/mcp_server.py`；或 `.gemini/settings.json` 的 `mcpServers` |
| MCPJam | ✅ 官方页面点名 | 按它界面加 stdio server，命令同上 |
| Windsurf / Zed / Cline / Continue | ⚠️ 未逐一核实 | 它们都提供"自定义 MCP server"入口，填 `python` + `tools/mcp_server.py` 即可；**这四个我没找到一手文档，先按未验证算** |
| Trae / CodeBuddy / 通义灵码 / 文心快码 | ⚠️ 未验证 | 中国区工具变动快，我没查到稳定的一手文档。请在你本地客户端里看有没有 "MCP" 入口；有就填命令，没有就退回 B/C 层 |

十个工具（名字固定）：`board_caps` `board_services` `board_manual` `board_run_job` `board_ocr`
`board_point` `board_click` `board_type` `board_key` `board_see`。细节见说明书 3.1 节。

> ⚠️ **一个真实的坑**：四份配置里写的是 `"command": "python"`。如果这台机器的 `python` 是
> Microsoft Store 的占位符（`where python` 指向 `WindowsApps\python.exe`，跑起来报
> "Python was not found"），MCP 客户端根本起不来——不是配置写错，是解释器不存在。
> 两个改法：把 `command` 换成真解释器（`...\Scripts\python.exe` 或 `py`），或者用
> `tools\mcp-serve.cmd <解释器路径>`（它先试 `py -3`、再试 `python`，也接受显式路径）。
> 本机（写这份文档的机器）就属于这种情况，所以**"配置已放好"不等于"开箱即通"，先 `where python` 看一眼**。

---

## B 层：能跑命令的（读 AGENTS.md，自己动手）

`AGENTS.md` 是跨生态的"给 AI 的 README"标准，官网自称 6 万多个开源仓库在用，并列出这些工具支持它
（[agents.md](https://agents.md/)）：Codex、Jules、Factory、Aider、goose、opencode、Zed、Warp、VS Code、
Devin、Junie、Amp、Cursor、RooCode、Gemini CLI、Kilo Code、Phoenix、Semgrep、GitHub Copilot coding agent、
Ona、Windsurf、Augment Code。

仓库根已经有 `AGENTS.md`（只放指针 + 红线，说明书还是 docs/ai-controller.md 一份）。所以：

| AI | 你只需做一个动作 |
|---|---|
| Codex（OpenAI） | 在仓库里直接开活，它自己会读 `AGENTS.md` |
| Jules / Factory / goose / opencode / Amp / RooCode / Kilo Code / Devin / Junie / Warp | 同上：读 `AGENTS.md`，然后让它跑命令 |
| Aider | `.aider.conf.yml` 里已经写好 `read: AGENTS.md`，直接 `aider` |
| Gemini CLI | 除了 MCP，还能让它读 `AGENTS.md`（`{"context":{"fileName":"AGENTS.md"}}`，见 agents.md 官网 FAQ） |
| Cursor / Windsurf / Cline | 仓库里的 `.cursor/` `.clinerules` `.windsurfrules` 已就位（Cline/Windsurf 我按同为规则文件推断，未逐一核实） |

它们动手时用的是：`python tools/connector.py --selfcheck`、`echo '{"steps":[...]}' | python tools/connector.py`、
`python tools/agent.py --caps` / `--services` / `--manual`。

---

## C 层：只会读文字的（喂说明书）

| AI | 你只需做一个动作 |
|---|---|
| 豆包 / Kimi / 通义 / 文心 / DeepSeek 网页版 / 任何聊天窗口 | 双击 `esp32p4-agent-ui.exe` → 点「保存 AI 说明书…（下载）」→ 把 `AI-控制器说明书.md` 拖进聊天框，然后说"按这个说明书操作，作业 JSON 你写，我负责执行" |

⚠️ 这一层的边界：**聊天 AI 没有手**。它只能给你 JSON，执行还得靠你（或窗口版的「用板子运行作业」）。
这就是为什么窗口版把「运行作业」也做成了按钮——它就是 C 层用户的执行器。

说明书里专为这一层写了 **第 9 节「只能聊天的 AI 怎么干活」**：回话格式（一次只给一个 ```json 代码块）、
先只读再动手、不许编造结果、不可逆先问人。写这一节之前我拿说明书真跑过一遍：
照着第 9 节抄那条示例作业 → 粘进窗口版 → `✓ 跑完了：1 步（ocr），共 0.9 秒`，38 个可点坐标回来了。
（第一版示例写的是"读 notepad 标题"，在这台机器上直接 `✗ 没跑成：没找到窗口`——已经改成纯只读的 `ocr`。）

> 为什么这些标"推断"：聊天工具的官方文档通常不承诺"支持拖 .md"，但这是通用能力。
> 我没编造任何一个"官方支持"，标的就是标着。

---

## 红线（写进每一个接入文件，AI 是照这些干活的）

1. **禁止改固件**：`firmware/` 只读。不编译、不烧录、不改 `.c/.h/sdkconfig/partitions.csv`。
2. **不动用户窗口**：只允许 `restore` / `focus`，不许最大化、移动、缩放别人的窗口。
3. **全屏不发键鼠**：前台是全屏应用时不发输入（除非那一步显式 `"force": true`）。
4. **不可逆先问人**：删除、发送、支付、关机、给真人发消息——先停下问。
5. **别猜坐标**：坐标只能来自 `ocr` / `find` / `ui`；`see` 报的坐标别信。

---

## 怎么自己验证这份矩阵

```bash
python tools/mcp_server.py --selftest     # MCP 服务器本身健康：initialize/list/caps 全过 → 退出码 0
python tests/mcp_smoke.py                 # 协议级回归（坏 JSON、未知工具、进程不死）
python tests/agents_files.py              # 每个接入文件是否合法、红线在不在、路径真不真实
```

客户端那一侧：连上后让 AI 调一次 `board_caps`——能返回 26 个操作，就说明真的通了（不用碰板子）。

**什么时候这份表会过期**：MCP 协议版本、各客户端配置文件位置、中国区工具的支持情况，都是几个月一变的。
矩阵是按当天官方文档写的，遇到对不上的，以你本地客户端的实际界面为准，回来改这个文件。
