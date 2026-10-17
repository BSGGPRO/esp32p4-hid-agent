# JC-ESP32P4-M3-DEV 硬件 / 烧录事实取证（hardware-facts）

> 状态：**v1.1 取证完成（2026-10-17 19:3x）** · Owner: `hw-facts`（task-3）· 唯一写入范围：本文件
> v1.1 变更：新增 §4.3 与**实际 `firmware/` 代码**的差异对照（19:2x 快照），标出 rhport 未配置、`bInterval=10` 在 HS 下=64 ms 与 `protocol.md` §7 目标冲突、16 MB/QIO/分区表未声明三项风险。
> 用途：把项目里"靠猜"的硬件事实变成"有证据"的事实，供 `firmware/` 与烧录手册引用。
> 本文件**只记录事实与出处**，不改变 `docs/protocol.md`（Owner: Lead）的接口契约。

## 0. 证据等级定义与本次取证的限制

| 标记 | 含义 |
|---|---|
| 【文档原文】 | 直接摘自商家 PDF / TXT 文档的原文（含页码或行号） |
| 【日志实测】 | 烧录工具日志、程序运行日志里的**原始输出行** |
| 【代码原文】 | 源码 / sdkconfig / 分区表 / 配置文件里的原文行 |
| 【推断】 | 由上述证据推导，非直接陈述；已注明推导依据 |
| 【无法验证（仅图片）】 | 只存在于图片（原理图 PNG / 结构图 JPG / 截屏 PNG），**本次未复核** |

**本次取证的硬限制（必须如实告知下游）**

1. **本 agent 无视觉能力**，不能读图。所有 `5-Schematic\*.png`、`3-Structure_Diagram\5.jpg`、
   `8-Burn operation\Burn operation instructions\*.png`、PDF 内嵌的界面截图/接口表图片，
   **一律没有看过**。凡只能靠图才能确定的结论，本文标【无法验证（仅图片）】并写明"依赖项目已有结论，本次未复核"，不做任何图像内容断言。
