# JC-ESP32P4-M3 上电 · 烧录 · 验收手册（v1.0）

> 目标：从"板子插上电脑"到"AI 通过串口让这块板子当键鼠操作电脑"，每一步都有可判定的通过标准。
> 所有硬件事实的出处见 [hardware-facts.md](hardware-facts.md)（证据分级）；接口契约见 [protocol.md](protocol.md) v1.0.1。
> 固件：`firmware/`（ESP-IDF v5.5.6 / esp32p4 / esp_tinyusb v2）；PC 端：`pc/`（零依赖 Python）。

---

## 0. 三条线的接线（先看清楚再动手）

```
                    ┌──────────────── JC-ESP32P4-M3 ────────────────┐
   本机 USB ────────┤ 转 UART 口 (CH340C → UART0 GPIO37/38)          │  烧录 + 协议通道 + 供电
                    │                                                │
   本机 USB ────────┤ 高速 Type-C (ESP_USB_P/N → P4 USB-OTG HS)     │  HID 键鼠（插谁控谁）
                    └────────────────────────────────────────────────┘
```

- **转 UART 口**：一条线同时负责「烧录」「协议指令」「日志/供电」。本机识别为 CH340（`VID_1A86&PID_7523`）。
- **高速 Type-C**：插到**要被控制的那台电脑**。插本机 = 板子控制本机（端到端自测就是这么做）。
- 供电：两个口都插着即可（板载 F2 保险丝限 1 A，P4 满载 350–600 mA，够用）。**1 A 限流与双口供电接法属【无法验证（仅图片）】**，见 hardware-facts §6.2。

> ⚠️ 别把「全速 Type-C」当数据口用：那是 P4 的 USB 1.1 FS 口，本项目 HID 走**高速口**。

---

## 1. 装 CH340 驱动（一次性，需要管理员）

本机当前状态：设备管理器里是 `USB Serial`、Class=Unknown、**没有 COM 口** → 驱动未安装。

```
右键 → 以管理员身份运行：
  esp32p4-hid-agent\tools\install-ch340-admin.cmd
```

脚本做四步：`pnputil /add-driver ... /install` → `/scan-devices` → 再装一次（扫描后设备才匹配得上）→ 打印当前串口。

**驱动来源（重要）**：商家 RAR 里那份 `CH341SER.INF` 是 **2009 年的 3.1.2009.06、没有数字签名**，Windows 11 会直接拒装并报
`无法添加驱动程序包: 第三方 INF 不包含数字签名信息`。所以本项目改用 **Microsoft Update Catalog 里的签名版本**：

| 项 | 值 |
|---|---|
| 包名 | `wch.cn - Ports - 3.9.2024.9`（Catalog 搜索 `CH340`，2024-09 那条） |
| 版本 | `09/16/2024, 3.9.2024.09` |
| 类 | `Class = Ports`（正经串口类，不是 Other hardware） |
| 签名 | `CH341SER.sys` → Status **Valid**，`CN=Microsoft Windows Hardware Compatibility Publisher` |
| 本地位置 | `D:\esp\ch340-signed\`（`CH341SER.INF` + `CH341SER.CAT` + `*.sys`/`*.dll`，9 个文件） |

下载方式（无需管理员，已由 Lead 完成，记录以便复现）：在 `https://www.catalog.update.microsoft.com/Search.aspx?q=CH340`
取最新一条的 GUID → POST `DownloadDialog.aspx` → 得到 `catalog.s.download.windowsupdate.com/.../*.cab` → `expand.exe xxx.cab -F:* D:\esp\ch340-signed`。

**通过标准**：`[System.IO.Ports.SerialPort]::GetPortNames()` 里多出一个 COM 口（通常 COM6/COM7），
且设备管理器「端口(COM 和 LPT)」下出现 `USB-SERIAL CH340 (COMx)`。
没出现就拔插一次板子再跑一遍脚本。

> 备选（不用脚本）：设备管理器 → 那个 `USB Serial` → 更新驱动程序 → **自动搜索驱动程序**，
> Windows Update 上架的正是同一个 3.9.2024.9 签名版。
> **不要**选"从磁盘安装"去指 `D:\esp\ch340-driver`（那是 2009 未签名版，必然失败）。

---

## 2. 烧录

### 2.1 推荐：`idf.py flash`（一条命令，地址自动）

```powershell
cd <repo>\firmware
cmd /c "..\tools\idf.cmd -p COM7 flash"
```

（把 `COM7` 换成第 1 步实际出现的口。`..\tools\idf.cmd` 是已封装好环境的包装器，见 `tools/idf.cmd`。）

