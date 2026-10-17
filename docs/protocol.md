# JC-ESP32P4-M3 HID Agent — 通信协议契约 v1.2

> 本文件是 **固件（firmware/）与 PC 桥（pc/）之间唯一权威接口定义**。
> 两侧实现必须逐字遵守本文；任何修改都必须同时改两侧并升级 `ver`。
> Owner: Lead。状态: **v1.2（2026-10-17，追加 stats / sd_test；v1.1 的键鼠报文格式未变）**

---

## 1. 物理层

| 项 | 值 | 说明 |
|---|---|---|
| 通道 | UART0（板子"转 UART 口" / CH340C） | GPIO38 = RXD，GPIO37 = TXD（P4 侧交叉） |
| 参数 | 115200 8N1，无流控 | 兼容模式，CH340 最稳 |
| 帧 | UTF-8 JSON，**一行一个对象**，`\n`(0x0A) 结尾 | 请求帧单行 ≤ 512 字节；`stats` 响应例外（约 1.5–2 KB），见 §10.1 |
| 控制台日志 | 固件默认 `ESP_LOG_NONE`（静默） | 保证协议不被日志污染；调试时用 `RX_DEBUG=1` |

**PC 侧解析规则（必须实现）**：逐行读取，`line.strip()` 后仅当以 `{` 开头且以 `}` 结尾才尝试 `json.loads`；解析失败的行**静默丢弃**（上电时 ROM 会吐乱码）。禁止因为乱码行报错退出。

**固件侧解析规则（必须实现）**：用 `fgets` 式按行缓冲读取 UART0 原始字节（不进 VFS console），同样只处理 `{...}` 行，非法行静默丢弃；行内 JSON 用 `cJSON` 解析（IDF 自带 `json` 组件）。

---

## 2. 报文总则

- 请求（PC→P4）：`{"cmd": <string>, "id": <int?>, ...}`，`id` 可选。
  - 带 `id` → 固件**必须**回一条同 `id` 的响应。
  - 不带 `id` → fire-and-forget，执行但不回响应（用于低延迟连发）。
- 响应（P4→PC）：`{"ok": true|false, "id": <int>, ...}`，`ok:false` 时必有 `"err"` 字符串。
- 异步事件（P4→PC）：`{"evt": <string>, ...}`，无 `id`。
- 所有响应/事件都带 `"t"`：固件开机以来的毫秒数（`esp_timer_get_time()/1000`）。
- **未知字段必须忽略**（前向兼容）；未知 `cmd` 必须回 `err:"unknown_cmd"`。

---

## 3. 命令表

### 3.1 探活 / 状态

| cmd | 请求字段 | 响应 |
|---|---|---|
| `ping` | `id` | `{"ok":true,"id":N,"pong":true,"fw":"<ver>","proto":"1.2","uptime_ms":T,"host_mounted":bool,"caps":[...]}` |
| `status` | `id` | `{"ok":true,"id":N,"t":T,"host_mounted":bool,"kbd_keys":<int>,"mouse_btn":<int>,"rx_ok":<int>,"rx_bad":<int>,"safety_timeout_ms":<int>}` |
| `keepalive` | `id?` | 刷新安全超时；带 `id` 时回 `{"ok":true,"id":N,"t":T}` |
| `set` | `key`(string), `value`(int) | 支持的 key：`safety_timeout_ms`（0 = 关闭安全释放）、`temp_warn_c` / `temp_throttle_c`（§10.4 过热阈值，20–100 °C）、**`dfs`（`0` = 固定 360 MHz 稳定态，`1` = adaptive 省电调度，§10.4）**。返回 `{"ok":true,"id":N,"key":...,"value":...,"pm":{...}}` |
| `stats` | `id` | §3.6 的完整统计对象 |
| `sd_test` | `id`, `bytes`? (默认 1048576) | §3.7 的 TF 卡读写校验测速 |
| `sd_remount` | `id` | §3.8 卸载并重新探测/挂载 TF 卡，立即回 `sd` 状态 |
| `sd_format` | `id`, `confirm`(="ERASE"), `fs`?("fat32") | §3.9 **破坏性**格式化 FAT32 + 重挂载 + 立即 1MB `sd_test` |

`caps` 固定为 `["hid_keyboard","hid_mouse","uart"]`（v1.2 未改动；`stats` / `sd_test` 的存在不体现在 `caps` 里，用 §3.6 的 `proto` 判断版本）。

### 3.2 键盘

| cmd | 请求字段 | 说明 |
|---|---|---|
| `key` | `keys`: string[] | **按下并保持**（不自动抬起）。元素可为修饰键名或普通键名（见 §4） |
| `key_tap` | `keys`: string[], `hold_ms`?: int (默认 30) | 按下 → 延时 `hold_ms` → 全部抬起。组合键用这个 |
| `key_down` | `keys`: string[] | 同 `key`（别名，语义更明确） |
| `key_up` | `keys`: string[] | 抬起指定键 |
| `release_all` | — | 清空所有按键与鼠标按钮，HID 报全 0 |
| `text` | `text`: string, `cps`?: int (默认 40) | 逐字符输入（US 布局，自动处理 Shift）。仅 ASCII 0x20–0x7E，遇其他字符回 `err:"unsupported_char"` 并 `detail:<位置>` |
| `text_tap` | 同 `text` | `text` 的别名 |