2. 本文引用的所有文件均为**只读数据源**，未做任何修改。写入仅限本文件。
3. PDF 文本用 `pypdf 6.21.1` 抽取（工作区 venv：`_venv-pdf\`，抽取脚本 `_venv-pdf\extract*.py`，输出 `manual.txt` / `spec.txt` / `dltool_en.txt` / `p4_ds.txt` / `c6_ds.txt`）。
   抽取文本会出现 `⚶` 之类的连字符乱码（原 PDF 的 `~`/`-`），引用时已按原样标注。
4. 两处**数据源自身矛盾**已单独列出（见 §8），不要拿其中一句当唯一依据。

---

## 1. 烧录参数表

### 1.1 工具

| 项 | 值 | 证据 |
|---|---|---|
| 商家附带工具 | Flash Download Tool **v3.9.7**（`flash_download_tool_3.9.7.exe`） | 【代码原文】路径 `...\8-Burn operation\flash_download_tool_3.9.7\flash_download_tool_3.9.7.exe` |
| 该版本支持 P4 | `3.9.7：` / `* support ESP32-P4` | 【文档原文】`...\flash_download_tool_3.9.7\doc\release_note.txt:1-2` |
| 工具自带文档版本 | "This document is applicable to Flash Download Tool v3.9.6." | 【文档原文】`...\doc\Flash_Download_Tool__en.pdf` 第 2 页（**与 release_note 的 3.9.7 不一致**） |
| 推荐日常工具 | `esptool` / `idf.py flash`（本项目用 ESP-IDF 5.5.6，见 `tools/idf-env.ps1`） | 【文档原文】用户手册第 6 页："In the programmer entry of the same tab, we choose "esptool"" |
| 历史烧录全部走 | 商家 Flash Download Tool（13 个日志文件，全部 `Download result: PASS`） | 【日志实测】`...\logs\*.txt` |

> 说明：商家没有提供 IDF/esptool 的烧录说明，手册是 Arduino 视角（见 §1.6）。地址映射与参数主要靠**日志实测 + sdkconfig 原文**还原。

### 1.2 历史烧录日志逐条引用（真实烧录参数的硬证据）

日志目录：`...\8-Burn operation\flash_download_tool_3.9.7\logs\`（共 13 个 `.txt`，均为 2024-12 的会话）。
**13 个文件里每一段会话都包含同样的头三行**，这是波特率的直接证据：

```
CONNECT BAUD: 115200
baud:115200
root baud:115200
```
【日志实测】例：`6055F9FAFBC6.txt:3,7,8`（其余 12 个文件同）

**关键：只有 `6055F9FAFBC6.txt` 记录了完整的分地址烧录**（其余日志都是"合并镜像写在 0x0"或整片镜像）：

| 日志文件 | 行 | 原始行（逐字引用） | 含义 |
|---|---|---|---|
| `6055F9FAFBC6.txt` | 15-17 | `get flash id : 0x001840c8` / `manufacturer_id: 0xc8` / `device_id: 0x4018` | 该板 Flash JEDEC ID |
| 同上 | **18** | `Wrote 23248 bytes (14291 compressed) at 0x00002000 in 0.5 seconds (effective 347.4 kbit/s)...` | **@0x2000 bootloader** |
| 同上 | **19** | `Wrote 3072 bytes (120 compressed) at 0x00008000 in 0.0 seconds (effective 524.3 kbit/s)...` | **@0x8000 分区表** |
| 同上 | **20** | `Wrote 4282032 bytes (1419658 compressed) at 0x00010000 in 40.7 seconds (effective 840.9 kbit/s)...` | **@0x10000 app** |
| 同上 | **21** | `Wrote 4194304 bytes (3199460 compressed) at 0x00910000 in 50.1 seconds (effective 669.2 kbit/s)...` | **@0x910000 数据分区（4 MB）** |
| 同上 | 26 | `Download result: PASS` | 结果 |

**这些地址/大小的解释（含交叉验证）**

- `0x2000` / `0x8000` / `0x10000` = ESP32-P4 的 bootloader / partition-table / app 标准偏移，与 sdkconfig 原文一致：
  - 【代码原文】`xiaozhi-esp32\sdkconfig:683` `CONFIG_PARTITION_TABLE_OFFSET=0x8000`
  - 【代码原文】`NoDisplay\uvc_camera_ov5647\sdkconfig:681` `CONFIG_PARTITION_TABLE_OFFSET=0x8000`
  - 【文档原文】商家 demo 运行日志 `withDisplay\lvgl_demo_v9\README_CN.md:83,93`：`2 factory factory app 00 00 00010000 00800000` / `Loaded app from partition at offset 0x10000`
- `0x910000` 这个偏移**正好等于 9 MB app 的结束地址**（`0x10000 + 0x900000 = 0x910000`），和商家 `withDisplay\test\partitions.csv` 的布局完全吻合：
  - 【代码原文】`withDisplay\test\partitions.csv:5-6`：`factory,  app,  factory, ,  9M,` / `storage,  data, spiffs,  ,  4M,`
  - 【推断】所以 `0x910000` 那一笔 = `storage`(spiffs, 4 MB) 分区；`4282032` 字节那笔 = `factory` app。
  - 依据：偏移算术 + 分区表尺寸 4M=`4194304` 完全相等 + 偏移 `0x910000` 完全相等。
- 该会话总覆盖区间 `0x0`–`0xD10000` = **13697024 字节**，而多个日志里"合并镜像写 @0x0"的大小恰好就是 `13697024`，两处互相印证：
  - 【日志实测】`6055F9FAFBC4.txt:18` `Wrote 13697024 bytes (4639600 compressed) at 0x00000000 in 111.0 seconds (effective 987.1 kbit/s)...`
  - 【日志实测】`6055F9FAFBC1.txt:18` 同 13697024 字节 @0x0
  - 【代码原文】工具配置 `configure\esp32p4\spi_download.conf` 的 `[ESPTOOL_PARAM]` 段：`compress = True` / `verify = True`（对应日志里的 `(… compressed)` 与 `Hash of data verified.`）
- **日志本身不记录文件名**，只记录"写入字节数 @ 地址"。上面 bootloader/app/分区表的归属是【推断】（依据：偏移 + 典型体积 + 分区表原文），不是日志明说。

**其余 12 个日志的写入事实（摘要，逐条可回溯）**

| 日志文件 | MAC（`mac l`/`mac h`） | Flash ID | 写入（原始行） | 结果 |
|---|---|---|---|---|
| `6055F9FAFBC4.txt` | `0xf9fafbc4` / `0x6055` | `0x00182085` (mfr `0x85`, dev `0x2018`) | L18 `13697024 bytes … at 0x00000000`；L43 同；L68 `4194304 bytes … 0x00000000`；L86 同；L111 `13697024 … 0x0` | PASS ×4 |
| `6055F9FAFBC1.txt` | `0xf9fafbc1` / `0x6055` | `0x00182085` | L18 `13697024 bytes … 0x0` | PASS |
| `6055F9FAFBC6.txt` | `0xf9fafbc6` / `0x6055` | `0x001840c8` | 见上表（4 笔分地址） | PASS |
| `6055F9FAFBCA.txt` | `0xf9fafbca` / `0x6055` | `0x00182085` | **无写入行**（只有 sync + `Download result: PASS`，L19-22） | PASS |
| `E806909B7068.txt` | `0x909b7068` / `0xe806` | `0x00182085` | L18 `16777216 bytes (1078132 compressed) at 0x0`；L43 `5280112 bytes … 0x0` | PASS ×2 |
| `E806909B7234.txt` | `0x909b7234` / `0xe806` | `0x00182085` | L42 `16777216 …`；L91 `617536 …`；L116 `16777216 … (2186.6 kbit/s)`；L141 `5280112 …` | PASS ×4（L18-23 为无写入会话） |
| `E806909B73DC.txt` | `0x909b73dc` / `0xe806` | `0x00182085` | L18 `4194304 … 0x0`；L43 `5280112 … 0x0` | PASS ×2 |
| `E806909B7450.txt` | `0x909b7450` / `0xe806` | `0x00182085` | L18 `4194304 … 0x0`；L43 `5280112 … 0x0` | PASS ×2 |
| `E806909B7BA4.txt` | `0x909b7ba4` / `0xe806` | `0x00182085` | L18 `16777216 … 0x0`；L43 `5280112 … 0x0` | PASS ×2 |
| `E806909B7C08.txt` | `0x909b7c08` / `0xe806` | `0x00182085` | L35 `16777216 … 0x0`；L60 `5280112 … 0x0` | PASS ×2（L18-34 无写入） |
| `9C9E6E5BA8A4.txt` | `0x6e5ba8a4` / `0xfe9c9e` | `0x00164020` (mfr `0x20`, dev `0x4016`) | L35 `Wrote 1146768 bytes (628738 compressed) at 0x00000000 in 55.3 seconds` | PASS |
| `F09E9E046648.txt` | `0x9e046648` / `0xf09e` | `0x00182085` | L18 `1737280 bytes … 0x0`；L43 `1847632 bytes … 0x0` | PASS ×2 |
| `3C8427C7FDF4.txt` | `0x27c7fdf4` / `0x3c84` | `0x00182085` | L18 `4194304 … 0x0`；L43 `4194304 … 0x0` | PASS ×2 |

**Flash 容量与厂商（从 Flash ID 推导）**

- 【日志实测】两种 ID 反复出现：`0x00182085`（mfr `0x85`, dev `0x2018`）与 `0x001840c8`（mfr `0xc8`, dev `0x4018`）。
- 【推断】JEDEC 容量字节 `0x18` → `2^24` = **16 MiB**；因此这两颗都是 16 MB。依据：JEDEC 容量字节编码规则（通用知识，非本数据源原文）+ 多份日志写入 `16777216 bytes` 整片成功。
- 【推断】`0x00164020`（dev `0x4016`，`0x16` → `2^22` = **4 MiB**）出现在 `9C9E6E5BA8A4.txt`，写入的 `1146768` 字节与商家 `Burn files\JC1060P470_C6.bin` **字节数完全相同**（`1146768`），故该日志是给**另一台 4 MB 设备（显示模组的 ESP32-C6）**烧 `JC1060P470_C6.bin`，**不是** JC-ESP32P4-M3-DEV 本体。依据：字节数完全相等（`Burn files` 目录实测）。
- 厂商名（`0x85`=Puya、`0xc8`=GigaDevice 等）属于通用知识，本数据源无原文，标【推断】且**不建议写进交付手册**。

### 1.3 烧录参数汇总表（可直接引用）

| 参数 | 实测/原文值 | 证据等级 | 出处 |
|---|---|---|---|
| 连接波特率 | **115200** | 【日志实测】 | 13 个日志全部：`CONNECT BAUD: 115200` / `baud:115200` / `root baud:115200` |
| 工具配置里的端口/波特索引 | `com_port1 = COM6` / `baudrate1 = 5` / `checkmac1 = 1` | 【代码原文】 | `configure\esp32p4\spi_download.conf` `[DOWNLOAD]` 段（`baudrate1 = 5` 是 UI 下拉索引，**不是**波特率数值，勿直接当 115200 用） |
| 串口监视波特率 | 115200 | 【代码原文】 | `xiaozhi-esp32\sdkconfig:670`、`uvc_camera_ov5647\sdkconfig:668` 均 `CONFIG_ESPTOOLPY_MONITOR_BAUD=115200` |
| 应用层串口参数（本项目） | 115200 8N1 无流控 | 【代码原文】 | `docs/protocol.md:14`（Lead 契约）；与上面厂商值一致 |
| bootloader 偏移 | **0x2000** | 【日志实测】+【推断】归属 | `6055F9FAFBC6.txt:18` |
| partition-table 偏移 | **0x8000** | 【日志实测】+【代码原文】 | `6055F9FAFBC6.txt:19`；`CONFIG_PARTITION_TABLE_OFFSET=0x8000` |
| app（factory）偏移 | **0x10000** | 【日志实测】+【文档原文】 | `6055F9FAFBC6.txt:20`；`lvgl_demo_v9\README_CN.md:83,93` |
| 数据分区示例偏移 | 0x910000（4 MB） | 【推断】（见 §1.2） | `6055F9FAFBC6.txt:21` + `withDisplay\test\partitions.csv:6` |
| Flash 大小 | **16 MB** | 【文档原文】+【代码原文】+【日志实测】 | 规格书 p4 "16MB Nor Flash integrated in module"/"Storage Space 16M"；`CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`；整片 `16777216 bytes` 写入成功 |
| Flash 频率 | **80 MHz** | 【代码原文】+【日志实测（非本板）】 | `CONFIG_ESPTOOLPY_FLASHFREQ_80M=y` / `CONFIG_ESPTOOLPY_FLASHFREQ="80m"`；`lvgl_demo_v9\README_CN.md:75` `SPI Speed : 80MHz` |
| Flash 模式 | **QIO**（但同一 sdkconfig 里字符串是 `"dio"`，见 §8 矛盾 1） | 【代码原文】+【日志实测（非本板）】 | `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`；`lvgl_demo_v9\README_CN.md:76` `SPI Mode : QIO`、`:136` `spi_flash: flash io: qio` |
| 烧录工具是否改模式 | `[ESPTOOL_PARAM]` 里 `flash_size = keep` / `flash_freq = keep` / `flash_mode = keep`；`spicfgdis = 1`（SPI 配置界面被禁用） | 【代码原文】 | `configure\esp32p4\spi_download.conf` `[FLASH_CRYSTAL]`/`[ESPTOOL_PARAM]`/`[ESPTOOL_PARAM]` |
| 该配置的原始值 | `spispeed = 3` / `spimode = 0` | 【代码原文】（**含义未验证**） | 同上；这两个是工具 UI 的下拉索引，本 agent 无法在无 GUI 情况下确认映射，标【推断待确认】 |
| 写入前复位 / 写入后 | `before = default_reset` / `after = no_reset` | 【代码原文】 | 同上 `[ESPTOOL_PARAM]` |
| 校验 / 压缩 | `verify = True` / `compress = True` | 【代码原文】 | 同上；对应日志 `Hash of data verified.` |
| 结果 | 13 个日志全部 `Download result: PASS` | 【日志实测】 | `logs\*.txt` |

> **日志里没有的东西（不要凭空补）**：烧录日志**不含** Flash 模式、Flash 大小、SPI 频率的配置字样。
> 这些只能从 sdkconfig / 运行日志得到；且唯一的 QIO/80 MHz 运行日志来自商家**另一块板**（见下）。

### 1.4 关于 `lvgl_demo_v9` 日志的适用性（重要限定）

`withDisplay\lvgl_demo_v9\README_CN.md` 里的完整启动日志（QIO / 80 MHz / 16 MB / 32 MB PSRAM / 360 MHz）
**来自 ESP32-P4-Function-EV-Board**，不是本板实测：

- 【文档原文】`lvgl_demo_v9\README_CN.md:11`："一块 ESP32-P4-Function-EV-Board 开发板。"
- 【文档原文】同上 `:12,17-22`：屏幕适配板引脚接到 `GPIO26`(PWM) / `GPIO27`(LCD_RST)。

因此本文件里所有引用该日志的条目，等级写【日志实测】但**必须**带上"（来源为 P4-Function-EV-Board 示例，非 JC-ESP32P4-M3-DEV 实测）"。
对本板而言，QIO/80 MHz 目前是**推断成立**（sdkconfig 原文 + 同芯片家族日志），**不是本板实测**。

### 1.5 进入下载模式（P4 与手册/工具文档的差异）

- 【文档原文】ESP32-P4 数据手册 `esp32-p4_datasheet_en.pdf`（抽取文本 `p4_ds.txt:1448-1454`）Table 3-3 Boot Mode Control：
  ```
  GPIO35⚶GPIO38 control the boot mode after the reset is released.
  Boot Mode            GPIO35  GPIO36  GPIO373  GPIO383
  SPI Boot             1       Any value  Any value  Any value
  Joint Download Boot  0       1          Any value  Any value
  ```
  → **GPIO35 = 0 且 GPIO36 = 1** 进入 Joint Download Boot；`GPIO37/38` 取值任意（对本项目好消息：把 UART0 放在 GPIO37/38 不影响启动模式）。
- 【文档原文】同上 `p4_ds.txt:1395`："Strapping pin: GPIO35, GPIO36, GPIO37 and GPIO38"。
- 【文档原文】同上 `p4_ds.txt:1473`："In Joint Download Boot mode, users can download binary files into flash using USB, UART0, or SPI slave interface."
- 【文档原文】同上 `p4_ds.txt:1456-1461` 下载方式细分：USB Download Boot（USB-Serial-JTAG / USB 2.0 OTG）、UART Download Boot、SPI Slave Download Boot。
- 【文档原文】Flash Download Tool 文档 `Flash_Download_Tool__en.pdf` 第 9 页 §3.1 只给了 ESP32/S2/S3（`pull GPIO0 low`）与 ESP32-C3（`pull GPIO9 low and GPIO8 high`）的进下载模式步骤，**完全没有 ESP32-P4**；第 5 页只说 "other chip types support both UART and USB"。
- 【文档原文】商家用户手册第 7 页：「if we want to upload a new program, wee need to reset the power of the device, in order to guarantee that it enters flashing mode again.」→ 商家自己的操作习惯是**断电重上电**来重新进入烧录态。
- 【无法验证（仅图片）】"板上到底是 BOOT 按键、还是需要短接 GPIO35 焊盘进入下载模式" —— 只能看原理图 PNG（`4_ESP32P4.png`、`8_OTHER.png`）。**依赖项目已有结论，本次未复核。**
- 【代码原文 / 反例】商家板级配置里 `BOOT_BUTTON_GPIO = GPIO_NUM_21`（`xiaozhi-esp32\main\boards\guition-jc-esp32p4-m3-dev\config.h:22`）—— GPIO21 **不是** strapping 脚，所以这个"BOOT 键"**不是** ROM 的下载模式按键，只是应用按键。【推断】因此烧录时"GPIO35 拉低"这一步在本板上**很可能另有实现**（跳线/焊盘/自动下载电路），需看图确认。

### 1.6 串口与"转 UART 口"

- 【文档原文】`8-Burn operation\烧录说明：Burning instructions.txt:1-2`（全文仅 2 行）：
  ```
  1、电脑需要安装CH340驱动程序：The computer needs to install the CH340 driver program
  2、保证供电充足，开发板电流500ma以上：Ensure sufficient power supply, with a development board current of over 500mA
  ```
- 【文档原文】用户手册第 7 页示例代码 `Serial.begin(115200);`（Arduino 视角）。
- CH340 驱动安装包**确实在资料里**：`7-Character&Picture_Molding_Tool\USB-SERIAL CH340.rar`。
- 【代码原文】本项目把 UART0 用在 GPIO37/38：`esp32p4-hid-agent\docs\protocol.md:13`「通道 UART0（板子"转 UART 口" / CH340C） | GPIO38 = RXD，GPIO37 = TXD（P4 侧交叉）」。
  该引脚与 P4 数据手册默认值**一致**（见 §3），所以"GPIO37/38 是 UART0"这件事有手册级证据，**不是猜的**。
- 【无法验证（仅图片）】CH340C 挂在哪个 Type-C 口、口上丝印叫什么 —— 仅原理图 `5_USB&485.png`。

---

## 2. 三个 Type-C 口归属

**结论：归属无法由文字证据确定，唯一能确定的是"板上存在与 CH340 / USB / RS-485 相关的三处接口电路"。**

| 待定事实 | 现有证据 | 等级 |
|---|---|---|
| 板上共有几个 Type-C 口、各自丝印 | 无文字证据 | 【无法验证（仅图片）】—— 依赖项目已有结论，本次未复核 |
| 口 1「USB-UART / CH340」存在，用于烧录与串口 | `烧录说明…txt:1` 要求装 CH340 驱动；本项目 UART0 走"转 UART 口"（`protocol.md:13`） | 【文档原文】+【代码原文】（"必须装 CH340"成立；"它就是某个 Type-C"是【推断】） |
| 口 2「原生 USB（P4 USB OTG 2.0 HS）」存在 | 规格书 p3「In addition, USB OTG 2.0 HS is supported.」；p4「Interfaces CSI/DSI/USB2.0」；P4 手册 OTG_HS 章节 | 【文档原文】 |
| 口 3「USB ↔ RS-485」相关电路存在 | 原理图**文件名** `5_USB&485.png`（"USB & 485"同页） | 【推断】（仅凭文件名；页面内容未复核） |
| 三个 Type-C 与上述三者的**一一对应关系** | 无 | 【无法验证（仅图片）】—— 依赖项目已有结论，本次未复核 |
| 规格书第 5 页"Interface Description"接口表 | 该页 PDF **抽不出任何文字**，只有图片 | 【无法验证（仅图片）】 |

**写手册时必须这样表述**（建议模板）：

> 板上 Type-C 口的功能归属以原理图 `5-Schematic\5_USB&485.png` 为准（本项目未复核图片）。
> 已知可确证的只有：①存在需要 CH340 驱动的串口通道（用于烧录/日志/UART0 协议）；②P4 原生 USB 2.0 HS OTG 可用。
> **不要在手写手册里断言"左边那个口是 UART、右边那个是 OTG"**，除非有人现场看图/实测确认。

---

## 3. 占用 GPIO 表 + 剩余可用 GPIO

### 3.1 已占用 / 已消耗 GPIO

| GPIO | 用途 | 等级 | 出处 |
|---|---|---|---|
| 7 | I2C1 SDA（ES8311 音频 codec + GT911 触摸 + I2C 背光 0x45，同一总线） | 【代码原文】 | `main\boards\guition-jc-esp32p4-m3-dev\config.h:16`；`esp32p4-m3-dev.cc:89`（`.i2c_port = I2C_NUM_1`）；`ARDUINO-DEMO\jc_esp32p4_m3_lvgl_v8\pins_config.h:9` `TP_I2C_SDA 7` |
| 8 | I2C1 SCL（同上） | 【代码原文】 | `config.h:17`；`esp32p4-m3-dev.cc:90`；`pins_config.h:10` `TP_I2C_SCL 8`；`uvc…\sdkconfig:690-691` `MIPI_CSI_SCCB_I2C_SCL_PIN=8` / `SDA_PIN=7` |
| 9 | I2S DOUT（P4 → codec） | 【代码原文】 | `config.h:13` `AUDIO_I2S_GPIO_DOUT GPIO_NUM_9` |
| 10 | I2S WS | 【代码原文】 | `config.h:10` |
| 11 | 音频功放使能 PA | 【代码原文】 | `config.h:15` `AUDIO_CODEC_PA_PIN GPIO_NUM_11` |
| 12 | I2S BCLK | 【代码原文】 | `config.h:11` |
| 13 | I2S MCLK | 【代码原文】 | `config.h:9` |
| 21 | 应用 BOOT 按键（**非**下载模式脚） | 【代码原文】 | `config.h:22` `BOOT_BUTTON_GPIO GPIO_NUM_21`；`esp32p4-m3-dev.cc:247` |
| 21 | 同文件又声明为触摸 INT（**冲突**） | 【代码原文】 | `config.h:49` `LCD_TOUCH_INT GPIO_NUM_21` ← 与 `config.h:22` 同脚 |
| 22 | 触摸 RST（声明了但**代码里没用**） | 【代码原文】 | `config.h:48` `LCD_TOUCH_RST GPIO_NUM_22`；但 `esp32p4-m3-dev.cc:109-110` 实际传 `.rst_gpio_num = GPIO_NUM_NC, .int_gpio_num = GPIO_NUM_NC` |
| 48 | I2S DIN（codec → P4） | 【代码原文】 | `config.h:12` `AUDIO_I2S_GPIO_DIN GPIO_NUM_48` |
| 37 | **UART0 TXD**（P4→CH340） | 【文档原文】+【代码原文】 | P4 数据手册 `p4_ds.txt:981` `69 GPIO37 UART0_TXD_PAD O`；`p4_ds.txt:2446-2448`；`docs\protocol.md:13` |
| 38 | **UART0 RXD**（CH340→P4） | 【文档原文】+【代码原文】 | `p4_ds.txt:982` `70 GPIO38 UART0_RXD_PAD I1`；`protocol.md:13` |
| 35 | **strapping**（下载模式；SPI Boot 需 =1） | 【文档原文】 | `p4_ds.txt:1395,1449-1454` |
| 36 | **strapping**（下载模式需 =1；同时是 ROM 打印门控） | 【文档原文】 | 同上；`p4_ds.txt:1490-1501` Table 3-5 |
| 14,15,16,17 | ESP32-C6（WiFi6）SDIO **D0–D3** | 【代码原文】+【推断】 | `xiaozhi-esp32\sdkconfig:2528-2531` `…SDIO_PIN_D0_SLOT_1=14` … `D3=17`（**注意该 sdkconfig 的板型是 JC8012P4A1，见 §8 矛盾 3**） |
| 18 | C6 SDIO **CLK** | 【代码原文】+【推断】 | 同上 `:2527` `…PIN_CLK_SLOT_1=18` |
| 19 | C6 SDIO **CMD** | 【代码原文】+【推断】 | 同上 `:2526` `…PIN_CMD_SLOT_1=19` |
| 54 | C6 SDIO **RESET_SLAVE** | 【代码原文】+【推断】 | 同上 `:2524,2532` `…RESET_SLAVE_GPIO_MIN=54` / `…GPIO_RESET_SLAVE=54` |
| 26, 27 | P4 内部 FS USB 收发器对 `USB1P1_N1/P1`；数据手册说 **FS OTG 默认用 GPIO26/27** | 【文档原文】 | `p4_ds.txt:1116-1117`（`55 GPIO26 USB1P1_N1` / `56 GPIO27 USB1P1_P1`）；`p4_ds.txt:2757-2760`「The USB 2.0 Full-Speed OTG interface can use each of them. By default, the pins are multiplexed with GPIO26⚶GPIO27」 |
| 24, 25 | 内部 FS USB 收发器对 `USB1P1_N0/P0`；数据手册说 **USB Serial/JTAG 默认用 GPIO24/25** | 【文档原文】 | `p4_ds.txt:1114-1115`；`p4_ds.txt:2784-2786` |
| 49, 50（**专用脚，非 GPIO**） | `USB_DM` / `USB_DP`，供电 `VDD_USBPHY`，属 **USB 2.0 OTG HS PHY** | 【文档原文】 | `p4_ds.txt:781-782`「49 USB_DM Dedicated VDD_USBPHY」「50 USB_DP Dedicated」；`p4_ds.txt:3467-3468`「USB2 OTG PHY DM/DP」 |
| 26（旁证） | 实测"GPIO26 不可用，可能与其他功能冲突" | 【日志实测（非本板）】 | `lvgl_demo_v9\README_CN.md:142` `W (1250) ledc: GPIO 26 is not usable, maybe conflict with others` |

**MIPI-DSI / MIPI-CSI 不占 GPIO**（走专用 PHY/LDO）：
- 【代码原文】`config.h:42,45-46`：`LCD_MIPI_DSI_LANE_NUM (2)`、`MIPI_DSI_PHY_PWR_LDO_CHAN (3)`、`MIPI_DSI_PHY_PWR_LDO_VOLTAGE_MV (2500)`。
- 【代码原文】`esp32p4-m3-dev.cc:167-192`：`dpi_clock_freq_mhz = 80`(:169)、`.h_size = 800` / `.v_size = 1280`(:173-174)、`.lane_num = 2`(:192)、`use_dma2d = true`(:183)；面板驱动 JD9365（`:205 esp_lcd_new_panel_jd9365`）。
- 【代码原文】`config.h:36-37`：`PIN_NUM_LCD_RST = GPIO_NUM_NC`、`PIN_NUM_BK_LIGHT = GPIO_NUM_NC` → **LCD 复位和背光都不占 GPIO**。
- 【代码原文】背光走 I2C：`esp32p4-m3-dev.cc:43-47`，`uint8_t i2c_address = 0x45;`、`uint8_t reg = 0x96;`（`CONFIG_LCD_TYPE_800_1280_10_1_INCH_A` 分支）。
- 【日志实测（非本板）】同类板 `lvgl_demo_v9\README_CN.md:143` `MIPI DSI PHY Powered on`。

### 3.2 剩余的候选用 GPIO（**供本 HID 项目**）

前提与免责：
1. 下面这些是「**在本次取证的所有数据源里都没有被引用**」的 P4 GPIO ⇒ 等级【推断（未被任何数据源引用）】。
2. **"没被引用"≠"物理上是空脚"**：原理图（图片）本次未复核，模块排针是否引出需现场确认。
3. 依据（可用于交叉验证的一个弱证据）：原理图符号库 `5-Schematic\PCB package library\JC-ESP32P4-M3.SCHLIB` 的二进制字符串里出现的 GPIO 名为
   `GPIO0–GPIO13, GPIO20–GPIO23, GPIO26–GPIO54`，
   **缺 GPIO14–GPIO19 与 GPIO24–GPIO25**（等级【推断】，方法：binary strings 提取，非图像）。
   与 §3.1 中"C6 走 SDIO 用 14–19""USB 用 24/25"一致 —— 可作为"这些脚在模块内部已被消耗"的**旁证**。

| 候选 GPIO | 说明 | 等级 |
|---|---|---|
| **20, 23** | 未被任何数据源引用（注意：`guition-jc8012p4a1` 那块**别的板**用 20 做 PA、23 做背光，本板不是） | 【推断（未被引用）】 |
| **28, 29, 30, 31, 32, 33, 34** | 未被引用；注意 GPIO28–38 与 SPI2 有复用（数据手册 `p4_ds.txt:2534`），做 UART/GPIO 无妨 | 【推断（未被引用）】 |
| **39, 40, 41, 42, 43, 44, 45, 46, 47** | 未被引用 | 【推断（未被引用）】 |
| **51, 52, 53** | 未被引用 | 【推断（未被引用）】 |
| 0, 1, 2, 3, 4, 5, 6 | 未被引用，但 GPIO0–6 常见用于外部功能（本板未知），置信度更低 | 【推断（未被引用，低置信）】 |

**明确不建议用**（已被板级功能吃掉或语义敏感）：
- 7/8（I2C）、9/10/11/12/13/48（I2S+PA）、21（按键/触摸 INT）、22（触摸 RST 声明）、37/38（本项目 UART0 协议通道）
- 35/36（strapping，动了会影响启动模式）、14–19+54（C6 SDIO）、24/25/26/27（USB 相关）、49/50（专用 USB 脚，不是 GPIO）

> **给 firmware-dev 的一句话**：本项目的 UART0 固定 GPIO37(TXD)/GPIO38(RXD)，与 P4 默认 UART0 复用完全一致，不需要 GPIO 矩阵重映射；
> 除了 UART 和原生 USB，本项目**不需要再动任何 GPIO**，§3.2 的清单只是"万一要加 LED/按键时的安全区"。

---

## 4. USB-OTG 证据（P4 设备模式）

### 4.1 硬件能力（数据手册原文）

- `p4_ds.txt:2673-2676`（§4.2.2.9 USB 2.0 High-Speed OTG）：
  「The ESP32-P4 chip features a USB 2.0 High-Speed On-The-Go peripheral (OTG_HS) with an integrated …」/
  「The interface supports USB 2.0 High-Speed mode (480 Mbit/s), Full-Speed mode (12 Mbit/s), …」
- `p4_ds.txt:2699`：「Integrated UTMI High-Speed transceiver」
- `p4_ds.txt:2757-2760`：两对 FS 收发器复用在 `GPIO24/25` 与 `GPIO26/27`，FS OTG 默认 `GPIO26/27`，D+/D- 可交换。
- `p4_ds.txt:781-782,3467-3468`：HS PHY 走**专用** `USB_DM`(pin49)/`USB_DP`(pin50)，电源 `VDD_USBPHY`（`p4_ds.txt:3194` 推荐 2.97/3.3/3.63 V）。

### 4.2 商家 P4 USB **设备模式** demo 的 sdkconfig 原文（`uvc_camera_ov5647`）

来源：`...\1-Demo\IDF-DEMO\NoDisplay\uvc_camera_ov5647\sdkconfig`（ESP-IDF **5.5.0**，见 `dependencies.lock:135-138`）

| 配置项原文 | 行 | 意义 |
|---|---|---|
| `CONFIG_SOC_USB_OTG_SUPPORTED=y` | 28 | SoC 支持 USB OTG |
| `CONFIG_SOC_USB_OTG_PERIPH_NUM=2` | 315 | 2 个 OTG 外设实例 |
| `CONFIG_SOC_USB_UTMI_PHY_NUM=1` | 316 | 1 个 UTMI（HS）PHY |
| `CONFIG_ESP_ROM_USB_OTG_NUM=5` | 612 | ROM 里 USB-OTG 的控制器编号 |
| `CONFIG_ESP_ROM_USB_SERIAL_DEVICE_NUM=6` | 611 | ROM 里 USB-Serial-JTAG 编号 |
| `CONFIG_USB_OTG_SUPPORTED=y` | 2183 | 使能 OTG 驱动 |
| **`CONFIG_TINYUSB_RHPORT_HS=y`** | 2325 | **TinyUSB 挂在高速 root port**（P4 设备模式的唯一实测配置） |
| `CONFIG_TUSB_VID=0x303A` | 2319 | 厂商 VID（Espressif） |
| `CONFIG_TUSB_PID=0x8000` | 2320 | 该 demo 用的 PID |
| `CONFIG_TUSB_MANUFACTURER="Espressif"` / `CONFIG_TUSB_PRODUCT="ESP UVC Device"` | 2321-2322 | 字符串描述符 |
| `CONFIG_TUSB_SERIAL_NUM="12345678"` | 2323 | 固定序列号（**非** MAC） |
| `CONFIG_UVC_MODE_ISOC_CAM1=y` | 2333 | 该 demo 用等时传输（HID 不用） |
| `CONFIG_USJ_ENABLE_USB_SERIAL_JTAG=y` | 1005 | USB Serial/JTAG 控制器使能 |
| `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y` | 1345 | 控制台**副**输出到 USB-Serial-JTAG |
| `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED=y` | 1346 | （主控制台仍是 UART，`# CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG is not set` @1341） |

