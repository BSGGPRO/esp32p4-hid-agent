# esp32p4-hid-agent — 让 AI 用 ESP32-P4 当"手"，用截屏当"眼"

> JC-ESP32P4-M3（GUITION，ESP32-P4 + 板载 C6）插电脑即插即用成一个**标准 USB HID 键鼠**，
> PC 端零依赖脚本负责「截屏 → 发指令 → 板子动手 → 再截屏验证」的闭环。
> 纯 ESP-IDF（不用 Arduino），零驱动、零授权、不碰任何反作弊。

---

## 当前状态（2026-10-17，真机已跑通）

| 部分 | 状态 | 证据 |
|---|---|---|
| 固件 `firmware/` | ✅ 编译 + 烧录 + 真机运行 | `esp32p4_hid_agent.bin` = 328,736 B，0 error / 0 warning；esp_tinyusb 2.4.0 + tinyusb 0.21.0 |
| HID 描述符 | ✅ 从编译产物解析验证 | `tools/check-hid-descriptor.py` → ID 1 = 64 bits（8B 键盘）/ ID 2 = 40 bits（5B 鼠标），逐字段吻合 §5，`RESULT: PASS`（证据 `docs/evidence/hid-descriptor.txt`） |
| 真机枚举 | ✅ **被系统认成标准键鼠** | `HID Keyboard Device`(COL01) + `HID-compliant mouse`(COL02)，序列号 = 芯片 MAC `80F1B2D39CE6` |
| PC 桥 `pc/` | ✅ 已验收 | `selftest` 39/39 PASS；截屏 2560×1440；零第三方依赖（仅 Pillow） |
| 协议 `docs/protocol.md` | ✅ **v1.1 冻结** | 15 命令 / 7 错误码 / 4 事件；v1.1 修掉 `release_all` 被限流、RX 20 ms 空等 |
| 硬件事实 `docs/hardware-facts.md` | ✅ 取证 + §11 真板实测补充 | 含芯片 rev v1.3 约束、签名 CH340、协议实测 |
| 端到端真机闭环 | ✅ **通过** | 截屏→移鼠标→再截屏差分：变化 7037 px（0.19%），`E2E OK` |
| 性能 | ✅ 达标 | ping **18.4 ms**（目标 <30）/ mouse_move **12.5 ms**（目标 <15）/ 连发 **332 条/秒** |

### 真机验收清单（Windows 侧独立取证，不采信板子自报）

| 检查 | 取证方式 | 结果 |
|---|---|---|
| `host_mounted` | 固件 `status` | `True` |
| 鼠标相对移动 | `GetCursorPos()` 前后对比 | 请求 (120,60) → 实测 +199,+23（方向正确；幅度被"提高指针精确度"放大，属系统行为） |
| 键盘按下/抬起 | `GetAsyncKeyState(VK_SHIFT)` | False → **True** → False |
| Win 键 | `GetAsyncKeyState(VK_LWIN)` | **True** → False |
| 安全释放 | 按住 ctrl 后停发报文 2.2 s，再读按键状态 | 已自动全抬（`safety_timeout_ms=1500` 兜底生效） |
| 端到端闭环 | 截屏差分 | 有变化，`E2E OK: change detected` |

### 接线（两个口必须同时插）

| 口 | 接到 | 作用 |
|---|---|---|
| 转 UART（CH340） | 本机 | 烧录 + 指令通道；换 USB 口会改 COM 号，用 `--list-ports` 自动识别 |
| 高速 Type-C | 目标机（本机亦可） | USB HID 键鼠输出，`VID_303A&PID_4004` |

只插 UART 口：能烧录、能收发协议，但 `host_mounted=False`，板子"没有手"。
只插高速口：板子被认成键鼠，但没有指令通道，板子"没有脑"。**两个都要。**

---

## 快速开始（一条命令，两个口都插好后）

```powershell
cd %USERPROFILE%\Documents\deepseek-harness\default-workspace\esp32p4-hid-agent
pwsh -ExecutionPolicy Bypass -File tools\flash-and-verify.ps1
```