- `key` / `key_down` / `key_up` 的 `keys` 数组里修饰键（`leftctrl` 等）折叠进 modifier 字节，非修饰键写入 6 键槽位（最多 6 个，超出回 `err:"bad_arg"`）。
- 不在 §4 表内的键名 → `err:"bad_key"`，`detail` 为第一个非法键名。

### 3.3 鼠标

| cmd | 请求字段 | 说明 |
|---|---|---|
| `mouse_move` | `dx`: int, `dy`: int | 相对移动，范围 -32767..32767；固件按每报 ≤127 自动拆分连续上报 |
| `mouse_btn` | `btn`:"left"\|"right"\|"middle", `action`:"down"\|"up"\|"click" | `click` = down + `click_ms`(默认 40) + up |
| `mouse_scroll` | `dy`: int | 滚轮，正 = 上滚（HID 语义），拆分为 ≤127 的连续上报 |
| `mouse_click` | `btn`? (默认 left), `count`? (默认 1), `interval_ms`? (默认 80) | 连击便捷命令 |

### 3.4 错误码

| err | 触发条件 |
|---|---|
| `bad_json` | 行内不是合法 JSON / 无 `cmd` 字段 |
| `unknown_cmd` | `cmd` 不在本表 |
| `bad_arg` | 类型错、缺必填字段、越界、键槽溢出 |
| `bad_key` | 键名不在 §4 表 |
| `unsupported_char` | `text` 含非 ASCII-可打印字符 |
| `not_mounted` | HID 未被主机枚举（`tud_mounted()==false`）时收到会真正产生报文输出的命令 |
| `rate_limited` | 超过 200 条/秒（固件丢弃并回此错误，PC 侧应退避） |

### 3.5 异步事件

| evt | 含义 |
|---|---|
| `hid_mounted` | 主机完成枚举（`tud_mount_cb`） |
| `hid_unmounted` | 主机断开（`tud_umount_cb`） |
| `safety_release` | 安全超时触发，已强制释放全部按键/按钮，`detail` 为被释放的说明 |
| `boot` | 固件启动完成，携带 `fw` 与 `proto` |

### 3.6 `stats`（v1.2 新增，**字段已冻结**）

请求：`{"cmd":"stats","id":N}`（不带 `id` 则执行但不回包）。

响应：

```json
{"ok":true,"id":N,"stats":{
  "uptime_ms":123456,
  "cpu":{"core0_pct":1.2,"core1_pct":0.0,"mhz":360,"window_ms":1000},
  "mem":{"heap_free":123456,"heap_min_free":100000,"heap_total":327680,
         "psram_free":33554432,"psram_total":33554432},
  "temp_c":45.2,"temp_warn":false,
  "tasks":[{"name":"hid_rx","prio":5,"state":"blocked","stack_hwm":2345}],
  "sd":{"present":true,"mounted":false,"err":"exfat_not_supported","fs":"exfat",
        "capacity_mb":61056,"free_mb":null,
        "cid":"mfg=0x03 oem=0x5344 name=SD64G serial=0x12345678 date=0x2024 rev=8",
        "type":"SDXC","speed_write_kbps":null,"speed_read_kbps":null,
        "bus":"4bit/20MHz","cd":null,
        "tries":[{"bus":"1bit/400kHz","err":"ok"}]},
  "net":{"up":false,"dhcp":false,"ip":null,"netmask":null,"gw":null,
         "mac":"80:F1:B2:D3:9C:E6","link_mbps":0},
  "hid":{"mounted":true},
  "uart":{"rx_ok":42,"rx_bad":0,"tx_ok":41},
  "proto":"v1.2"}}
```

