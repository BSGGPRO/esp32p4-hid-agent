# Copilot 接入文件（esp32p4-hid-agent）——只放指针，说明书不复制。

这块 ESP32-P4 板子 = 这台 Windows 上的真实 USB 键鼠（无驱动、无注入）。
完整说明书（唯一真相）：[docs/ai-controller.md](docs/ai-controller.md) —— 作业格式、26 个操作、
第 7 节排错。本文件只放指针。

## 红线

**禁止改固件**：`firmware/` 只读，不编译、不烧录、不改源码或配置。不动用户窗口（只 restore/focus）。
全屏应用不发键鼠（除非 `"force": true`）。不可逆动作（删除/发送/支付/关机）先问人。
别猜坐标：先 `ocr` / `find` / `ui`。

## 用法

- VS Code 内置 MCP：仓库已带 `.vscode/mcp.json`（server `esp32p4-hid-board`，10 个 `board_*` 工具）。
- 命令行：`python tools/connector.py --selfcheck`，或把作业 JSON 从 stdin 喂给同一个脚本。
