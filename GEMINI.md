# GEMINI.md

这块 ESP32-P4 板子 = 这台 Windows 上的真实 USB 键鼠（无驱动、无注入）。
完整说明书（唯一真相）：[docs/ai-controller.md](docs/ai-controller.md)——作业格式、26 个操作、第 7 节排错。

**红线**：禁止改固件（`firmware/` 只读，不编译、不烧录、不改源码）；不动用户窗口（只 restore/focus）；
全屏不发键鼠（除非 `"force": true`）；不可逆动作（删除/发送/支付/关机）先问人；别猜坐标，先 `ocr`/`find`/`ui`。

**MCP**：仓库已带 `.gemini/settings.json`（顶层 `mcpServers`，server `esp32p4-hid-board`，命令
`python tools/mcp_server.py`，10 个 `board_*` 工具），在这个项目里启动 Gemini CLI 就能用。

**让它额外读 AGENTS.md（可选）**：往同一个 `.gemini/settings.json` 里再加一个键：

```json
{"context": {"fileName": "AGENTS.md"}}
```

不走 MCP 时：`python tools/connector.py --selfcheck`，或把作业 JSON 从 stdin 喂给它。