**同 demo 的编译期依赖原文**（`main\idf_component.yml:1-7` + `dependencies.lock:99-134`）：

```yaml
dependencies:
  esp_video:      { version: "0.8.*" }
  usb_device_uvc: { version: "1.1.*" }
  espressif/tinyusb: { version: "0.15.0~10" }
```
锁定版本：`espressif/tinyusb 0.15.0~10`、`espressif/usb_device_uvc 1.1.2`、`espressif/esp_video 0.8.0~3`、`idf 5.5.0`。
→ 商家证明：**P4 上跑 USB 设备模式的可用组合是 `espressif/tinyusb` + `CONFIG_TINYUSB_RHPORT_HS=y`**。

### 4.3 与本项目 `firmware/` 的差异点（诚实版）

> **快照说明（时效）**：本节对照的 `firmware/` 代码是 **2026-10-17 19:2x 的快照**（`firmware-dev` 同期在写，之后可能继续变化）。
> 引用行号前请先重新 `grep` 确认。此前 19:1x 时 `firmware/` 尚不存在。

| 维度 | 商家 UVC demo（证据） | 本项目 `firmware/`（实际代码） | 结论 / 风险 |
|---|---|---|---|
| 组件路线 | 裸 `espressif/tinyusb` **0.15.0~10** + `usb_device_uvc 1.1.2`（`uvc main\idf_component.yml:6-7`、`dependencies.lock:99-134`） | `espressif/esp_tinyusb: "^2.0.0"`（`firmware\main\idf_component.yml:5`） | **不同路线**。商家资料里**没有** `esp_tinyusb` 的 P4 用例；P4 上唯一被实测过的组合是"裸 tinyusb + `CONFIG_TINYUSB_RHPORT_HS=y`" |
| **USB 端口选择（rhport）** | `CONFIG_TINYUSB_RHPORT_HS=y`（`uvc sdkconfig:2325`）—— 商家**显式**选了高速端口 | `firmware\sdkconfig.defaults` **没有任何** `TINYUSB_RHPORT` / 端口相关配置；只在注释里断言："esp_tinyusb's default port on ESP32-P4 is the high-speed OTG 2.0 port"（`firmware\main\hid_device.c:32-35`） | ⚠️ **最高风险项（【推断/待确认】）**：该断言**没有证据支撑**，且与商家"显式设置"的做法相反。建议在 `firmware\sdkconfig.defaults` 里显式加 `CONFIG_TINYUSB_RHPORT_HS=y`，并在 `idf.py build` 后 `grep RHPORT sdkconfig` 确认符号真实存在（若不属于 esp_tinyusb 的 Kconfig，需按其端口选项处理） |
| 设备类 | UVC（等时/ISOC） | HID（Report ID 1 键盘 / 2 鼠标，`firmware\main\hid_device.c:48-51`） | 与 `protocol.md:139-142` 一致 |
| **端点轮询间隔** | UVC 用等时，无 HID bInterval | `HID_EP_INTERVAL 10`（`firmware\main\hid_device.c:39`），且**自注释**已指出：HS 下 "2^(10-1) * 125us = 64 ms" | ⚠️ **与 `protocol.md` §7 目标冲突（【推断】，依据 USB 2.0 规范）**：HS 端口下 `bInterval=10` ⇒ **64 ms** 轮询；FS 下 ⇒ `10 × 1 ms = 10 ms`。而 `protocol.md:180` 要求 `mouse_move` 单条延迟 **< 15 ms** ⇒ 该指标**只有在 full-speed 下才可能达标**。这使"选 HS 还是 FS rhport"从"配置细节"升级为"性能指标前提" |
| VID/PID | 硬编码 `0x303A`/`0x8000`（`uvc sdkconfig:2319-2320`） | `CONFIG_TINYUSB_DESC_USE_ESPRESSIF_VID=y` + `USE_DEFAULT_PID=n` + `CUSTOM_PID=0x4005`（`firmware\sdkconfig.defaults:19-23`） | 与 `protocol.md:158` 一致；PORJECT 自己的注释说明 esp_tinyusb 自动 PID 会是 `0x4004`，故必须走 custom |
| 序列号 | `"12345678"` 固定串（`uvc sdkconfig:2323`） | `esp_read_mac(mac, ESP_MAC_BASE)` → 12 位大写 hex（`firmware\main\hid_device.c:214-219`） | 与 `protocol.md:159` 一致 |
| 字符串描述符 | `"Espressif"` / `"ESP UVC Device"` | `"JC-ESP32P4-M3 Agent"` / `"P4 HID Bridge"`（`firmware\main\hid_device.c:65-71`） | 与 `protocol.md:159` 一致 |
| 配置描述符功耗 | 未取证 | `TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 100`（`firmware\main\hid_device.c:56`） | 与 `protocol.md:160` 一致；与"板子 ≥500 mA"（`烧录说明…txt:2`）无矛盾（100 mA 是声明值，非实际功耗） |
| **协议 UART 引脚** | — | `LINK_UART_NUM = UART_NUM_0`、`LINK_TX_GPIO 37`、`LINK_RX_GPIO 38`、`uart_set_pin(...)`（`firmware\main\uart_link.c:15-17,40`） | ✅ 与 P4 数据手册 UART0 默认复用（`p4_ds.txt:981-982`）**完全一致**，无需 GPIO 矩阵重映射 |
| 日志静默 | 主 UART0 + 副 USB-Serial-JTAG 控制台（`uvc sdkconfig:1345-1346`） | `CONFIG_LOG_DEFAULT_LEVEL_NONE=y` + `CONFIG_BOOTLOADER_LOG_LEVEL_NONE=y`（`firmware\sdkconfig.defaults:8-10`）；另有 `HID_AGENT_RX_DEBUG` 开关（`firmware\main\Kconfig.projbuild:3-14`） | ✅ 与 `protocol.md:16` 一致。但注意：**ROM 的打印不受这些宏控制**（`p4_ds.txt:1487` ROM 默认把启动信息打到 UART0）⇒ PC 侧仍必须丢弃非 JSON 行（`protocol.md:18` 已要求） |
| **Flash 大小 / 模式 / 分区表** | `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`、`FLASHMODE_QIO=y`、`FLASHFREQ_80M=y`、分区表 offset `0x8000`（`uvc sdkconfig:642-660,674-682`） | `firmware\` 全目录 grep **无** `FLASHSIZE` / `FLASHMODE` / `FLASHFREQ` / `PARTITION` 命中；`firmware\CMakeLists.txt` 只 `project(esp32p4_hid_agent)` | ⚠️ **待补**：本板文档+实测为 **16 MB / QIO / 80 MHz**（§1.3），firmware 未显式声明，会落到 IDF 默认值。建议补 `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`、`CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`、`CONFIG_ESPTOOLPY_FLASHFREQ_80M=y`、`CONFIG_ESPTOOLPY_MONITOR_BAUD=115200` |
| 分区表 | `CONFIG_PARTITION_TABLE_SINGLE_APP`（`uvc sdkconfig:674`） | 无自定义分区表（用 IDF 默认 single-app） | 可接受；若要按 §5.2 固定布局，需加 `partitions.csv` + `CONFIG_PARTITION_TABLE_CUSTOM=y` |
| IDF 版本 | demo 5.5.0（`uvc dependencies.lock:135-138`）；板级 demo 文档写 `V5.5`（`IDF-DEMO\README.md:6`） | `firmware\CMakeLists.txt:2` 注释写 "ESP-IDF v5.5.x"；本机 `tools/idf-env.ps1:9` = 5.5.6 | 补丁级差异，风险低 |

> **给 firmware-dev 的行动项（按优先级）**
> 1. **端到端确认 rhport**：本板哪个 Type-C 口的原生 USB 能被主机枚举 → 决定 HS 还是 FS（§2、§10 第 4 项）。
> 2. **解决 bInterval 与 §7 目标的矛盾**：若最终是 HS 端口，`bInterval=10`(64 ms) 会让 `mouse_move < 15 ms` 不可能达成 —— 要么改契约（Lead 决策），要么把端点按 FS 描述（`full_speed_config` 已存在，见 `hid_device.c:225-227`）并确认实际枚举速度。
> 3. **显式声明 16 MB/QIO/80 MHz 与分区表偏移**（见上表末两行）。
> 4. 若走 FS 收发器，注意 GPIO24/25 与 GPIO26/27 两组是**二选一**（`p4_ds.txt:2757-2760,2784-2786`），且 GPIO26/27 与 §3.1 的冲突警示相关。

### 4.4 与本项目**不冲突**的两点（避免下游过度担心）

- 【代码原文】`firmware\main\hid_device.c:225-227` 同时提供 `full_speed_config` 与 `high_speed_config`（同一份描述符），
  这在 P4 上是有必要的（HS 端口需要 `high_speed_config`），**不是** bug。
- 【代码原文】`firmware\main\hid_device.c:351-352` 在 `hid_device_release_all()` 里先清本地状态再发全 0 报，
  与 `protocol.md:169`（上电即释放）一致 —— 与硬件无关，但与"USB 未枚举时发报是 no-op"（`hid_device.c:238-239` 注释）配合下逻辑自洽。

---

## 5. 16 MB Flash 分区布局建议

### 5.1 已确证的布局事实

- 【代码原文】`CONFIG_PARTITION_TABLE_OFFSET=0x8000`（`xiaozhi sdkconfig:683`、`uvc sdkconfig:681`）→ **分区表必须落在 0x8000**，与烧录日志 `6055F9FAFBC6.txt:19` 一致。
- 【日志实测】分区表二进制实测大小 **3072 字节**（=0xC00），app 偏移实测 **0x10000**（`6055F9FAFBC6.txt:19-20`）。
- 【代码原文】商家两套参考布局：
  - xiaozhi 16 MB（双 OTA，每槽 6 MB）：`xiaozhi-esp32\partitions\v1\16m.csv:1-8`
    ```
    nvs,      data, nvs,     0x9000,    0x4000,
    otadata,  data, ota,     0xd000,    0x2000,
    phy_init, data, phy,     0xf000,    0x1000,
    model,    data, spiffs,  0x10000,   0xF0000,
    ota_0,    app,  ota_0,   0x100000,  6M,
    ota_1,    app,  ota_1,   0x700000,  6M,
    ```
  - 显示 demo（单 app 9 MB + 4 MB spiffs）：`withDisplay\test\partitions.csv:1-6`（被 §1.2 的 0x910000 日志印证）
  - 运行日志里的另一种 8 MB/7 MB 布局：`lvgl_demo_v9\README_CN.md:81-84`
- 【文档原文】规格书 p4：「32MB PSRAM stacked in chip package, **16MB Nor Flash integrated in module**」「Storage Space 16M」。
- 【日志实测（非本板）】`lvgl_demo_v9\README_CN.md:77` `SPI Flash Size : 16MB`。

### 5.2 建议布局（本项目：单 app、无 OTA、无大文件）

本项目是 UART→HID 桥，**不需要 6 MB 的 app、不需要 OTA 双槽、不需要 4 MB spiffs**。建议：

```csv
# ESP-IDF Partition Table — JC-ESP32P4-M3-DEV HID Agent (16MB flash)
# Name,   Type, SubType, Offset,   Size,    Flags
nvs,      data, nvs,     0x9000,   0x6000,
phy_init, data, phy,     0xf000,   0x1000,
factory,  app,  factory, 0x10000,  3M,
# 0x310000 ~ 0x1000000 (约 13.9MB) 保持未分配；将来要加 spiffs/模型再扩
```
- 【推断（配置建议）】`0x10000 + 3M = 0x310000`，远小于 16 MB，给未来留 13.9 MB。
- 【代码原文】nvs `0x6000` 这个尺寸来自商家实践：`withDisplay\test\partitions.csv:3` `nvs, data, nvs, 0x9000, 0x6000,`。
- 【推断】若**要** OTA：可用 `CONFIG_PARTITION_TABLE_TWO_OTA`；但注意本项目协议是 UART 控制，OTA 会引入"回声/复位"复杂度，v1 不做更稳。
- 【代码原文/风险】`uvc sdkconfig:679` 有 `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"` 但 `:680` 实际用的是 `partitions_singleapp.csv` —— 商家 demo 存在"自定义文件名写了却没用"的情况，别被误导；本项目请显式设
  `CONFIG_PARTITION_TABLE_CUSTOM=y` + `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"`。