它会：自动挑出 CH340 串口（自动排除蓝牙虚拟口）→ 烧录 → `ping` 探活 → 校验 `host_mounted` → 跑 `e2e-demo`（截屏→鼠标右移→再截屏→差分）。
接线见上表：**转 UART 口 → 本机**（烧录+指令），**高速 Type-C → 目标机**（HID）。

只想验证不重烧：`tools\flash-and-verify.ps1 -SkipFlash`。
只想跑端到端：`python pc\agent_bridge.py e2e-demo --dx 200 --port COM7`。

手工分步版见 [docs/bringup-runbook.md](docs/bringup-runbook.md)。

---

## 目录

```
esp32p4-hid-agent/
├─ docs/
│  ├─ protocol.md          # ★ 固件↔PC 唯一接口契约（Lead 维护，v1.1）
│  ├─ hardware-facts.md    # 硬件/烧录事实取证（每条带证据等级）
│  └─ bringup-runbook.md   # 上电·烧录·验收手册（含故障排查）
├─ firmware/               # ESP-IDF v5.5.6 / esp32p4 / esp_tinyusb v2
│  ├─ main/{app_main,uart_link,keymap,hid_device,cmd_protocol}.c
│  ├─ sdkconfig.defaults   # 16MB/QIO/80MHz、HID_COUNT=1、日志全关
│  └─ README.md            # size 报告 + cmd→函数表
├─ pc/                     # 零依赖 Python 桥（仅 Pillow）
│  ├─ agent_bridge.py      # CLI + 协议客户端
│  ├─ transport_uart.py    # ctypes 直调 Win32 串口（无 pyserial）
│  ├─ eyes.py / verify.py  # 截屏 / 像素差分
│  ├─ mock_p4.py / selftest.py
│  └─ README.md            # 验收证据 + 命令清单
└─ tools/
   ├─ idf.cmd              # ESP-IDF 包装器（cmd）
   ├─ idf-env.ps1          # 同上（PowerShell，dot-source）
   ├─ install-ch340-admin.cmd   # ★ 需管理员
   ├─ check-hid-descriptor.py   # 从 ELF 里解析 HID report descriptor 并校验报告布局
   └─ flash-and-verify.ps1      # 一键烧录+验收
```

---

## 关键设计决策（都是踩过坑之后的定论）

| 决策 | 值 | 为什么 |
|---|---|---|
| HID 结构 | 单接口 + Report ID 1=键盘 / 2=鼠标 | 与 IDF 官方 `tusb_hid` 例程一致，Windows 免驱识别为键鼠两个设备 |
| 端点 bInterval | **4** | HS 下 2³×125µs = **1 ms/帧**；用 10 会变成 **64 ms/帧**，拆帧移动和打字直接不可用 |
| VID/PID | 0x303A / **0x4004**（esp_tinyusb 默认） | 不自定义 PID，少一个配置面 |
| 挂载事件 | 25 ms 轮询 `tud_mounted()` 边缘检测 | esp_tinyusb v2 把 `tud_mount_cb`/`tud_umount_cb` 收进组件内部，应用再定义会 `multiple definition` 链接失败 |
| 协议通道 | UART0（GPIO37 TX / GPIO38 RX）115200 8N1，JSON-lines | 数据手册实锤 UART0 引脚；GPIO37/38 虽是 strapping 但 Boot Mode 表允许任意值 |
| 日志 | 默认 `ESP_LOG_NONE` | UART0 是协议通道，日志会污染 JSON 流 |
| 安全释放 | 1500 ms 无报文自动全抬 + 退出/拔出释放 | 防止按键卡死在目标机上 |
| **芯片版本** | `ESP32P4_SELECTS_REV_LESS_V3=y` + `REV_MIN_100=y` | **实测这块板是 rev v1.3（早期硅片）**；IDF 5.5 默认按 v3.1+ 编译，两者 Kconfig 互斥，不设必烧录失败。见 hardware-facts §11.1 |
| **`release_all` 免限流** | 限流判断放在解析 `cmd` 之后，`release_all` 短路放行 | 实测 200 帧风暴后 `release_all` 被 `rate_limited` 拒绝 = 唯一能抬键的命令失效。见 protocol §9.6 |
| **RX 用 UART 事件队列** | `xQueueReceive` + `uart_read_bytes(..., ticks_to_wait=0)` | IDF 的 `uart_read_bytes` 会为每个缺失字节各等一次超时；256 字节缓冲 + 20 ms 超时让 ping 多花 20 ms。见 protocol §9.7 |
| Flash | 16 MB / 80 MHz / QIO | `--flash_mode dio` 是正常的：IDF 源码注释明确"烧录用 dio，bootloader 启动后升级 quad" |

