# JC-ESP32P4-M3 HID Agent — 通信协议契约 v1.1

> 本文件是 **固件（firmware/）与 PC 桥（pc/）之间唯一权威接口定义**。
> 两侧实现必须逐字遵守本文；任何修改都必须同时改两侧并升级 `ver`。
> Owner: Lead。状态: **FROZEN v1.1（2026-10-17，真板联调后冻结）**

---

## 1. 物理层

| 项 | 值 | 说明 |
|---|---|---|
| 通道 | UART0（板子"转 UART 口" / CH340C） | GPIO38 = RXD，GPIO37 = TXD（P4 侧交叉） |
| 参数 | 115200 8N1，无流控 | 兼容模式，CH340 最稳 |
| 帧 | UTF-8 JSON，**一行一个对象**，`\n`(0x0A) 结尾 | 单行 ≤ 512 字节 |
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
| `ping` | `id` | `{"ok":true,"id":N,"pong":true,"fw":"<ver>","proto":"1.0","uptime_ms":T,"host_mounted":bool,"caps":[...]}` |
| `status` | `id` | `{"ok":true,"id":N,"t":T,"host_mounted":bool,"kbd_keys":<int>,"mouse_btn":<int>,"rx_ok":<int>,"rx_bad":<int>,"safety_timeout_ms":<int>}` |
| `keepalive` | `id?` | 刷新安全超时；带 `id` 时回 `{"ok":true,"id":N,"t":T}` |
| `set` | `key`(string), `value`(int) | 支持的 key：`safety_timeout_ms`（0 = 关闭安全释放）。返回 `{"ok":true,"id":N,"key":...,"value":...}` |

`caps` 固定为 `["hid_keyboard","hid_mouse","uart"]`。

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

---

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