| 字段 | 语义 |
|---|---|
| `uptime_ms` | 开机毫秒（与响应 `t` 同基准） |
| `cpu.core0_pct` / `core1_pct` | **两次 `stats` 调用之间**该核的 CPU 占用（% = 1 − idle 时间 / 窗口时间）。第一次调用（或窗口不足 1 ms）返回 0，`window_ms` 告知实际窗口 |
| `cpu.mhz` | **当前** CPU 主频（`esp_clk_tree_src_get_freq_hz(SOC_MOD_CLK_CPU)`）；启用 DFS 后空闲时会是 40，负载时回到 360 |
| `mem.heap_*` | **内部 RAM** 堆：free / 历史最低 free / 总量，单位字节（三者同一堆域，`heap_min_free` 不再混入 PSRAM） |
| `mem.psram_*` | PSRAM 堆 free / total，单位字节；未启用 PSRAM 时为 0 |
| `temp_c` | （v1.2 新增，可选，§10.4）ESP32-P4 内部温度传感器读数，单位 °C，保留 1 位小数；传感器不可用时为 `null` |
| `temp_warn` | （v1.2 新增，可选，§10.4）最近一次读数 ≥ 告警阈值（默认 70 °C） |
| `pm` | （v0.2 追加，可选，§10.4）省电调度状态：`{governor:"fixed360" \| "adaptive", dfs:bool, min_mhz, max_mhz, cur_mhz, idle_ms, boosts, downs, last_reason}`。`idle_ms` = 本次命令到达前的静默时长；`downs>0` 证明真的降过频；老客户端可忽略整个对象 |
| `tasks[]` | `uxTaskGetSystemState()` 全量快照；`state` ∈ `running/ready/blocked/suspended/deleted`；`stack_hwm` 单位**字节**（本 port 的 `StackType_t` 是 `uint8_t`） |
| `sd` | §3.7 的 TF 卡状态；`err` 为 `null` 表示挂载成功，否则是 `<阶段>:<esp_err_name>`（如 `card_init:ESP_ERR_TIMEOUT` / `mount:ESP_FAIL` / `exfat_not_supported`） |
| `sd.fs` | **从原始 boot sector 判定**：`"fat32"` / `"exfat"` / `"unknown"`（FAT12/16 也归 `unknown`，因为字段只允许这三个值） |
| `sd.capacity_mb` | CSD 容量（1 MB = 1024×1024 B）；`free_mb` 仅挂载成功时非 null |
| `sd.type` | `SDSC` / `SDHC` / `SDXC` / `MMC` / `unknown` |
| `sd.speed_*_kbps` | 最近一次 `sd_test` 的结果；未跑过为 `null` |
| `sd.bus` | （v1.2 新增，可选，§10.4）成功应答的总线配置，如 `"4bit/20MHz"` / `"1bit/400kHz"`；没有卡应答时为 `null` |
| `sd.cd` | （v1.2 新增，可选，§10.4）卡检测开关状态。**本板卡座没有独立 CD 引脚**（microSD 的 pin2 是 CD/DATA3 复用），无法读取，固定为 `null` |
| `sd.tries` | （v1.2 新增，可选，§10.4）探测重试记录，每项 `{bus, err}`；`err` 为 `null`/`"ok"` 表示该次探测成功。用于在没插卡时解释"为什么没卡" |
| `net` | **v0.2 没有网络出口**（不做以太网/Wi-Fi）：固定 `up=false`、`ip/netmask/gw=null`、`link_mbps=0`；`mac` 是芯片 efuse 基础 MAC，仅供识别 |
| `hid.mounted` | `tud_mounted()` |
| `uart.rx_ok` / `rx_bad` | 同 `status` 的计数器（含被限流丢弃的帧只计 `rx_ok`？**不**：见 §9 说明，限流丢的帧不计入任何计数器） |
| `uart.tx_ok` | 已写出的 UART 帧数 |
| `proto` | 固定字符串 `"v1.2"`（注意带 `v`；`ping.proto` 按 §3.1 仍是 `"1.2"`） |

**实现约定**：

- 组装该对象需要在 UART RX 任务内挂起调度器做一次任务快照（µs 级），语义上仍是同步命令：先 `ok` 后 `stats`。
- 该响应**超过 §1 的 512 字节帧上限**（实测 ~1.5 KB），PC 侧必须把 **RX 行缓冲上限提到 ≥ 4096 字节**，见 §10.1。
- `stats` 不产生 HID 报文，因此 `tud_mounted()==false` 时也正常返回（§9.3）。

### 3.7 `sd_test`（v1.2 新增，**字段已冻结**）

请求：`{"cmd":"sd_test","id":N,"bytes":1048576}`（`bytes` 可选，默认 1048576，合法范围 1024–16777216）。

响应（命令被接受就 `ok:true`，卡的问题在 `sd_test.err` 里如实报告）：

```json
{"ok":true,"id":N,"sd_test":{
  "bytes":1048576,"write_kbps":812,"read_kbps":1650,
  "verify":true,"path":"/sdcard/ops_test.bin","err":null}}
```

| 字段 | 语义 |
|---|---|
| `bytes` | 实际测试字节数（回显） |
| `write_kbps` / `read_kbps` | 千字节/秒（1024 B/s 记 1 kB/s），基于 `esp_timer` 墙钟，越大越快 |
| `verify` | 读回内容与写入内容逐字节一致 |
| `path` | 固定 `/sdcard/ops_test.bin`（测试完保留，不删除） |
| `err` | `null` 或 `not_mounted` / `fopen_write_failed` / `fwrite_failed` / `fread_failed` / `verify_mismatch` / `no_mem` |

**行为约定**：

- 1 MB 读写可能耗时数秒，`sd_test` 在**独立任务**里执行（不阻塞 UART 解析：期间 `ping`/`stats` 仍可响应）；同一时刻只允许一个 `sd_test`，重复请求回 `{"ok":false,"err":"bad_arg","detail":"sd_test busy"}`。
- 未挂载（无卡/exFAT/挂载失败）时不报 `not_mounted` 错误码，而是 `ok:true` + `sd_test.err="not_mounted"` + `verify:false`，方便 PC 侧直接展示卡的状态。
- 卡上没有空间或 I/O 出错只反映在 `err` 里；**任何情况下都不格式化卡**。

---

### 3.8 `sd_remount`（v0.2 追加，非破坏性）

用于用户重新插卡 / 换卡 / 格完卡后**不重启板子**。语义：