### 5.3 整体地址图（16 MB）

```
0x000000  (保留，ESP32-P4 镜像头/ROM 用)
0x002000  bootloader.bin        ← 日志实测 23248 B @0x2000
0x008000  partition-table.bin   ← 日志实测 3072 B  @0x8000（必须）
0x009000  nvs (0x6000)
0x00F000  phy_init (0x1000)
0x010000  factory app (建议 3M) ← 日志实测 app @0x10000
0x310000  …… 未分配（约 13.9 MB）……
0x910000  商家 demo 曾在此放 4MB spiffs storage（日志实测，本方案不用）
0x1000000 16 MB 结束
```

---

## 6. 供电与风险

### 6.1 有文字证据的部分

| 事实 | 原文 | 等级 |
|---|---|---|
| 工作电压 **5 V** | 规格书 p4 参数表「Operating Voltage 5V」 | 【文档原文】 |
| 供电电流要求 | 「2、保证供电充足，开发板电流500ma以上：Ensure sufficient power supply, with a development board current of over 500mA」 | 【文档原文】`烧录说明：Burning instructions.txt:2` |
| 烧录必须装 CH340 驱动 | 「1、电脑需要安装CH340驱动程序」 | 【文档原文】同上 `:1` |
| 驱动包位置 | `7-Character&Picture_Molding_Tool\USB-SERIAL CH340.rar` | 【代码原文】（文件存在） |
| 220 V 侧无信息 | — | 不适用 |
| 工作温度 | `-20℃~+70℃`（规格书 p4） | 【文档原文】 |
| 板子尺寸 | `92*62mm`（规格书 p4）；有线网口 RJ45、RS-485 电路（原理图 `7_RJ45.png` / `5_USB&485.png` 文件名） | 【文档原文】+【推断】 |