### 2.2 备用：esptool 手工烧（地址有日志级证据）

```powershell
cmd /c "..\tools\idf.cmd -p COM7 -b 460800 flash"
# 等价于：
# esptool --chip esp32p4 -p COM7 -b 460800 --before default_reset --after hard_reset \
#   write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
#   0x2000 build\bootloader\bootloader.bin \
#   0x8000 build\partition_table\partition-table.bin \
#   0x10000 build\esp32p4_hid_agent.bin
```

### 2.3 烧录参数速查（全部有出处，见 hardware-facts §1.3）

| 参数 | 值 | 说明 |
|---|---|---|
| 连接波特率 | 115200 | 13 份商家烧录日志全部 `CONNECT BAUD: 115200` |
| bootloader | `0x2000` | 日志实测 |
| 分区表 | `0x8000` | 日志实测 + `CONFIG_PARTITION_TABLE_OFFSET=0x8000` |
| 应用 | `0x10000` | 日志实测 |
| Flash | 16 MB / 80 MHz / QIO | 规格书 + sdkconfig；**esptool 仍以 `dio` 写入是正常的**——IDF 源码注释明确写：烧录用 dio，bootloader 启动后自行升级到 quad |
| 应用镜像 | `esp32p4_hid_agent.bin` = **329,904 B** | 默认 singleapp 分区（1 MB）装得下 |
| 进下载模式 | GPIO35=0 且 GPIO36=1（Joint Download Boot） | P4 数据手册 Table 3-3；**板上物理实现（BOOT 键/焊盘）属【无法验证（仅图片）】** |
| 商家习惯 | 断电重新上电即可再次进烧录态 | 用户手册第 7 页 |

> 若 `idf.py flash` 报 "Failed to connect"：先按商家习惯**断电重上电**再试；仍不行则需要手动把 GPIO35 拉低（看原理图确认板上的实现方式）。

### 通过标准
- 烧录最后一行出现 `Hash of data verified.` / `Leaving... Hard resetting via RTS pin...`
- **高速口插到目标电脑后，设备管理器出现 `P4 HID Bridge`（HID 键盘 + HID 鼠标各一个）**

---

## 3. 串口协议自检（固件 → PC 通道）

```powershell
$PY = 'C:\Users\lu''hao\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
cd <repo>\pc

& $PY agent_bridge.py --list-ports          # 认出 CH340 那行
& $PY agent_bridge.py ping   --port COM7    # 期望 ok:true, proto:"1.0", host_mounted:true
& $PY agent_bridge.py status --port COM7    # 期望 host_mounted:true
```

**通过标准**：`ping` 往返 < 30 ms；`status.host_mounted == true`（false = 高速口没插到主机 / 主机没枚举）。

---

## 4. 端到端验收（真正"AI 看屏 → 动手 → 再验证"）

```powershell
# 鼠标右移 200 像素，前后各截一屏做差分
& $PY agent_bridge.py e2e-demo --dx 200 --port COM7

# 键盘：Win+R 打开运行框（能看到就说明键盘通了）
& $PY agent_bridge.py key-combo leftgui r --port COM7
& $PY agent_bridge.py text "notepad" --port COM7
& $PY agent_bridge.py key-tap enter --port COM7
```

**通过标准**（对应 roadmap 第五阶段检查点）：

| # | 检查点 | 通过标准 | 判据 | 实测（2026-10-17） |
|---|---|---|---|---|
| 1 | 手（枚举） | 目标机出现 HID 键盘 + 鼠标 | 设备管理器 `HID Keyboard Device` + `HID-compliant mouse` | ✅ `VID_303A&PID_4004` COL01/COL02，序列号 = MAC `80F1B2D39CE6` |
| 2 | 眼（截屏） | 能截出真实桌面 | `screenshot` 出 PNG，尺寸=屏幕 | ✅ 2560×1440 / 821.9 KiB |
| 3 | 手（鼠标） | `mouse-move` 后光标真的动 | `e2e-demo` 报"有变化"且光标位置改变 | ✅ 请求 (120,60) → `GetCursorPos` 实测 +199,+23 |
| 4 | 手（键盘） | `text` 能输入字符 | 记事本里出现文字 | ✅ `GetAsyncKeyState` 独立取证：SHIFT/LWIN 按下与抬起均生效 |
| 5 | 闭环 | 截屏 → 指令 → 屏幕变化 → 再截屏确认 | `e2e-demo` exit 0 + 变化像素 > 0 | ✅ 7037 px / 0.19%，`E2E OK: change detected` |
| 6 | 安全 | 停发报文后按键自动抬起 | `safety_release` | ✅ 静默 2.2 s 后 ctrl 已抬起 |