1. 若已挂载则先 `esp_vfs_fat_sdcard_unmount()`；
2. 重跑完整探测阶梯（1bit/400kHz → 4bit/20MHz → 1bit/20MHz）并尝试挂载；
3. 立即回复当前 `sd` 状态对象（字段与 §3.6 的 `sd` 完全相同）：
   `{"ok":true,"id":N,"sd":{present,mounted,err,fs,capacity_mb,free_mb,cid,type,bus,cd,tries},"remount_ok":bool}`
4. 失败不崩：`ok:true` 仍然返回，原因在 `sd.err`（如 `card_init:ESP_ERR_TIMEOUT`、`exfat_not_supported`）；`sd_test`/`sd_format` 正在跑时返回 `bad_arg`（`detail:"sd busy"`）。

### 3.9 `sd_format`（v0.2 追加，**破坏性**）

请求：`{"cmd":"sd_format","id":N,"confirm":"ERASE","fs":"fat32"}`

* `confirm` **必须逐字等于 `"ERASE"`**（大小写敏感），缺失或不匹配 → `bad_arg`（`detail:"confirm:\"ERASE\" required"`）**且不执行任何写操作**；
* `fs` 可省略，出现时只允许 `"fat32"`，其它 → `bad_arg`（`detail:"only fs:\"fat32\""`）；
* 未插卡 / 卡无响应 → `ok:false`，`sd_format.err` = `card_init:no_card`；
* 执行流程：先取**格式化前**证据（CID/type/容量/原 fs）→ `esp_vfs_fat_sdmmc_mount(format_if_mount_failed=true)` 走 IDF FatFs 的 `f_mkfs`（64 GB 自动选 FAT32）→ 重新挂载 → 读 boot sector 复核 `fs` → **等 2 s** → 跑 1 MB `sd_test`（失败重试 1 次）→ 一次性回复；
* 失败时仍会重新探测/挂载，避免把卡留在"半死"状态；任何失败都不复位、不 panic；
* 成功响应：
  `{"ok":true,"id":N,"sd_format":{"confirm":"ERASE","before":{cid,type,fs,capacity_mb},"after":{mounted,fs,capacity_mb,free_mb,bus},"sd_test":{bytes,write_kbps,read_kbps,verify,path,err},"err":null}}`
* 耗时：64 GB 卡约 **2–4 s**（写两张 FAT）；PC 侧超时请给 ≥ 30 s。

> ⚠️ 两者都会在 SD 忙时返回 `bad_arg`；`sd_format` 与 `sd_test` 共用同一把"SD 忙"标志，避免并发访问同一张卡。

## 4. 键名表（唯一权威，两侧各实现一份）

**修饰键（折叠进 modifier 字节）**

| 名称 | bit | 名称 | bit |
|---|---|---|---|
| `leftctrl` | 0x01 | `rightctrl` | 0x10 |
| `leftshift` | 0x02 | `rightshift` | 0x20 |
| `leftalt` | 0x04 | `rightalt` | 0x40 |
| `leftgui` / `win` / `cmd` | 0x08 | `rightgui` | 0x80 |

别名统一：`win`、`cmd`、`meta`、`super` → `leftgui`。

**普通键（HID Usage ID，Keyboard/Keypad page 0x07）**

| 名称 | 值 | 名称 | 值 | 名称 | 值 |
|---|---|---|---|---|---|
| `a`..`z` | 0x04–0x1D | `1`..`9` | 0x1E–0x26 | `0` | 0x27 |
| `enter` | 0x28 | `esc` | 0x29 | `backspace` | 0x2A |
| `tab` | 0x2B | `space` | 0x2C | `minus` `-` | 0x2D |
| `equal` `=` | 0x2E | `leftbracket` `[` | 0x2F | `rightbracket` `]` | 0x30 |
| `backslash` `\` | 0x31 | `semicolon` `;` | 0x33 | `quote` `'` | 0x34 |
| `grave` `` ` `` | 0x35 | `comma` `,` | 0x36 | `period` `.` | 0x37 |
| `slash` `/` | 0x38 | `capslock` | 0x39 | `f1`..`f12` | 0x3A–0x45 |
| `printscreen` | 0x46 | `scrolllock` | 0x47 | `pause` | 0x48 |
| `insert` | 0x49 | `home` | 0x4A | `pageup` | 0x4B |
| `delete` | 0x4C | `end` | 0x4D | `pagedown` | 0x4E |
| `right` | 0x4F | `left` | 0x50 | `down` | 0x51 |
| `up` | 0x52 | `numlock` | 0x53 | `kp_slash` | 0x54 |
| `kp_asterisk` | 0x55 | `kp_minus` | 0x56 | `kp_plus` | 0x57 |
| `kp_enter` | 0x58 | `kp_1`..`kp_9` | 0x59–0x61 | `kp_0` | 0x62 |
| `kp_period` | 0x63 | `application` | 0x65 | `power` | 0x66 |
| `kp_equal` | 0x67 | `f13`..`f24` | 0x68–0x73 | | |

**`text` 的 Shift 规则（US 布局）**：`A-Z` → shift + 小写键；`!@#$%^&*()_+{}|:"<>?~` → shift + 对应基础键。