### 6.2 **1 A 保险丝 / 双口供电接法** —— 无法验证

- 【无法验证（仅图片）】"1 A 保险丝（自恢复保险丝 / PTC / F1）"：在**全部文字数据源**（PDF 抽取文本、TXT、sdkconfig、源码、配置）里**搜不到** `保险丝` / `fuse` / `1A` 的任何相关记录（唯一的 `eFuse` 命中全是芯片 eFuse，无关）。
  该结论只能来自原理图 `5-Schematic\1_PWR&SPEAKER.png`（892×1108）。
  **依赖项目已有结论，本次未复核**（本 agent 无视觉能力）。写手册时须写"以原理图为准"。
- 【无法验证（仅图片）】"双口供电接法（两个 Type-C 同时插 / 从哪个口供电 / 是否可同时插 UART + USB）"：无文字证据。
  可能相关：`5-Schematic\1_PWR&SPEAKER.png`、`3-Structure_Diagram\5.jpg`（800×800）。**同上，未复核**。
- 【文档原文 / 参考，非本板】`lvgl_demo_v9\README_CN.md:25-26`：「使用 USB-C 电缆将 `USB-UART` 端口连接到 PC（用于供电和查看串行输出）」「打开开发板的电源开关。」
  —— 这两句描述的是 **ESP32-P4-Function-EV-Board**（同文件 `:11`），**不能当成 JC-ESP32P4-M3-DEV 有电源开关的证据**。

