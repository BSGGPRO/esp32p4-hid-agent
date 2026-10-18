# pc/ — JC-ESP32P4-M3 HID Agent PC 桥接

PC 侧桥接 CLI：把 JSON-lines 命令通过 UART 发给 P4（协议见 `../docs/protocol.md` v1.0.1），
并提供键鼠注入、截屏、差分验证与无硬件自测。

**零第三方依赖**：串口用 `ctypes` 直调 Win32（`CreateFileW` / `SetCommState` / `ReadFile` / `WriteFile`），
**不使用 pyserial**；除截图用的 Pillow 外只用标准库。

---

## 1. 运行环境（本机实测）

| 项 | 值 |
|---|---|
| 唯一可用解释器 | `%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe` |
| 版本 | CPython **3.12.14** (MSC v.1944 64-bit) |
| Pillow | **12.3.0**（唯一第三方依赖） |
| pyserial | **未安装**（本实现不需要） |
| 系统 PATH 上的 `python` | 不可用（Microsoft Store 占位符）→ **所有命令都用上面的绝对路径** |
| 屏幕 | 2560 × 1440（primary == virtual desktop），DPI 模式 `per-monitor-aware` |

下面统一记作 `$PY`：

```powershell
$PY = 'python'    # 你的 Python 3.12+（装了 Pillow；PATH 上是商店占位符时换成完整路径）
```

## 2. 文件清单

| 文件 | 作用 |
|---|---|
| `agent_bridge.py` | CLI + 协议客户端（id 关联/超时/`--no-wait` 连发/安全兜底） |
| `transport_base.py` | 传输层抽象（`open/close/write/readline/timeout/drain`）+ 行缓冲 |
| `transport_uart.py` | ctypes 直调 Win32 串口（115200 8N1，DCB/COMMTIMEOUTS）+ 串口枚举识别 |
| `transport_tcp.py` | C6 Wi-Fi **预留 stub**（v1.0 未实现，调用即明确报错，绝不假装成功） |
| `mock_p4.py` | 内置 mock P4（按 §3 实现固件侧语义，供自测/演示，无硬件） |
| `keymap.py` | protocol.md §4 键名表 + `text` 的 US 布局 Shift 规则 |
| `eyes.py` | Pillow `ImageGrab` 全屏截图 → PNG，返回 `(w, h, path, size)` |
| `verify.py` | 两图像素差分（变化像素数/比例/判定/包围盒，支持 `--region`） |
| `selftest.py` | 39 个用例：协议编解码 + 错误路径 + 释放语义 + 依赖审计 |
| `requirements.txt` | 只有 `Pillow` |
| `evidence/` | 验收原始输出与实测截图（`acceptance_raw.log`、`screenshot_acceptance.png` 等） |

## 3. 验收证据（全部真跑，逐条留证）

原始输出：`evidence/acceptance_raw.log`。

### 3.1 `--help` / `--list-ports` / `selftest` 全部 exit 0

```text
> $PY agent_bridge.py --help
usage: agent_bridge.py [-h] [--transport {uart,tcp,mock}] [--port PORT] [--baud BAUD]
                       [--host HOST] [--timeout SEC] [--no-wait] [--repeat N]
                       [--no-release-on-exit] [--json] [--stats] [--quiet] [--verbose]
                       [--list-ports] [--list-keys] <command> ...

JC-ESP32P4-M3 HID Agent — PC 桥接 CLI（protocol.md v1.0.1，零第三方依赖）
PC bridge CLI for the P4 HID agent (protocol v1.0.1, stdlib + Pillow only).
...
exit=0

> $PY agent_bridge.py --list-ports
COM    型号/Model                                VID:PID     建议/Advice                         总线/Bus
--------------------------------------------------------------------------------------------------------------
COM4   Bluetooth 虚拟串口                          -           跳过：蓝牙虚拟口，不是开发板                    BTHENUM
         friendly: 蓝牙链接上的标准串行 (COM4)
COM5   Bluetooth 虚拟串口                          -           跳过：蓝牙虚拟口，不是开发板                    BTHENUM
         friendly: 蓝牙链接上的标准串行 (COM5)
--------------------------------------------------------------------------------------------------------------
提示/Note: COM4, COM5 是蓝牙虚拟串口，开发板不在这里 / Bluetooth virtual ports, not the board.
没有可自动识别的开发板串口 / no likely board port; 请用 --port 手动指定。

共 2 个串口 / 2 port(s).  协议 §1: 115200 8N1, CH340C。
exit=0

> $PY agent_bridge.py selftest
...
连发吞吐实测 / burst throughput (loopback): 86217 条/秒  (帧 35B；115200 8N1 理论上限 329 条/秒)
结果 / result: 39/39 通过 (passed), 0 失败 (failed), 耗时 2120 ms
== SELFTEST PASS ==
exit=0
```