---

## 5. HID 描述符契约（固件侧必须匹配）

**单一 HID 接口 + 两个 Report ID**（与 ESP-IDF 官方 `examples/peripherals/usb/device/tusb_hid` 完全一致的做法，
即 `CFG_TUD_HID = 1`，`esp_tinyusb` 负责接口描述符，应用只提供 report descriptor 与 configuration descriptor）：

| Report ID | 用途 | 报告负载 | 布局 |
|---|---|---|---|
| 1 (`HID_ITF_PROTOCOL_KEYBOARD`) | Keyboard (boot) | 8 字节 | `[modifier, reserved, k0..k5]` |
| 2 (`HID_ITF_PROTOCOL_MOUSE`) | Mouse (boot) | 5 字节 | `[buttons, dx, dy, wheel, pan]` |

```c
const uint8_t hid_report_descriptor[] = {
    TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(HID_ITF_PROTOCOL_KEYBOARD)),
    TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(HID_ITF_PROTOCOL_MOUSE))
};
// 下发：
tud_hid_keyboard_report(HID_ITF_PROTOCOL_KEYBOARD, modifier, keycode6);   // keycode6 为 NULL 表示全抬
tud_hid_mouse_report(HID_ITF_PROTOCOL_MOUSE, buttons, dx, dy, wheel, pan);
```

- `buttons`：bit0=左，bit1=右，bit2=中。
- `dy` 正 = 向下移动（HID 语义）；PC 侧约定：**`mouse_move` 的 `dy` 正 = 向下**（与屏幕坐标一致），固件不得翻转。
- 滚轮 `wheel`：正 = 上滚；`pan` 水平滚动，v1 固定填 0。
- 每份报告单次 `dx/dy/wheel` 绝对值 ≤ 127，超出由固件拆成多帧。
- VID/PID：VID `0x303A`（Espressif），PID **`0x4004`**（esp_tinyusb 默认值 `0x4000 | (CFG_TUD_HID << 2)`，HID=1 → 0x4004）。**不自定义 PID**，减少配置面。
- 字符串描述符：Manufacturer `JC-ESP32P4-M3 Agent`，Product `P4 HID Bridge`，Serial 用芯片 MAC。
- 配置描述符 power 字段 100 mA、属性 `TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP`（照官方例程）。
- 端点：EP IN `0x81`，16 字节，**`bInterval = 4`**（HS: 2^3 × 125µs = **1 ms/帧**（1000Hz）；FS: 4 ms/帧）。
  **禁止用 10**：HS 下 2^9 × 125µs = 64 ms/帧，会让拆帧 move 与 text 退化到不可用（固件侧用单个 `#define HID_EP_INTERVAL` 表达，便于再调）。
- HID 事件：esp_tinyusb v2 **不再允许**应用定义 `tud_mount_cb`/`tud_umount_cb`（组件内强符号，重复定义会链接失败）。固件用 25ms 轮询 `tud_mounted()` 的边缘检测来发 `hid_mounted`/`hid_unmounted` 并清状态（事件延迟 ≤25ms，§6 语义不变）。

---

## 6. 安全语义（硬性要求，缺一不可）

1. **安全释放**：只要存在按下的键/按钮，超过 `safety_timeout_ms`（默认 1500 ms）没收到**任何** PC 报文 → 立即 `release_all` 并发 `safety_release` 事件。
2. **断开即释放**：`tud_umount_cb` 里清空本地状态。
3. **上电即释放**：固件启动先发一次全 0 报告，避免残留按键。
4. **PC 侧兜底**：桥接进程退出（`finally` / `atexit` / 信号）必须发 `release_all`；若端口已拔，忽略异常。脚本 `Ctrl+C` 也要释放。
5. 任何 `text` / `key_tap` 操作期间若收到 `release_all`，立即中止后续字符。

---

## 7. 时序与性能目标

### 7.1 目标（设计值）

| 指标 | 目标 |
|---|---|
| `ping` 往返 | < 30 ms（115200 下） |
| `mouse_move` 单条延迟 | 首个报告 < 15 ms；拆帧连发按 `bInterval=4` 的 1 ms/帧推进（HS） |
| `text` 吞吐 | 默认 `cps=40`（25 ms/字）在 1 ms/帧下有余量；FS(4 ms/帧) 约 8 ms/字 |
| `key_tap` hold | 默认 30 ms，实际 = max(hold_ms, 1 个端点周期) |
| 连发吞吐 | ≥ 100 条/秒（PC 侧可不等 ack 连发无 `id` 命令） |
| 安全释放延迟 | ≤ `safety_timeout_ms` + 50 ms |

### 7.2 真板实测（2026-10-17，P4 rev v1.3 + CH340 @115200，n=30~40）

| 指标 | 实测 | 判定 |
|---|---|---|
| `ping` 往返 | mean **18.4 ms**（p95 19.1，min 17.4） | ✅ |
| `mouse_move` ack | mean **12.5 ms**（p95 12.8） | ✅ |
| `mouse_move` dx=1000（拆 8 帧） | mean **19.6 ms** | ✅ |
| 连发吞吐（no-wait） | **332 条/秒**（≈ 115200 线速上限） | ✅ |
| `key_tap` ack | min 11.7 / p50 41.5 ms | ⚠️ 见下 |
| 安全释放（停发报文 1500 ms） | 实测 2.2 s 静默后按键已抬起 | ✅ |