### 6.3 CH340 驱动安装步骤

- 【文档原文】商家资料没有给出**步骤**，只有要求（`烧录说明…txt:1`）。可确证的只有：
  1. 电脑需安装 CH340 驱动（`烧录说明…txt:1`）；
  2. 驱动包在 `7-Character&Picture_Molding_Tool\USB-SERIAL CH340.rar`；
  3. 装完后在工具里能选到串口 —— 【文档原文】Espressif 工具文档 FAQ §4.1：
     「First go to the device manager and check if the serial port has been successfully installed. If not, check the driver for any possible issues.」
     （原文说"去设备管理器确认串口装好没有"）
- 【文档原文】工具文档 §4.1 还给了两个报错的处理：
  - `COM FAIL/连接串口失败` → 「Firstly, make sure the selected COM is correct; Then, check if the COM is already occupied by another thread.」
  - 找不到串口 → 去设备管理器确认驱动。
- 【文档原文】`configure\esp32p4\spi_download.conf` 里 `com_port1 = COM6`（商家机器上的历史选择，只作示例）。
- 【推断】"装 CH340 → 出现 `USB-SERIAL CH340 (COMx)` → 选该口" 的这个 SOP 是通用做法，商家文档没写死；手册里可以写，但要标注"通用步骤，非商家原文"。

### 6.4 其余风险清单（按证据等级）

| 风险 | 证据 |
|---|---|
| **烧错芯片型号**：工具 FAQ 明确 "The module selected in the tool is not the one you are actually using." 是同步失败/eFuse 报错的常见原因 | 【文档原文】`Flash_Download_Tool__en.pdf` §4.2、§4.3、§4.5 |
| **模式/频率选错导致下载后启动崩溃**：「The selected flash boot mode is wrong. / The selected flash download mode is wrong.」 | 【文档原文】同上 §4.5.1 |
| **日志文件与真实板子可能不是同一块**：13 个日志里有 5 个不同 MAC 前缀（`0x6055…`、`0xe806…`、`0x3c84…`、`0xfe9c9e…`、`0xf09e…`），其中 `9C9E6E5BA8A4` 明显是 4 MB 的另一台设备 | 【日志实测】见 §1.2 表 |
| **首包乱码**：ROM 默认把启动信息打到 UART0（本项目 UART0 就是协议通道） | 【文档原文】`p4_ds.txt:1487`「(Default) UART0 and USB Serial/JTAG controller」；【代码原文】`protocol.md:18` 已要求 PC 侧丢弃非 `{...}` 行 |
| **GPIO21 一脚两用**（按键 + 触摸 INT 声明冲突） | 【代码原文】`config.h:22` vs `config.h:49` |
| **GPIO37/38 是 strapping 脚**：数据手册允许任意值（`p4_ds.txt:1452-1454`），但**上电瞬间**若外部电路（如 CH340 的 TX 拉低）在窗口期内驱动它，理论上可影响 Joint Download Boot 判定表 | 【文档原文】Table 3-3 +【推断】—— 实测未见问题（13 个日志全 PASS 是在 GPIO37/38 已接 CH340 的板上） |
| **USB 口选错**：若固件同时开了 USB-Serial-JTAG 控制台，PC 会看到两个串口 | 【代码原文】`uvc sdkconfig:1345`；【推断】 |
| **手工烧录时忘"断电重上电"**：商家手册明确说上传后要断电才能再进烧录态 | 【文档原文】用户手册 p7 |

---

## 7. 速查表（给固件与手册用）

### 7.1 给 `firmware/`（一句话可执行）