> **延迟实测**（`ping` n=40）：mean 18.4 ms / p95 19.1 ms；`mouse_move` ack n=30：mean 12.5 ms；连发 332 条/秒。
> 复测脚本：`python D:\esp\perf.py <COM口>`（Lead 侧临时脚本，未纳入仓库）。
> **`key_tap` 的 ack ≈ 42 ms 属正常**：它含默认 `hold_ms=30`。

---

## 5. 安全机制（别忽略）

- **安全释放**：有键/按钮按下时，若 1500 ms 内没收到任何 PC 报文，固件自动全抬并发 `{"evt":"safety_release"}`。可用 `set safety_timeout_ms` 调（0–60000，0=关闭）。
- **退出即释放**：PC 端进程退出（含 Ctrl+C）会发 `release_all`。
- **断开即释放**：拔掉高速口，固件清空按键状态并发 `hid_unmounted`。
- 想跨进程保持按住：`--no-release-on-exit`（见 pc/README §4），用完务必 `release-all`。

---

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 设备管理器只有 `USB Serial`、无 COM 口 | CH340 驱动未装 | 跑 `tools\install-ch340-admin.cmd`（管理员） |
| `ping` 超时 / 无响应 | 固件没跑起来，或串口被 monitor 占用 | 关掉 `idf.py monitor`；确认烧的是本工程固件 |
| `ping` 返回乱码行被丢弃后仍无 JSON | 波特率不对 | 确认 115200 8N1 |
| `host_mounted:false` | 高速口没插目标机 / 只插了供电口 | 把**高速 Type-C** 插到目标电脑 |
| 设备管理器出现"未知 USB 设备(设备描述符请求失败)" | 高速口供电/线材问题 | 换数据线；两个口都插上；避免只用转 UART 口供电去带 HID |
| `mouse-move` 有效但移动距离不对 | 目标机指针加速 | 这是相对移动的固有特性，需要绝对定位请见下节"未做" |
| 烧录 `Failed to connect` | 没进下载模式 | 断电重上电；必要时 GPIO35 拉低 |

---

## 7. 已知未验证 / 未实现（诚实清单）

**未验证（依赖原理图 PNG，本 AI 无视觉能力）**
- 三个 Type-C 口的**一一对应关系**、1 A 保险丝、双口供电接法、进下载模式的物理方式 → 见 hardware-facts §2、§6.2、§1.5。

**未实现（v1 范围外）**
- **绝对坐标鼠标**：HID boot mouse 只有相对移动，无法"直接跳到某坐标"。需要的话要加绝对定位 HID（digitizer/绝对鼠标）报告描述符。
- **C6 Wi-Fi/UDP 通道**：`pc/transport_tcp.py` 目前是明确报错的 stub；固件只做 UART。
- **Consumer control（音量/媒体键/home）**：原 iOS 项目的 `homescreen` 依赖 Consumer 页，本项目未加。
- **真板延迟实测**：`ping` 往返、`mouse_move` 实际延迟要等板子通后才能填真数（当前只有 115200 理论值与回环值）。

---

## 8. 环境备忘（本机）

| 项 | 值 |
|---|---|
| ESP-IDF | `D:\esp\idf-full\esp-idf-v5.5.6`（v5.5.6，官方完整包含 submodule） |
| 工具链 | `D:\esp\.espressif`（riscv32-esp-elf 14.2.0 / cmake 3.30.2 / ninja 1.12.1 / openocd / esp-rom-elfs） |
| Python env | `D:\esp\python_env` |
| 环境包装器 | `tools\idf.cmd`（cmd） / `tools\idf-env.ps1`（PowerShell，需先 `Set-ExecutionPolicy -Scope Process Bypass`） |
| PC 端解释器 | `%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（CPython 3.12.14 + Pillow 12.3.0） |

**两个已踩过的坑（别再踩）**
1. GitHub 的自动源码 tarball **不含 submodule**，`tlsf`/`cJSON`/`micro-ecc` 会全空 → 必须用官方 `esp-idf-v5.5.6.zip`（2 GB，`dl.espressif.com/github_assets/...` 国内镜像 94 MB/s）。
2. `python` 必须解析到 `D:\esp\python_env\Scripts\python.exe`，否则 `idf.py` 报 `No module named 'click'`（`tools\idf.cmd` 已把 venv 排在 PATH 最前）。