> 回环吞吐每次实测在 8.6万–12.5万条/秒之间浮动（进程调度/CPU 影响），与协议 §7 的 ≥100 条/秒无关痛痒；
> 真串口的瓶颈是 115200 波特率，见 §7 性能表。

> `--list-ports` 正确识别出 **COM4/COM5 是蓝牙虚拟串口**（BTHENUM `蓝牙链接上的标准串行`），
> 并明确提示「开发板不在这里」。CH340（VID `1A86`）一旦插上会以 `★ 最可能是开发板` 排在最前并被自动选中
> （识别逻辑由 selftest 的 `port_listing_never_crashes` 用合成端口验证）。

### 3.2 `screenshot` 真出图（尺寸 == 屏幕，> 50KB）

```text
> $PY agent_bridge.py screenshot evidence/screenshot_acceptance.png --min-bytes 51200
SCREENSHOT OK  ...\pc\evidence\screenshot_acceptance.png
  尺寸/size : 2560 x 1440
  文件/bytes: 990688 B (967.5 KiB)
  DPI 模式  : per-monitor-aware  (grabbed 2560x1440)
  屏幕校验  : matches primary
exit=0

文件: ...\pc\evidence\screenshot_acceptance.png
字节数: 990688 bytes (967.5 KiB)

> $PY eyes.py --info
主屏/primary      : (2560, 1440)
虚拟桌面/virtual  : (2560, 1440) @ (0, 0)
DPI 模式          : per-monitor-aware
```

| 验收项 | 要求 | 实测 |
|---|---|---|
| PNG 存在 | 是 | ✅ `evidence/screenshot_acceptance.png` |
| 尺寸 = 屏幕 | 是 | ✅ **2560 × 1440**，`GetSystemMetrics` 主屏同值（`matches primary`） |
| 大小 > 50KB | 是 | ✅ **990 688 B ≈ 967.5 KiB**（50 KB 门限由 `--min-bytes 51200` 强制，exit 0） |
| DPI 缩放 | 不得缩水 | ✅ 先 `SetProcessDpiAwareness(2)`；`grabbed 2560x1440` 与屏幕一致 |

> PNG 字节数随桌面内容浮动（同一天多次实测：947 KiB / 967 KiB / 1.27 MiB），每次都远超 50 KB 门限。

### 3.3 无第三方依赖证明

```text
> $PY -c "<audit script>"          # 见 evidence/acceptance_raw.log 第 6 节
python                      : 3.12.14
Pillow                      : 12.3.0
third-party imported by pc/ : ['PIL']
pyserial(serial) installed? : False
numpy imported?             : False
RESULT: 除 Pillow 外零第三方依赖 / stdlib + Pillow only -> OK
exit=0
```

另外 `selftest` 内置用例 `dependency_audit_stdlib_plus_pillow` 会用 **AST 扫 `pc/*.py` 的全部 import**，
只允许「标准库 + `PIL` + 本目录模块」，并断言 `importlib.util.find_spec("serial") is None`（即本机没装 pyserial）→ 该用例通过。

### 3.4 无硬件端到端链路（mock）

```text
> $PY agent_bridge.py e2e-demo --transport mock --outdir evidence --dx 120
[1/5] ping OK  fw=P4-HID-1.0  proto=1.0  host_mounted=True  caps=hid_keyboard,hid_mouse,uart
[2/5] 截屏 before: ...\evidence\e2e_before.png  2560x1440  999166 B
[3/5] mouse_move dx=120 dy=0 -> {'ok': True, 'id': 2, 't': 202, 'dx': 120, 'dy': 0, 'reports': 1}
[4/5] 截屏 after:  ...\evidence\e2e_after.png  2560x1440  991369 B
[5/5] 差分: 变化像素 5763 / 3686400 (0.1563%)  判定: 有变化
结论/conclusion: mock 传输只做协议回环、不驱动真实 HID；因此差分检测到的任何变化都来自桌面自身
                （光标/终端刷新），不能当作 HID 生效的证据。
exit=0
```