| 项 | 值 | 等级 |
|---|---|---|
| Target | `esp32p4` | 【代码原文】`sdkconfig:513` / `sdkconfig.defaults.esp32p4:1` |
| IDF 版本 | 5.5.6（本机 `tools/idf-env.ps1:9`；商家 demo 5.5.0） | 【代码原文】 |
| Flash | 16 MB，QIO，80 MHz | 【代码原文】+【推断】（模式见 §8 矛盾 1） |
| 分区表偏移 | `0x8000`；app `0x10000` | 【代码原文】+【日志实测】 |
| 分区表 | 用 §5.2 的单 app 方案（nvs 0x6000 / phy_init 0x1000 / factory 3M @0x10000） | 【推断（建议）】 |
| **协议 UART** | **UART0 = GPIO37(TXD) / GPIO38(RXD)**，115200 8N1，无流控 | 【文档原文】（`p4_ds.txt:981-982`）+【代码原文】`protocol.md:13` |
| 控制台 | 固件默认关日志（`ESP_LOG_NONE`），**UART0 不进 VFS console** | 【代码原文】`protocol.md:16,20` |
| **HID USB** | `CFG_TUD_HID = 1`；Report ID 1=键盘(8B)、2=鼠标(5B)；EP IN 0x81/16B/bInterval 10 | 【代码原文】`protocol.md:137-161` |
| vendor/pid | `0x303A` / **`0x4005`**（商家 demo 是 `0x8000`，必须改） | 【代码原文】`protocol.md:158` vs `uvc sdkconfig:2320` |
| TinyUSB 关键项 | 商家实测组合：`CONFIG_TINYUSB_RHPORT_HS=y` + `espressif/tinyusb 0.15.0~10`；本项目走 `espressif/esp_tinyusb ^2.0.0`，**sdkconfig 里没有 rhport 配置**（见 §4.3） | 【代码原文】+【推断/待确认】 |
| bInterval 陷阱 | `bInterval=10`：FS ⇒ 10 ms、HS ⇒ **64 ms**；`protocol.md` §7 要求 `mouse_move < 15 ms` ⇒ 只有 FS 能达标 | 【推断】（USB 2.0 规范）+ `firmware\main\hid_device.c:32-39` |
| USB 引脚 | HS：专用 `USB_DM`(49)/`USB_DP`(50)；FS 备选：GPIO24/25（USB-Serial-JTAG 默认）或 GPIO26/27（FS OTG 默认） | 【文档原文】`p4_ds.txt:781-782,2757-2760,2784-2786` |
| 不要碰的 GPIO | 7,8(I2C) 9-13,48(I2S/PA) 21(按键) 35,36(strapping) 14-19,54(C6 SDIO) 24,25,26,27(USB) | 见 §3.1 |
| 安全区 GPIO（若需加外设） | 20, 23, 28–34, 39–47, 51–53（+ 低置信 0–6） | 【推断（未被引用）】 |

### 7.2 给烧录手册（可逐条落到纸面）

| 步骤 | 内容 | 等级 |
|---|---|---|
| 1 | 安装 CH340 驱动（资料包内 `USB-SERIAL CH340.rar`）；装完在设备管理器确认出现串口 | 【文档原文】+【推断】 |
| 2 | 用 USB-C 线连**转 UART 口**（CH340 所在口）—— 具体哪个口以原理图为准 | 【推断】+【无法验证（仅图片）】 |
| 3 | 保证供电 ≥ 500 mA（5 V） | 【文档原文】`烧录说明…txt:2` |
| 4 | 进入下载模式：P4 需 **GPIO35=0 且 GPIO36=1**；或用工具/自动下载电路自动进入 | 【文档原文】`p4_ds.txt:1448-1454` |
| 5 | 工具：Flash Download Tool 3.9.7（`ChipType=ESP32-P4`，`WorkMode=Develop`，`LoadMode=UART`） | 【文档原文】release_note + 工具文档 §2.1 |
| 6 | 波特率 **115200**（实测 PASS），SPI MODE **QIO**，SPI SPEED **80 MHz**，Flash Size **16 MB** | 【日志实测】+【代码原文】 |
| 7 | 烧录**分地址**（不要只烧合并镜像）：`bootloader.bin@0x2000`、`partition-table.bin@0x8000`、`app.bin@0x10000` | 【日志实测】+【推断】 |
| 8 | 校验：日志出现 `Hash of data verified.` / `Download result: PASS` | 【日志实测】 |
| 9 | 烧完若要再烧一次：**断电重上电**再进下载模式 | 【文档原文】用户手册 p7 |
| 10 | 用本项目：另开串口（115200 8N1）跑 `pc/` 桥；**首包会是乱码，属正常** | 【代码原文】`protocol.md:18` |

---

## 8. 数据源自身的矛盾与不一致（引用前必读）

| # | 矛盾 | 原文 | 建议处理 |
|---|---|---|---|
| 1 | 同一 sdkconfig 里 FLASHMODE 既 QIO 又 "dio" | `uvc sdkconfig:642` `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y` **但** `:647` `CONFIG_ESPTOOLPY_FLASHMODE="dio"`；`xiaozhi sdkconfig:644` vs `:649` 同样 | 以运行日志 `spi_flash: flash io: qio`（`lvgl_demo_v9\README_CN.md:136`）与 `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y` 为准：**QIO**；字符串 `"dio"` 视为 IDF 生成物不一致【推断】 |
| 2 | 主频 400 MHz vs 360 MHz | 规格书 p3 正文「running at speeds up to 400 MHz」vs p3 Features「Main frequency up to 360M」、p4 参数表「Master Frequency 360M」；用户手册 p2 也说 400 MHz | 手册写 360 MHz（参数表 + 实机日志 `cpu freq: 360000000 Hz`，`lvgl_demo_v9\README_CN.md:118`）【推断】 |
| 3 | xiaozhi 的 sdkconfig 不是本板 | `xiaozhi sdkconfig:700` `CONFIG_BOARD_TYPE_GUITION_JC8012P4A1=y`，`:701` `# CONFIG_BOARD_TYPE_GUITION_JC_ESP32P4_M3_DEV is not set` | 其中 C6 SDIO 引脚（14-19/54）来自 esp_hosted 默认值，**不是** JC-ESP32P4-M3-DEV 的板级声明 → 本文件已标【推断】。「C6 走 SDIO 而非 UART」这一点同时被模块符号缺 GPIO14-19 旁证 |
| 4 | 用户手册的对象写错 | 手册 p2「how to upload an Arduino program to the **JC1060P470** module」，但文件是 `Getting started JC-ESP32P4-M3-DEV.pdf` | 手册是"照抄另一块板"的模板；不能当本板的接口说明 |
| 5 | UVC demo 目录名 vs 配置 | 目录 `uvc_camera_ov5647`，但 `sdkconfig.defaults.esp32p4:1` 是 `CONFIG_CAMERA_SC2336=y`，`sdkconfig.defaults:15` 才是 `CONFIG_CAMERA_OV5647=y` | 与我们的 HID 无关，但说明商家 demo 是拼装的，引用时**只引 sdkconfig 里的 USB 行** |
| 6 | 工具文档版本 vs 工具版本 | 文档说适用 v3.9.6，`release_note.txt` 是 3.9.7 | 无需处理，知道即可 |
| 7 | 板级 `config.json` 的 build 名字拼错 | `main\boards\guition-jc-esp32p4-m3-dev\config.json:5` `"name": "guition-jc-esp32p4-mc-dev"`（`p4`→`mc`） | 若用 xiaozhi 的构建系统选板，注意别按 name 匹配 |
| 8 | 本板 `README.md` 是空的 | `main\boards\guition-jc-esp32p4-m3-dev\README.md` 只有 2 个空行 | 板级说明为零，这也是本次要取证的原因 |

---

## 9. 本次实际引用的数据源（只读，未修改）