- **`key_tap` 的 ack 含 `hold_ms`**（默认 30 ms），所以 ack 延迟 ≈ 12 + 30 ≈ 42 ms 是**设计使然**，不是链路慢。测延迟请用 `ping` / `mouse_move`。
- **修掉的一处 20 ms 空等**：固件原先用 `uart_read_bytes(..., 256, pdMS_TO_TICKS(20))`，而 ESP-IDF 的该函数内部是 `while (length)`，会为**每一个缺失的字节**等满 `ticks_to_wait`；一条 25 字节命令因此被压后最多 20 ms 才解析。改为 UART 事件队列 + `ticks_to_wait=0` 后，`ping` 往返由 **38.4 ms 降到 18.4 ms**、`mouse_move` 由 **32.0 ms 降到 12.5 ms**。见 §9.7。
- **已知局限（限流窗口是固定 1 秒桶）**：`RATE_LIMIT_PER_SEC=200` 用「距窗口起点 ≥1000 ms 就清零」的**固定窗口**实现，因此跨越桶边界的连发可能两半都不到 200 条而**不触发**限流（实测连发 250 条后紧跟的 5 条普通命令全部放行）。这不是安全缺陷（`release_all` 本来就免限流，见 §9.6），如需严格限速应改成滑动窗口。

---

## 8. 版本

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0 | 2026-10-17 | 首版冻结：UART JSON-lines、键鼠复合 HID、安全释放 |
| 1.0.1 | 2026-10-17 | 追加 §9 实现细节裁定（行长/分块、0-based 索引、not_mounted 边界），两侧必须同步 |
| 1.1 | 2026-10-17 | **真板联调后冻结**：§9.6 `release_all` 免限流（安全缺陷修复）、§9.7 RX 改事件队列且 `ticks_to_wait=0`（去掉 20 ms 空等）；§7 增补真板实测值与固定窗口局限。协议报文格式**未变**，PC 侧无需改动即可兼容。 |
| 1.2 | 2026-10-17 | **追加 `stats` / `sd_test`**（§3.6/§3.7，字段冻结）、`ping.proto` 升到 `"1.2"`、`fw` 升到 `1.2.0`；新增 §10（stats 长响应行 / 16 MB 分区表 / 无以太网）。键鼠命令报文格式**未变**；PC 侧唯一的必要改动是把 RX 行缓冲上限提到 ≥ 4096（§10.1）。 **同日补充（仅新增可选字段/命令）**：`stats.temp_c` / `stats.temp_warn`（§10.4 温度与过热保护）、`stats.sd.bus` / `stats.sd.cd` / `stats.sd.tries`（SD 探测可观测性）、`set` 新增 `temp_warn_c` / `temp_throttle_c`；新增 `sd_remount` / `sd_format` 命令（§3.8/§3.9）；新增 `stats.pm` 省电调度状态与 `set dfs` 运行时开关（§10.4，默认 fixed360）。 |

---

## 9. 实现细节裁定（v1.0.1，Lead 裁定，两侧必须一致）

### 9.1 行长与 `text` 分块

- 单帧上限 512 字节（含 `\n` 前的 JSON 全文）。
- **固件行缓冲必须 ≥ 640 字节**；读到超长行（缓冲满仍未遇到 `\n`）→ 丢弃整行、`rx_bad++`、**不回任何响应**（避免把半行当命令）。
- **PC 侧负责分块**：`text` 长文本自动切成多个 `text` 命令，保证每帧编码后 ≤ 512 字节，且**不切断字符**（按 UTF-8 边界，实际上 v1 仅 ASCII 可打印字符）。
- PC 侧**永不主动发送超长帧**；固件侧必须能抗住超长行而不越界。

### 9.2 `unsupported_char` 的 `detail`

- `detail` 为**首个非法字符的 0-based 索引**（整数，不是字符串拼接）。示例：`text="ab☃c"` → `{"ok":false,"id":N,"err":"unsupported_char","detail":2}`。
- 两侧 mock/实现都用 0-based，禁止 1-based。

### 9.3 `not_mounted` 的边界

| 命令 | `tud_mounted()==false` 时的行为 |
|---|---|
| `release_all` | **永远 `ok:true`**（安全原语不允许失败；内部照样清状态） |
| `ping` / `status` / `keepalive` / `set` | 正常处理，`ok:true`；`status.host_mounted=false` |
| 其余会产生 HID 报文的命令（`key`/`key_down`/`key_up`/`key_tap`/`text`/`text_tap`/`mouse_move`/`mouse_btn`/`mouse_scroll`/`mouse_click`） | 回 `{"ok":false,...,"err":"not_mounted"}`，且**不得修改本地按键状态** |

### 9.4 其它已确认约定

- `id` 回显：响应里的 `id` 必须与请求**数值相等**（不是字符串）。
- `set` 的 `value` 仅接受整数；`safety_timeout_ms` 合法范围 0–60000，超范围回 `bad_arg`。
- 响应字段顺序不作要求，PC 侧按 key 取值。