> mock 分支**不会**把「桌面自己动了一下」误报成 HID 生效 —— 结论文字明确说明这是回环链路验证。

### 3.5 CLI 错误路径与退出码（全部实测）

```text
> $PY <临时检查脚本>          # 见 evidence/acceptance_raw.log 第 9 节
PASS text 非 ASCII -> exit 4 + detail=1                rc=4 (want 4)
PASS mouse-move 越界 -> exit 4                          rc=4 (want 4)
PASS key-combo 非法键名 -> exit 4                       rc=4 (want 4)
PASS set 越界 -> exit 4 (本地拦截)                        rc=4 (want 4)
PASS 用法错误 -> exit 2                                 rc=2 (want 2)
PASS ping --json 输出可解析                              rc=0 (want 0)
PASS ping --json 内容是合法响应对象
PASS --list-keys --json 表正确 (leftgui=8, f24=0x73)     rc=0 (want 0)
PASS text 长文本分块 (1000 字符)                          rc=0 (want 0)

CLI CHECKS: ALL PASS
exit=0
```

> 本地校验失败时会同时打印对应的**协议错误码**（`[BAD-ARG] err=bad_arg …` / `err=bad_key` / `err=unsupported_char`），
> 便于和固件侧返回的 `err` 对照。

## 4. 插板后怎么用（最小命令清单）

板子插上后（CH340C → 通常 `COM6`/`COM7`），按顺序跑：

```powershell
$PY = 'python'    # 你的 Python 3.12+（装了 Pillow；PATH 上是商店占位符时换成完整路径）
cd esp32p4-hid-agent\pc

# 0) 确认板子在哪：应看到 CH340 那行带 ★（蓝牙 COM4/COM5 会被标为「跳过」）
& $PY agent_bridge.py --list-ports

# 1) 探活（<30ms 目标）：看 fw / proto / host_mounted
& $PY agent_bridge.py ping --port COM7

# 2) 看状态：host_mounted 必须是 true，否则 HID 还没被宿主枚举
& $PY agent_bridge.py status --port COM7

# 3) 鼠标相对移动 60,-20（dy 正 = 向下）
& $PY agent_bridge.py mouse-move 60 -20 --port COM7

# 4) 组合键 Win+R
& $PY agent_bridge.py key-combo leftgui r --port COM7

# 5) 逐字符输入（仅 ASCII 0x20-0x7E）
& $PY agent_bridge.py text "hello world" --port COM7

# 6) 低延迟连发（不带 id、不等 ack）
& $PY agent_bridge.py mouse-move 5 0 --repeat 100 --no-wait --port COM7

# 7) 端到端自证：截屏 -> 移动 -> 截屏 -> 差分
& $PY agent_bridge.py e2e-demo --dx 200 --port COM7

# 8) 有异常时抓事件流（插拔 USB 会看到 hid_mounted / hid_unmounted）
& $PY agent_bridge.py events --seconds 5 --port COM7
```

省略 `--port` 时会自动挑一个最像开发板的串口（CH340 优先）；挑不到会明确报错让你手动指定。

**跨进程按住键**：默认退出即 `release_all`（§6.4）。要 `key-down` 后在本进程退出后仍保持按住，必须加
`--no-release-on-exit`，例如：

```powershell
& $PY agent_bridge.py key-down leftctrl --port COM7 --no-release-on-exit
& $PY agent_bridge.py mouse-move 100 0 --port COM7 --no-release-on-exit
& $PY agent_bridge.py release-all --port COM7
```

## 5. CLI 参考

```text
agent_bridge.py [全局选项] <命令> [命令选项]
```

全局选项可写在命令前或命令后：`--transport {uart,tcp,mock}`、`--port/-p`、`--baud`、`--host`、
`--timeout SEC`、`--no-wait`、`--repeat N`、`--no-release-on-exit`、`--json`、`--stats`、`--quiet`、`--verbose`、
`--list-ports`、`--list-keys`。

命令：`ping` `status` `keepalive` `set KEY VALUE` `mouse-move DX DY` `mouse-scroll DY`
`mouse-click [--btn left] [--count N] [--interval-ms 80]` `mouse-btn --btn --action`
`key-combo KEY... [--hold-ms 30]` `key-down KEY...` `key KEY...`（同 key-down）`key-up KEY...`
`release-all` `text "..."` `screenshot OUT.png [--min-bytes N]` `e2e-demo` `events --seconds N`
`list-ports` `list-keys` `verify A B` `selftest`。

