# CLAUDE.md

这块 ESP32-P4 板子 = 这台 Windows 上的真实 USB 键鼠（无驱动、无注入）。
完整说明书（唯一真相）：[docs/ai-controller.md](docs/ai-controller.md)——作业格式、26 个操作、第 7 节排错。

**红线**：禁止改固件（`firmware/` 只读，不编译、不烧录、不改源码）；不动用户窗口（只 restore/focus）；
全屏不发键鼠（除非 `"force": true`）；不可逆动作（删除/发送/支付/关机）先问人；别猜坐标，先 `ocr`/`find`/`ui`。

**MCP（推荐）**：`claude mcp add esp32p4-hid-board -- python tools/mcp_server.py`；
仓库已带 `.mcp.json`（同一个 server）。10 个工具：`board_caps` `board_services` `board_manual`
`board_run_job` `board_ocr` `board_point` `board_click` `board_type` `board_key` `board_see`。

**不用 MCP 时**：`python tools/connector.py --selfcheck`，或 `echo '{"steps":[...]}' | python tools/connector.py`。