| 文件（绝对路径） | 用途 |
|---|---|
| `%USERPROFILE%\Downloads\JC-ESP32P4-M3-DEV\8-Burn operation\烧录说明：Burning instructions.txt` | CH340 / 供电（全文 2 行） |
| `...\8-Burn operation\flash_download_tool_3.9.7\logs\`（13 个 `.txt`） | 波特率、地址映射、Flash ID、结果（§1.2 逐条引用） |
| `...\8-Burn operation\flash_download_tool_3.9.7\configure\esp32p4\spi_download.conf`（非 UTF-8，含 GBK 路径） | 工具参数原文 |
| `...\8-Burn operation\flash_download_tool_3.9.7\configure\esp32p4\utility.conf` | `sector_protect_enable = False` 等（未用于结论） |
| `...\8-Burn operation\flash_download_tool_3.9.7\doc\release_note.txt` | 3.9.7 支持 P4 |
| `...\8-Burn operation\flash_download_tool_3.9.7\doc\Flash_Download_Tool__en.pdf`（pypdf 抽取） | 工具用法、FAQ、下载模式（无 P4 步骤） |
| `...\8-Burn operation\Burn files\JC-ESP32P4-M3-DEV-TEST-V1.3.bin`（1198448 B）、`JC1060P470_C6.bin`（1146768 B） | 与日志字节数交叉验证 |
| `...\6-User_Manual\Getting started JC-ESP32P4-M3-DEV.pdf`（11 页，抽取 4540 字符） | 手册原文（Arduino 视角） |
| `...\2-Specification\JC-ESP32P4-M3-DEV Specifications-EN.pdf`（6 页，抽取 2338 字符） | 规格原文（16 MB / 32 MB PSRAM / 5 V / USB OTG HS） |
| `...\4-Driver_IC_Data_Sheet\esp32-p4_datasheet_en.pdf`（89 页，抽取 147992 字符） | UART0 默认脚、strapping、Boot Mode 表、USB PHY 引脚 |
| `...\1-Demo\IDF-DEMO\README.md` | IDF 版本 V5.5、屏幕型号提示 |
| `...\1-Demo\IDF-DEMO\xiaozhi-esp32\main\boards\guition-jc-esp32p4-m3-dev\{config.h,config.json,README.md,esp32p4-m3-dev.cc}` | 板级 pin 定义与实现（**本板唯一权威 pin 源**） |
| `...\xiaozhi-esp32\{sdkconfig,sdkconfig.defaults.esp32p4}` | 16MB/QIO/80M、C6 SDIO 引脚、板型选择 |
| `...\xiaozhi-esp32\partitions\v1\16m.csv` | 参考分区表（双 OTA） |
| `...\xiaozhi-esp32\scripts\flash.sh` | 仅 2 行：`esptool.py -p /dev/ttyACM0 -b 2000000 write_flash 0 ../releases/.../<merged-binary.bin>`（**别照抄**：那是另一块板/另一套镜像的写法，本项目未采用） |
| `...\1-Demo\IDF-DEMO\NoDisplay\uvc_camera_ov5647\{sdkconfig,sdkconfig.defaults,sdkconfig.defaults.esp32p4,dependencies.lock,main\idf_component.yml,README.md}` | P4 USB 设备模式证据 |
| `...\1-Demo\IDF-DEMO\withDisplay\test\partitions.csv` | 9M app + 4M spiffs（解释日志 0x910000） |
| `...\1-Demo\IDF-DEMO\withDisplay\lvgl_demo_v9\README_CN.md` | 启动日志（QIO/80M/16MB/GPIO26 冲突）**来自 EV 板** |
| `...\1-Demo\ARDUINO-DEMO\jc_esp32p4_m3_lvgl_v8\pins_config.h` | 触摸 I2C = 7/8 的独立佐证 |
| `...\5-Schematic\PCB package library\JC-ESP32P4-M3.SCHLIB` | binary strings：引脚名 `ESP_USB_N/P`、`USB1_P1_N/P`、GPIO 列表缺 14-19/24-25（仅作【推断】旁证） |
| `%USERPROFILE%\Documents\deepseek-harness\default-workspace\esp32p4-hid-agent\docs\protocol.md` | 与厂商证据做差异对照（**未修改**） |
| `...\esp32p4-hid-agent\firmware\{sdkconfig.defaults,main\idf_component.yml,main\hid_device.c,main\uart_link.c,main\Kconfig.projbuild,CMakeLists.txt}` | §4.3 的"本项目差异"实际对照（**只读，未修改**；快照时间 2026-10-17 19:2x） |
| `...\esp32p4-hid-agent\tools\idf-env.ps1` | IDF 5.5.6 / `D:\esp`（**未修改**） |

**只列目录、未逐字采信的文件**：`...\5-Schematic\*.png`（8 张，尺寸已记录，内容未看）、`...\3-Structure_Diagram\5.jpg`（800×800，未看）、`...\8-Burn operation\Burn operation instructions\*.png`（3 张，未看）、`...\1-Demo\IDF-DEMO\withDisplay\...\lvgl__lvgl\**`（第三方 LVGL 源码，无关）、`...\4-Driver_IC_Data_Sheet\esp32-c6_datasheet_en.pdf`（抽取了但本次未形成结论）。

---

## 10. 未验证 / 待补清单（下一位接手者请先做这些）

| # | 待补事实 | 需要的动作 | 现状 |
|---|---|---|---|
| 1 | 三个 Type-C 口的功能归属、丝印 | 人工看 `5-Schematic\5_USB&485.png` + 现场插拔实测 | 【无法验证（仅图片）】 |
| 2 | 1 A 保险丝是否存在、位置 | 人工看 `5-Schematic\1_PWR&SPEAKER.png` | 【无法验证（仅图片）】 |
| 3 | 双口供电接法（能否同时插两口、供电从哪口进） | 看图 + 实测 | 【无法验证（仅图片）】 |
| 4 | 原生 USB 口接的是 HS 专用 PHY 还是 GPIO24/25(FS) | 现场插 PC，看枚举/速度；或看图 | **firmware 的最大阻塞项** |
| 5 | 板上进入下载模式的物理方式（按键/跳线/自动下载） | 看图 + 实测 | 【无法验证（仅图片）】 |
| 6 | C6 到 P4 的实际引脚是否为 14-19/54 | 看图 `4_ESP32P4.png`，或跑 `esp_hosted` 实测 | 【推断】 |
| 7 | 本板 Flash 型号/厂商（`0x00182085` vs `0x001840c8` 哪颗） | 现场读 `esptool flash_id` | 【推断】 |
| 8 | 规格书第 5 页接口表（引脚/接口清单） | 人工看 PDF 第 5 页 | 【无法验证（仅图片）】 |
| 9 | 本板自身的启动日志（QIO/80M/16MB 的**本板**实测） | 烧一个 `hello_world` 看 boot log | 未做（当前仅有 EV 板日志） |
| 10 | `esp_tinyusb` 在 IDF 5.5.6 下的真实 Kconfig 名与 rhport 选法 | `D:\esp` 装好后查组件 Kconfig + `idf.py build` 后 `grep RHPORT sdkconfig` | 未做（`D:\esp` 属 Lead 所有，本 agent 未动）；`firmware\` 当前**没有**任何 rhport 配置，见 §4.3 |
| 11 | 本板原生 USB 的实际枚举速度（HS 480 / FS 12） | PC 侧看设备管理器，或固件打印 `tud_speed()` | 未做；直接决定 §4.3 的 bInterval 陷阱是否触发 |
| 12 | `firmware\` 是否补上 16 MB / QIO / 80 MHz 与分区表偏移声明 | 复核 `firmware\sdkconfig.defaults`（grep `FLASHSIZE\|FLASHMODE\|PARTITION`） | 19:2x 快照：**均无命中**，见 §4.3 |

---

*本文件由 `hw-facts`（task-3）生成，只写本文件；所有其他路径（含 `docs\protocol.md`、`firmware\`、`pc\`、商家资料 `%USERPROFILE%\Downloads\JC-ESP32P4-M3-DEV\`）均未修改。*
---

## 11. Lead 真板实测补充（2026-10-17，首次烧录）

本节条目全部为**真板实测**，优先级高于上文任何【推断】。执行者：Lead，端口 `COM6`。

### 11.1 芯片版本 = **v1.3（早期硅片）** ★ 直接影响编译配置

```text
> esptool --chip esp32p4 -p COM6 flash_id
Chip is ESP32-P4 (revision v1.3)
Features: High-Performance MCU
Crystal is 40MHz
MAC: 80:f1:b2:d3:9c:e6
Manufacturer: 68    Device: 4018
Detected flash size: 16MB
```

- **`Detected flash size: 16MB`** → 本文 §1.3「Flash 大小 16 MB」**真板确认**（此前是文档+sdkconfig 推断）。
- **revision v1.3 是硬约束**：ESP-IDF 5.5 默认 `CONFIG_ESP32P4_REV_MIN_301`（v3.1），而
  `components/esp_hw_support/port/esp32p4/Kconfig.hw_support:13-38` 明确写
  **rev <3.0 与 rev ≥3.0 支持互斥（硬件差异巨大，不可兼容）**。用默认配置烧录会失败：

  ```text
  A fatal error occurred: bootloader/bootloader.bin requires chip revision in range
  [v3.1 - v3.99] (this chip is revision v1.3). Use --force to flash anyway.
  ```

- **正确做法**：`firmware/sdkconfig.defaults` 增加
  `CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y` + `CONFIG_ESP32P4_REV_MIN_100=y`，
  然后**删除 `sdkconfig` 再 build**（已存在的 sdkconfig 不会被 defaults 覆盖）。
- **禁止**用 `esptool --force` 绕过：那样产物缺少早期硅片的 errata 补丁，属于带病运行。
- 改后：`esp32p4_hid_agent.bin` = **328,432 B**（改前 329,904 B），烧录 `Hash of data verified` 通过。

### 11.2 CH340 驱动：商家 RAR 里那份**装不上**（无签名）

| 项 | 商家 RAR 内 | 实际可用 |
|---|---|---|
| INF | `DRIVER\CH341SER.INF` | `D:\esp\ch340-signed\CH341SER.INF` |
| DriverVer | `06/03/2009, 3.1.2009.06` | `09/16/2024, 3.9.2024.09` |
| 签名 | `NotSigned`（无） | `Valid` / `CN=Microsoft Windows Hardware Compatibility Publisher` |
| 类 | 未声明 Class | `Class = Ports` |
| 结果 | `无法添加驱动程序包: 第三方 INF 不包含数字签名信息` | 装成功 → `USB-SERIAL CH340 (COM6)`，`oem83.inf`，厂商 `wch.cn` |

- 来源：Microsoft Update Catalog 搜 `CH340` → 最新一条 `wch.cn - Ports - 3.9.2024.9`。
- 安装：`tools\install-ch340-admin.cmd`（自提权 + 全程日志 `D:\esp\ch340-install.log`）。
- 设备硬件 ID `USB\VID_1A86&PID_7523` 在该 INF 的 `[WinChipHead.NTamd64]` 段中**存在**（已核对）。

### 11.3 协议链路实测（固件 v1.0.0 / proto 1.0）

| 检查 | 实测结果 |
|---|---|
| 上电 boot 事件 | `{"evt":"boot","fw":"1.0.0","proto":"1.0","uptime_ms":3}` ✔ |
| `ping` | `PING OK fw=1.0.0 proto=1.0 caps=hid_keyboard,hid_mouse,uart` ✔ |
| `status` | `host_mounted=False kbd_keys=0 mouse_btn=0 rx_ok=11 rx_bad=0 safety_timeout_ms=1500` ✔ |
| 帧完整性 | 连续 11 帧 `rx_ok=11`、`rx_bad=0`（115200 8N1 无错帧） ✔ |
| `host_mounted` | 插「转 UART 口」时恒为 **False**；需再把**高速 Type-C** 插到目标机才变 True |

- **已知竞态**：刚烧录完 / 刚插上串口时，打开端口会让 CH340 的 DTR/RTS 抖动 → 板子重新上电，
  第一帧落在固件 UART RX 任务起来之前被丢弃。表现为「第一次 ping 超时，之后全正常」。
  PC 侧已在 `agent_bridge.py::request()` 对**只读命令**（ping/status/keepalive）补发一次，
  绝不重发会动键鼠的命令（避免重复执行）。