退出码 / exit codes：

| code | 含义 |
|---|---|
| 0 | 成功 |
| 1 | selftest 失败 / `verify --expect` 未满足 / `screenshot --min-bytes` 未满足 / e2e 未检测到变化 |
| 2 | 用法错误（argparse）/ verify 文件错误 |
| 3 | 传输错误：端口不存在、被占用、中途拔出 |
| 4 | 设备回 `ok:false`（`err`）/ PC 侧本地参数校验失败（键名、越界、非 ASCII） |
| 5 | 协议超时（在 `--timeout` 内没等到 id 匹配的响应） |
| 6 | `--transport tcp`：v1.0 未实现 |
| 130 | Ctrl+C 中断（已发 `release_all`） |

## 6. 协议实现映射（protocol.md v1.0.1）

| 协议条款 | 实现位置 | 说明 |
|---|---|---|
| §1 逐行读、只认 `{...}`、解析失败静默丢弃 | `agent_bridge.AgentBridge._read_next_dict` | 非 `{...}` 行计入 `stats.garbage_lines`；`json` 失败计入 `stats.bad_json`；**都不退出** |
| §1 单行 ≤512B、`text` 分块不切断字符 | `AgentBridge.split_text` / `text` / `_write_obj` | 按最坏 10 位 id 预留预算；发送前硬校验，超长直接拒发 |
| §1 115200 8N1 无流控 | `transport_uart.UartTransport._configure` | `DCB`(28B) 位域手工布置，`SetCommTimeouts` 立即返回模式 + 1ms 轮询 |
| §2 带 id 必回同 id；不带 id 不回 | `send_nowait` / `send_ff` / `wait_for` | 乱序响应进 `_pending`，之后 `wait_for` 可直接命中 |
| §2 响应/事件带 `t`；未知字段忽略 | 全流程只按 key 取值 | `ping` 用例里显式发过未知字段验证前向兼容 |
| §3.1 `ping`/`status`/`keepalive`/`set` | `AgentBridge.ping/status/keepalive/set_value` | `set` 本地也校验 0–60000（§9.4） |
| §3.2 `key`/`key_down`/`key_up`/`key_tap`/`release_all`/`text`/`text_tap` | 同名方法 + `mock_p4.MockP4Device._cmd_*` | 修饰键折叠进位、非修饰键 ≤6 槽位 |
| §3.3 `mouse_move`/`mouse_btn`/`mouse_scroll`/`mouse_click` | 同名方法 | 范围 -32767..32767；拆分 ≤127 由固件负责（mock 已模拟） |
| §3.4 全部错误码 | mock 各分支 + `ProtocolError` | `bad_json`/`unknown_cmd`/`bad_arg`/`bad_key`/`unsupported_char`/`not_mounted`/`rate_limited` 均有对应用例 |
| §4 键名表 + text Shift | `keymap.py`（唯一表） | 95 个 ASCII 可打印字符全覆盖；`--list-keys` 可打印 |
| §6.1 安全释放 | `mock_p4.MockP4Device._check_safety`（固件侧语义） | 时钟可注入 → 超时释放是**确定性**用例 |
| §6.4 PC 侧兜底 | `AgentBridge.close` + `emergency_release` + `safety_cleanup` + 信号处理器 | `finally`/context manager/atexit/SIGINT/SIGTERM 都会发 `release_all`（不带 id），并 `FlushFileBuffers` 排空 TX |
| §7 性能目标 | selftest `no_wait_burst_throughput` | 见下 |
| §9.1/§9.2/§9.3/§9.4 裁定 | 见上表各行 | `detail` 0-based、超长行丢弃、`release_all` 永远 ok、`id` 数值相等 |

### §7 性能

| 指标 | 目标 | 实测 |
|---|---|---|
| `ping` 往返 | < 30 ms | 回环下 ~0.2 ms；**真串口数据要插板后测**（115200 下理论 ≈0.4 ms/帧 + 固件处理） |
| `mouse_move` 单条延迟 | < 15 ms | 同上，需真板验证 |
| 连发吞吐 | ≥ 100 条/秒 | ✅ 回环 8.6万–12.5万条/秒（多次实测浮动）；115200 8N1 下 35B 帧理论上限 **329 条/秒**（>100） |
| 安全释放延迟 | ≤ `safety_timeout_ms` + 50 ms | 固件侧语义，mock 用例确定性验证（`safety_timeout_auto_releases_and_events`） |