---

## 已知缺口（诚实版）

1. **绝对坐标鼠标**：HID boot mouse 只能相对移动，没有"直接跳到 (x,y)"。要的话得加绝对定位 HID。
2. **C6 Wi-Fi / UDP 通道**：`pc/transport_tcp.py` 是明确报错的 stub，固件只做 UART。
3. **Consumer control（音量/媒体键）**：未做。
4. **限流窗口是固定 1 秒桶**：跨桶边界的连发可能不触发限流（实测 250 帧后紧跟的 5 条普通命令全放行）。不是安全缺陷（`release_all` 免限流），但要严格限速得改滑动窗口。见 protocol §7.2。
5. **`key_tap` 的 ack 含 hold_ms**：默认 30 ms，所以 ack ≈ 42 ms 是设计使然；测链路延迟请用 `ping`/`mouse_move`。
6. **只插一个口会"半身不遂"**：UART 口=脑（指令），高速口=手（HID）。`host_mounted=False` 就是没插高速口。
7. **首次开串口会复位板子一次**：CH340 的 DTR/RTS 抖动导致，PC 侧已对只读命令补发一次，会动键鼠的命令绝不重发。
8. **仅图片可验证项**：三个 Type-C 口的一一对应、1 A 保险丝、双口供电接法、下载模式的物理实现方式 —— 本 AI 无视觉能力，未复核原理图，见 hardware-facts §2/§6.2/§1.5。

---

## 本机环境

| 项 | 值 |
|---|---|
| ESP-IDF | `D:\esp\idf-full\esp-idf-v5.5.6`（**官方完整包**，含 `.git`+全部 submodule） |
| 工具链 | `D:\esp\.espressif`（riscv32-esp-elf 14.2.0 / cmake 3.30.2 / ninja 1.12.1 / openocd） |
| Python env | `D:\esp\python_env` |
| PC 端解释器 | `%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（3.12.14 + Pillow 12.3.0） |
| Git | `C:\Program Files\Git\cmd`（2.55.0.5） |

> **坑 1**：GitHub 自动源码 tarball 不含 submodule（`tlsf`/`cJSON`/`micro-ecc` 全空）→ 必然编译失败，必须用官方 `esp-idf-v5.5.6.zip`（2 GB）。
> **坑 2**：`python` 必须解析到 `D:\esp\python_env\Scripts\python.exe`，否则 `idf.py` 报 `No module named 'click'`。
> **坑 3**：本机 PowerShell 执行策略禁止 dot-source `.ps1`，用 `tools\idf.cmd` 或先 `Set-ExecutionPolicy -Scope Process Bypass`。
> **坑 4**：这块板是 **P4 rev v1.3**，必须 `CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`，否则 `bootloader.bin requires chip revision in range [v3.1 - v3.99]`。**别用 `esptool --force` 绕过**（会缺 errata 补丁）。
> **坑 5**：改 `sdkconfig.defaults` 后必须**删掉 `sdkconfig`** 再 build，否则旧值不会被覆盖。
> **坑 6**：商家 RAR 里的 CH340 驱动是 2009 年**无签名** INF，Win11 拒装。用 Microsoft Update Catalog 的 `wch.cn - Ports - 3.9.2024.9`（签名有效），见 hardware-facts §11.2。

---

## 上游与出处

- 上游项目：`BSGGPRO/esp32p4-iphone-agent`（源自 `onehumanbeing/esp32-iphone-agent`，从控 iPhone 泛化为控任意 Windows 电脑）
- 商家资料：`%USERPROFILE%\Downloads\JC-ESP32P4-M3-DEV`（原理图/规格书/手册/例程/烧录工具）
- License: MIT