### 9.5 HID 事件与端点参数（固件实测约束，2026-10-17 裁定）

- esp_tinyusb v2 把 `tud_mount_cb` / `tud_umount_cb` 收进组件内部（v2.0.0 breaking change：不再允许应用在驱动外处理 TinyUSB 事件），应用**不得**定义这两个符号，否则 `multiple definition` 链接失败。
- 替代方案（已冻结）：固件用 25 ms 周期轮询 `tud_mounted()` 做**边缘检测**：
  - `false → true`：补发一次全 0 报告，并发 `{"evt":"hid_mounted"}`
  - `true → false`：清空本地按键/按钮状态，并发 `{"evt":"hid_unmounted"}`
  - 事件延迟 ≤ 25 ms，§6 的安全语义完全不变。
- 端点 `bInterval = 4`（见 §5）：HS 1 ms/帧、FS 4 ms/帧。固件上报必须用 `tud_hid_ready()` 判断 + 有界重试，**`release_all` 的报告绝不允许被静默丢弃**（必要时等待就绪再发）。
- 托管组件依赖：`espressif/esp_tinyusb ^2.0.0`（首次 `idf.py build` 需从 components.espressif.com 拉取，需联网）。

### 9.6 `release_all` 免限流（安全命令优先，2026-10-17 真板裁定）

**`release_all` 永远不受 §7 的 200 条/秒限流约束。**

- 真板复现的缺陷：连发 200 条 `mouse_move` 后紧跟 `release_all`，旧固件返回
  `{"ok":false,"err":"rate_limited"}` —— 也就是说**唯一能把目标机按键抬起来的命令被拒绝了**，
  按键保持按下直到安全定时器兜底。
- 实现要求：限流判断必须放在解析出 `cmd` 字段**之后**，并对 `cmd == "release_all"` 短路放行；
  其它命令的限流行为不变。
- 固件侧已按此实现（`cmd_protocol.c::proto_handle_line`），并有专项回归脚本
  （`D:\esp\test_safety.py`：250 帧风暴后 `release_all` 必须 `ok:true`）。
- 推论：PC 侧在异常路径里可以无条件调用 `release_all`，无需先退避。

### 9.7 RX 必须用事件队列，`ticks_to_wait` 必须是 0（2026-10-17 真板裁定）

- **禁止**写 `uart_read_bytes(port, buf, N, pdMS_TO_TICKS(t))` 且 `N > 一帧长度`：
  ESP-IDF 的 `uart_read_bytes()` 内部是 `while (length) { xRingbufferReceiveUpTo(..., ticks_to_wait, length) }`，
  会为**每一个还没到的字节**各等一次 `ticks_to_wait`。一条 25 字节的命令用 256 字节缓冲 + 20 ms 超时读，
  就会被压后**最多 20 ms** 才交给解析器（实测 `ping` 往返 38.4 ms）。
- 正确形态：`uart_driver_install(..., queue_depth, &queue, 0)` 拿事件队列 →
  `xQueueReceive(queue, &evt, portMAX_DELAY)` 阻塞等 `UART_DATA` →
  `uart_read_bytes(port, buf, evt.size, 0)` 只取已到达的字节、立即返回。
- 必须处理 `UART_FIFO_OVF` / `UART_BUFFER_FULL`：`uart_flush_input()` + `xQueueReset()` +
  丢弃当前半行 + `rx_bad++`（半行绝不可当命令执行）。
- 实测收益：`ping` 38.4 → 18.4 ms，`mouse_move` 32.0 → 12.5 ms（详见 §7.2）。
---

## 10. v1.2 实现裁定（2026-10-17，Lead 裁定）

### 10.1 `stats` 响应可以超过 512 字节（PC 侧必须配合）

- **只读命令的响应不受 §9.1 的 512 字节限制**：`stats` 的实测长度约 **1.5 KB**（任务表随任务数增长，上限约 3 KB）。
- 固件侧：请求行解析上限仍是 768 字节（`UART_LINK_LINE_MAX`，§9.1），**只有** `stats` 响应会超长；这是刻意保留的例外，不引入分块协议。
- PC 侧（`pc/transport_base.py::MAX_LINE_BYTES`）**必须从 512 提到 ≥ 4096**，否则 `LineBuffer` 会把它当超长行丢弃（当前实现：`max_line=512`，超长即 `overflow++` 丢弃）。
  - 例：`MAX_LINE_BYTES = 4096`；mock 的帧长断言同步放宽（`pc/mock_p4.py` 只约束 PC→固件方向，不需要改）。
- 其它命令的请求/响应长度约束**不变**。

### 10.2 16 MB flash 的分区表

- 板子实测 16 MB flash（见 `docs/hardware-facts.md`），v1.2 起使用自定义分区表 `firmware/partitions.csv`：

| Name | Type | SubType | Offset | Size |
|---|---|---|---|---|
| `nvs` | data | nvs | `0x9000` | 24 KB |
| `phy_init` | data | phy | `0xf000` | 4 KB |
| `factory` | app | factory | `0x10000` | **3 MB** |