## 7. selftest 覆盖（39 例，无需硬件）

```powershell
& $PY agent_bridge.py selftest          # 或：& $PY selftest.py
& $PY selftest.py --filter chunk        # 只跑名字含 chunk 的
& $PY selftest.py --json                # 机器可读结果
```

分组：

* **§1/§9.1 行解析**（6）：ROM 乱码静默丢弃、7 字节分片重组、坏 JSON 行、固件侧 `bad_json`、超长行整行丢弃、PC 侧拒发超长帧
* **§2 报文**（5）：`ping`/`caps`/`t`、`status` 计数、无关 id 跳过并缓存、乱序响应关联、超时干净抛出且不挂住
* **§3 命令与错误码**（11）：`unknown_cmd`、`bad_key`(detail=首个非法键名)、`bad_arg`(类型/越界/缺字段)、键槽溢出、
  `unsupported_char`(0-based)、Shift 规则、`text` 分块、键盘状态往返、鼠标拆分/连击、`set` 范围、`keepalive`、`not_mounted` 边界
* **§6 安全**（6）：超时自动释放 + 事件、`set 0` 关闭安全释放、context manager 退出释放、atexit 路径、`--no-release-on-exit`、中途拔出
* **交付物/CLI**（5）：`verify` 差分（区域/阈值/比例/尺寸不一致）、`eyes` 接口、CLI 退出码矩阵、依赖审计

## 8. 已知限制 / 未实现

1. **`--transport tcp` 未实现（v1.0 预留）**：`transport_tcp.TcpTransport.open()` 会抛
   `TransportNotImplemented` 并打印原因 + 替代方案，CLI 退出码 6。预留设计（C6 做 UART↔TCP 字节级透传、
   帧格式与 UART 逐字一致）写在文件头部注释里。
2. **真硬件指标未测**：本机当前只有蓝牙虚拟串口（COM4/COM5），没有任何 USB-UART 桥，
   因此 `ping` 往返 / `mouse_move` 延迟只能给回环值与理论值，真板数据需插板后补（Lead 负责）。
3. **`kbd_keys` 计数口径**：`status.kbd_keys` 本实现约定为「当前按下的键总数 = 修饰键位数 + 普通键槽位数」，
   需要固件侧采用同一口径（已在 selftest 中固定该行为）。
4. **`text` 分块边界**：分块按字符切（v1 只有 ASCII，天然安全）；分块后每个 `text` 命令独立返回，
   `--no-wait` 下不保证与其它命令的先后（UART 顺序写入仍然保序）。
5. **`mock` 的 `text` 不处理 §6.5 的中断**：mock 原子处理整条 `text`；「收到 `release_all` 立即中止后续字符」
   属于固件行为，需 firmware 侧实现。

## 9. 故障排查

| 现象 | 处理 |
|---|---|
| `[PORT-ERROR] 无法打开串口 COM7 … WinError 2` | `--list-ports` 看真实 COM 号；插板/装 CH340 驱动 |
| `WinError 5`（拒绝访问） | 串口被别的程序占用（串口监视器/PuTTY）→ 先关掉 |
| `[TIMEOUT] … 等 id=N 的响应超过 1.5s` | 波特率必须 115200（§1）；确认固件烧好；加大 `--timeout` |
| `[DEVICE-ERROR] … err=not_mounted` | 宿主没枚举 HID：查 USB 线/端口，`ping` 看 `host_mounted` |
| `[DEVICE-ERROR] … err=bad_key` | 用 `--list-keys` 查 §4 规范键名 |
| `[DEVICE-ERROR] … err=rate_limited` | 连发超过 200 条/秒 → 降速或退避重试 |
| `err=unsupported_char detail=N` | `text` 含非 ASCII 可打印字符，`N` 是 **0-based** 字符索引 |
| 截图尺寸不对 | `eyes.py --info` 对屏幕指标；本实现已开 per-monitor DPI 感知 |
| 想先验证 PC 侧再插板 | `--transport mock` + `selftest`，全链路无需硬件 |