- 原因：IDF 默认 single-app 只给 app **1 MB**；v0.2 镜像（HID + FatFs + SDMMC + stats）约 0.5 MB，后续功能必须留余量，**不允许**再出现"分区装不下"的构建失败。
- `factory` 仍在 `0x10000`（IDF 默认偏移），所以**回滚只需重烧 app**：
  `esptool --chip esp32p4 -p COM7 write_flash 0x10000 <旧固件>.bin`。
- 16 MB 剩余空间未分配（无 OTA / 无 storage 分区需求），不浪费在任何用不到的分区上。

### 10.3 v0.2 **不做**以太网 / 板载 HTTP 服务

- 用户明确不插网线：板载 RJ45（IP101）与 `esp_http_server` 在本版本**不启用**，`sdkconfig.defaults` 里显式关闭 EMAC：
  `CONFIG_ETH_USE_ESP32_EMAC=n`、`CONFIG_ETH_USE_SPI_ETHERNET=n`。
- 因此 `stats.net` 是**诚实的"无出口"状态**：`up=false`、`dhcp=false`、`ip/netmask/gw=null`、`link_mbps=0`；`mac` 填芯片 efuse 基础 MAC（仅用于识别设备）。
- 未来若恢复网络功能，字段名不得变（`net.*` 已冻结），只需让 `up/ip/...` 变成真实值。

### 10.4 被动散热与省电调度（温度可观测 + 过热保护 + adaptive governor）

**结论先说**：本板**不需要**为散热降频（实测芯片 **22.6–28.6 °C**），但省电调度做成了**用户可运行时开关**，且**默认关闭**（固定 360 MHz 稳定态）。

1. **温度可观测**：`stats.temp_c`（float，1 位小数，传感器不可用时 `null`）、`stats.temp_warn`（≥ 告警阈值）。ESP32-P4 内部温度传感器，20–100 °C 窗口（±2 °C）；每次 `stats` 采样一次，不额外起轮询任务。
2. **固定 240 MHz 不可行**：本芯片是早期样品 ESP32-P4 rev **v1.3**，`Kconfig.cpu` 对 `ESP32P4_SELECTS_REV_LESS_V3` 只提供 **40 / 360 MHz** 两档，没有 240 MHz。
3. **两种 governor**（`stats.pm.governor`）：
   - `fixed360`（**默认**）：`esp_pm_configure(max=360,min=360)`，CPU 恒定 360 MHz。这是 2026-10-17 二分实验里的稳定态：10/10 `sd_test` 通过、0 次 HID 重枚举。
   - `adaptive`（`{"cmd":"set","key":"dfs","value":1}` 开启）：
     - **立即升频**：收到任一 UART 帧、发出 HID 报文、开始 SD 传输 → 立刻 `esp_pm_lock_acquire(ESP_PM_CPU_FREQ_MAX)`（2026-10-17 实测 ping RTT 39.0 ms vs fixed360 38.6 ms、mouse_move 32.8 ms vs 32.9 ms，**无感知差异**）。
     - **延后降频（滞回）**：连续 **≥500 ms** 无任何活动才释放锁允许降到 40 MHz；期间任何活动重置计时。温度 `≥70 °C` 时阈值缩到 **100 ms**（`last_reason:"thermal"`）。
     - **IO 期间禁止切频**：`sd_test` / `sd_format` / `sd_remount` / 卡探测挂载全程持锁（引用计数，可嵌套），时钟绝不在传输中变化。
     - **零抖动**：60 s 空闲 ×2 轮实测 0 次复位、0 次 HID 事件，`downs` 单调增长（真降频）。
   - 切回 `{"cmd":"set","key":"dfs","value":0}` 立即回到 fixed360。
4. **为什么默认关 / 为什么不持久化**：2026-10-17 二分证据 —— 早期直接开"裸 DFS"（min=40，无 boost/滞回）时：`sd_test` 1 MB **首次即 `rst:0x7 (HP_SYS_HP_WDT_RESET)`**，HID 周期性重枚举（用户听到 USB 插拔提示音）；关掉后 10/10 sd_test 干净、0 重枚举。adaptive governor 用"活动期锁频"规避了该交互（15/15 sd_test 通过），但**默认仍留在 fixed360**（稳定优先）；`set dfs` **不写 NVS**，复位自动回到安全态。
5. **过热保护**：`temp_c ≥ 70` → `temp_warn=true`（并缩短自适应空闲阈值）；`≥ 80` → RX 任务帧间 `vTaskDelay(10 ms)` 限流，回落到阈值 −3 °C 自动解除。阈值运行时可配：
   `{"cmd":"set","id":1,"key":"temp_warn_c","value":70}`、`{"cmd":"set","id":2,"key":"temp_throttle_c","value":80}`（20–100 °C，须 warn < throttle）。
6. **失败安全**：任何 `esp_pm_configure` 失败 → 释放锁、回退 fixed360、`set` 返回 `{"ok":false,...,"detail":"pm_configure_failed"}`，绝不停在半配置状态。
7. **不做的事**：不轮询 TF 卡（`stats` 只读挂载时缓存的 `s_status`，无 SD 事务）；日志保持 `ESP_LOG_NONE`；无忙等。
