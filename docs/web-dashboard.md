# 网页仪表盘 — PC 当服务器，板子走串口

> 一个页面同时显示「**ESP32-P4 板子状态**」和「**这台 PC 的状态**」，手机/电脑浏览器都能看，每秒刷新。
> 因为板子**没有网络出口**（用户不插网线），页面和接口都由 PC 上的 `pc/hub.py` 托管；
> 板子数据由 hub 独占 COM7、每秒发一次 `stats` 命令采集。协议见 [protocol.md](protocol.md) §3.6（v1.2，字段冻结）。

---

## 0. TL;DR

```powershell
# 仓库根目录；python 指你的解释器（PATH 上是商店占位符时换成完整路径）
$PY = 'python'
& $PY pc\hub.py --serial COM7 --who "web-ui:hub" --quiet
```

- 电脑看：<http://127.0.0.1:8765/>
- 手机看（同一 Wi-Fi）：<http://192.168.1.221:8765/>
- `pc/brain.py` 那类需要裸串口的程序连 **127.0.0.1:8766**（TCP 透传）。
- 停止：`Ctrl+C`，或 `curl http://127.0.0.1:8765/api/shutdown`（只允许本机调用）。

---

## 1. 架构

```
┌──────────────────────────── PC（本机，192.168.1.221）────────────────────────────┐
│                                                                                  │
│   手机 / 电脑浏览器                                                               │
│        │  http://192.168.1.221:8765/                                             │
│        ▼                                                                          │
│   ┌──────────────────────────── pc/hub.py（单进程，标准库）──────────────────┐   │
│   │  HTTP :8765（0.0.0.0）                                                     │   │
│   │   GET /            → web/index.html                                       │   │
│   │   GET /app.js      → web/app.js        （页面内一律相对路径，无跨域）      │   │
│   │   GET /style.css   → web/style.css                                        │   │
│   │   GET /api/board   → 板子统计缓存（来自 COM7）                             │   │
│   │   GET /api/pc      → 本机统计缓存（ctypes/PowerShell/nvidia-smi）        │   │
│   │   GET /api/health  → 板子/本机/透传健康状态                                │   │
│   │   GET /api/shutdown→ 优雅退出（仅 127.0.0.1）                              │   │
│   │                                                                            │   │
│   │  后台线程 1：板子链路（持 tools/board_lock.py 锁）                          │   │
│   │     COM7 常驻 115200 8N1 ── 每 1 s 发 {"cmd":"stats","id":N} → 收响应缓存   │   │
│   │  后台线程 2：本机采样（1 s 缓存）                                          │   │
│   │  后台线程 3：TCP 透传 :8766（127.0.0.1）                                   │   │
│   └────────────────────────────────────────────────────────────────────────────┘   │
│        ▲ TCP 127.0.0.1:8766（一次一个客户端，字节双向透传，期间暂停 stats 轮询）      │
│        │                                                                          │
│   pc/brain.py（draw teammate 的 TcpTransport）──► 原生 JSON-lines → 板子            │
│                                                                                  │
│   CH340 串口（COM7，115200 8N1） ⇄ ESP32-P4（HID 键鼠 + stats）                    │
└──────────────────────────────────────────────────────────────────────────────────┘
```

要点：

| 设计 | 原因 |
|---|---|
| 板子不跑 HTTP，页面由 PC 托管 | 板子没有可用的网络出口（用户不插网线） |
| 串口**只在启动时打开一次、常驻** | CH340 每次打开都会复位板子；每秒开关串口会让板子反复重启 |
| hub 全程持 `tools/board_lock.py` 锁 | 串口唯一；与 fw-core 烧录、draw 绘图互斥（拿不到锁就等，不硬抢） |
| stats 轮询与 TCP 透传互斥 | 透传期间板子的响应必须全给 brain.py，不能被 stats 抢走 |
| 页面与接口同源 | 无 CORS/混合内容问题；手机只需访问 PC 一个地址 |

---

## 2. 启动

### 2.1 前台启动（推荐）

```powershell
cd <repo>
& python `
  pc\hub.py --serial COM7 --who "web-ui:hub" --quiet
```

启动横幅会打印本机所有可用 LAN IP 的访问地址、板子串口、透传端口、板子锁状态。

### 2.2 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | HTTP 绑定地址（手机访问需要 0.0.0.0） |
| `--port` | `8765` | HTTP 端口；`0` = 随机（自检用，横幅打印实际端口） |
| `--tcp-host` | `127.0.0.1` | 透传绑定地址（**不要**暴露到 0.0.0.0，避免局域网任何人操作键鼠） |
| `--tcp-port` | `8766` | 透传端口；`0` = 随机 |
| `--serial` | `COM7` | 板子串口（CH340；换 USB 口会变，用设备管理器确认） |
| `--who` | `web-ui:hub` | `board_lock` 持有者标识 |
| `--poll-interval` | `1.0` | 串口 stats 轮询周期（秒） |
| `--pc-interval` | `1.0` | 本机采样周期（秒） |
| `--top` | `12` | `/api/pc` 返回的进程数 |
| `--no-lock` | 关 | 不取板子锁（仅自检/无板调试，别在共用板子时用） |
| `--no-firewall` | 关 | 跳过自动加 Windows 防火墙入站规则 |
| `--once-board` | 关 | 只采一次板子数据并打印（排障：验证串口+协议，不启线程） |
| `--quiet` | 关 | 不打印每个 HTTP 请求（状态变化日志照常） |
| `--verbose` | 关 | 调试日志 |

### 2.3 板子锁与「谁在用串口」

- hub 启动后**全程持有** `.board.lock`（`tools/board_lock.py`），进程退出（Ctrl+C / `/api/shutdown` / 崩溃）时释放。
- 因此 **烧录固件、直接开串口调试、跑旧的 UART 脚本之前，先停 hub**；或者改走 TCP 透传（`127.0.0.1:8766`）。
- 反过来：如果 hub 启动时锁被 fw-core/draw 占着，hub **不会退出**，每 2 s 重试；这期间 `/api/board` 返回
  `{"ok":false,"err":"board_busy",...}`，页面显示「板子被占用」，锁释放后自动接管。
- 锁超过 900 s 未刷新会被视为 stale 自动打破（`board_lock.py` 行为）。

### 2.4 停止

```powershell
# 方式一：前台 Ctrl+C
# 方式二：本机调用（仅 127.0.0.1/::1 允许）
curl http://127.0.0.1:8765/api/shutdown
```

进程退出码 0；串口关闭、锁释放、两个监听端口关闭。

---

## 3. 手机访问

1. 手机和 PC 连**同一个 Wi-Fi/路由器**。
2. 手机浏览器打开 `http://192.168.1.221:8765/`（IP 以 hub 横幅打印的为准）。
3. 如果打不开，按顺序排查：

```powershell
# ① PC 的 LAN IP（WLAN）
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^127\.' } |
  Select-Object InterfaceAlias, IPAddress

# ② hub 是否在监听 0.0.0.0:8765
Get-NetTCPConnection -LocalPort 8765 -State Listen

# ③ 防火墙入站规则（hub 启动时会尝试自动加，没管理员权限会失败）
netsh advfirewall firewall show rule name="dsh-hub"
# 手动添加（需要管理员 PowerShell）：
netsh advfirewall firewall add rule name="dsh-hub" dir=in action=allow protocol=TCP localport=8765
```

> 也可以直接在本机浏览器开 `http://127.0.0.1:8765/` 对比：本机能开、手机不能开 → 基本就是防火墙/不同网段。

### PC IP 变了怎么办？

页面与接口**同源**，所以页面本身不用改任何 IP：手机用新 IP 打开 `http://<新IP>:8765/` 即可。
改 IP 只影响「手机输哪个地址」和「hub 横幅里显示的提示」。查当前 IP 用上面 ① 的命令，
或者看 hub 启动横幅里的「手机访问」行（启动时会枚举全部 IPv4）。

---

## 4. 页面说明

### 4.1 板子面板（左）

| 区域 | 内容 | 数据字段（protocol.md §3.6） |
|---|---|---|
| 指标行 | 运行时长 / CPU 主频 / HID 枚举 / 采样窗口 / **板子温度** | `uptime_ms`、`cpu.mhz`、`hid.mounted`、`cpu.window_ms`、`temp_c`+`temp_warn`（可选） |
| 双核占用 | 两个环形仪表 + 最近 120 s 双线曲线 | `cpu.core0_pct`、`cpu.core1_pct` |
| 内存 | Heap 空闲/历史最低/总量、PSRAM 空闲/总量，带占比条 | `mem.heap_*`、`mem.psram_*` |
| TF 卡 | 挂载状态、类型/文件系统、容量、可用、读/写速度、CID | `sd.*`（`speed_*_kbps` 来自 `sd_test`，没跑过显示 —） |
| 网络 | 连接状态、IP、链路速率、DHCP、网关、MAC | `net.*`（v0.2 **无网络出口**，固定 `up=false`，页面如实显示「未连接」） |
| UART | 收帧正常/收帧异常/发帧计数 | `uart.rx_ok/rx_bad/tx_ok` |
| 任务表 | FreeRTOS 任务（名称/优先级/状态/栈余量），可滚动、表头吸顶 | `tasks[]` |

温度卡片降级规则：
- 字段缺失 → 显示 `—`，不告警（兼容固件未提供该可选字段）；
- `temp_warn=true` → 红色 `⚠ 过热`；
- 无告警但 `temp_c ≥ 70 °C` → 琥珀色（本地阈值提示，不代表固件告警）。

### 4.2 本机面板（已移除）

**2026-10-18 按用户要求移除**：页面现在只显示板子面板（单栏），本机（PC）面板整块删除。

- 前端 `web/index.html` / `web/app.js` 不再引用 `api/pc`、不再有 `pollPc`，少一份每秒轮询开销；
- hub 的 `/api/pc` 接口与 PC 采样**保留**（`pc/selftest.py`、`docs/evidence/web-selftest.py` 仍在使用），
  只是页面不消费了；如需要可加 `--no-pc` 关掉采样（尚未实现，当前 1 Hz 采样开销约 100–300 ms/s，
  对稳定性无影响，实测 10 分钟 soak 期间 CPU/内存平稳）；
- `docs/evidence/web-selftest.py` 的断言已同步改为「页面不得再出现本机面板 id / api/pc / pollPc」，
  当前 **70/70 PASS**。

原面板内容（CPU 每核/内存/GPU/NPU/进程 Top）已随 HTML/JS 一并删除，历史上见 git 提交 `90d9555` 之前的版本。

### 4.3 断线、超时与离线态

- 页面每秒轮询板子数据；单次请求超时（2 s）自动重试，不弹窗。
- 超过 **3.2 s** 没新数据：面板整体标灰（降饱和），状态胶囊变「超时」。
- 板子三种明确离线原因（hub 透传，不猜）：
  - `board_offline`：COM7 打不开（没插板/串口被占用/COM 号变了）；
  - `board_busy`：`board_lock` 被别人占着（fw-core 烧录 / draw 绘图）；
  - `board_no_stats`：串口通了但固件不回 `stats`（固件版本旧）。
  页面左上胶囊与面板内提示条会显示具体原因，并持续重试。
- **透传中**（`passthrough=true`）：brain.py 正通过 `127.0.0.1:8766` 占用串口，hub 暂停 stats；
  页面胶囊显示「板子透传中」+ 黄色提示条（不是故障），透传结束后自动恢复轮询。
- 手机竖屏自适应：单列布局、指标 2 列、表格横向滚动、触控友好间距。

### 4.4 动态调频（DFS）开关

TF 卡卡片下方有一张「动态调频（DFS）」卡片，数据来自 `stats.pm`（固件 v1.2 可选字段）：

| 开关 | `stats.pm.governor` | 板子行为 |
|---|---|---|
| 关（默认） | `fixed360` | 固定 360 MHz，SD 卡 / USB 最稳 |
| 开 | `adaptive` | 智能调度：有活动立即升到 360 MHz；连续 ≥500 ms 无活动才降到 40 MHz；SD/USB 传输期间持锁禁止切频；`temp_c ≥ 70` 时空闲阈值缩到 100 ms |

- 开关状态**不持久化**：板子复位后回到 `fixed360`（安全默认），这是有意设计。
- 卡片同时显示 `boosts` / `downs` / `last_reason`，用来证明调度器真的在工作（只开不降 = 假功能）。
- 固件没上报 `pm` 字段时，开关自动禁用并提示「固件未上报」。
- 背景：裸 DFS（无 governor）会让 SD 大写入触发 `rst:0x7 (HP_SYS_HP_WDT_RESET)`；
  adaptive governor 的持锁 + 滞回规避了该问题，实测 15/15 次 1 MB 读写无复位、延迟无差异。
  详见 `docs/evidence/fw-governor.txt`、`fw-pm-toggle.txt`。

---

## 5. 接口契约

所有接口都在 **同一个 origin**（`http://<PC>:8765`）下，相对路径即可。

### 5.1 `GET /api/board`

板子在线：

```json
{"ok":true,
 "stats":{"uptime_ms":123456,"cpu":{...},"mem":{...},"tasks":[...],"sd":{...},
          "net":{...},"hid":{...},"uart":{...},"proto":"v1.2",
          "temp_c":52.5,"temp_warn":false},
 "ts":1792243648000, "age_ms":120, "port":"COM7", "serial_open":true, "passthrough":false}
```

板子不在线：

```json
{"ok":false,"err":"board_offline","detail":"无法打开串口: WinError 2 - 端口不存在（板子没插 / COM 号不对？）",
 "port":"COM7","serial_open":false,"passthrough":true|false}
```

- `stats` 对象就是 protocol.md §3.6 的冻结 schema，hub 原样透传（不裁剪未知字段）。
- hub 从 UART 响应里取数据，同时兼容 `{"ok":true,"id":N,"stats":{...}}` 与平铺字段两种实现，
  但 **对外 `/api/board` 一律是「顶层 stats」**：页面/第三方直接读 `resp.stats.cpu.core0_pct`，
  不要再套一层 `stats.stats`（app.js 用 `renderBoard(raw.stats)`，selftest 有断言防回退）。
- `age_ms` 是缓存年龄：> 3200 时页面标灰。

### 5.2 `GET /api/pc`（页面已不再使用）

```json
{"ok":true,"ts":1792243648000,
 "cpu":{"total_pct":8.3,"cores":[12.7,25.4,...],"mhz":2495},
 "mem":{"total_mb":15584,"used_mb":11543,"pct":74.1},
 "gpu":[{"name":"NVIDIA GeForce RTX 5060 Laptop GPU","util_pct":8.0,"temp_c":41.0,
         "mem_used_mb":1258,"mem_total_mb":8151}],
 "npu":{"detected":false,"name":null},
 "processes":[{"name":"chrome","pid":1234,"cpu_pct":4.2,"mem_mb":512.5}],
 "uptime_s":15180}
```

### 5.3 其它

| 接口 | 说明 |
|---|---|
| `GET /api/health` | `{"ok":true,"board":{state,serial_open,passthrough,...},"pc_ready":bool,"tcp_passthrough_port":N}` |
| `GET /api/shutdown` | 仅 127.0.0.1/::1；返回 `{"ok":true,"bye":true}` 后进程优雅退出 |
| `OPTIONS *` | 204 + CORS 头（`Access-Control-Allow-Origin: *`），便于外部工具调试 |
| 其它 GET | 静态文件（`web/` 目录，路径穿越已防护），未知路径 404 JSON |

### 5.4 TCP 透传 `127.0.0.1:8766`

给 `pc/brain.py` 用的裸串口替代：

- 只监听本机回环，**一次只服务一个客户端**；已有客户端时新连接被立即关闭并记日志。
- **每条连接一个独立线程**（accept 循环只负责接客），所以某个客户端卡住/僵死不会堵住
  accept、不会塞满 backlog；连接异常/EOF 一定走 `finally`：关 socket、清 active、恢复 stats 轮询。
- 连上后把客户端字节**原样**写入 COM7，把 COM7 字节原样写回客户端（JSON-lines 协议不变，
  不额外注入 ready 行，brain.py 侧无需改协议）。
- 切换过程是「两步握手」：先标记 `passthrough_requested` 让 manager 不再发起新轮询，
  再等在途的那次 stats 往返结束（≤0.9 s）后切模式并清空串口/队列 —— 避免 poll 线程
  长期持锁把透传饿死（旧版挂死 bug 的根因）。
- 透传期间 **stats 暂停**；断开后自动恢复，页面短暂标灰，板子面板显示「板子透传中」。
- 串口未打开时直接关闭连接（不假装成功）。
- 实测（live hub，板子在线）：ping 往返 ~16 ms；stats 响应 1186 B 完整透传；
  客户端断开后 `/api/board` 在 2 s 内恢复 `age_ms < 500`。

### 5.5 `POST /api/cmd`（网页按钮 → 板子命令）

`/api/cmd` 让页面能直接给板子发一条 JSON 命令（内部与 stats 轮询共用同一把 `_poll_lock` 串行化，
不会和 1 Hz 轮询抢串口）。

```
POST /api/cmd
Content-Type: application/json

{"cmd":"set","key":"dfs","value":1}
```

白名单：`ping`、`stats`、`set`、`sd_remount`、`sd_test`。
另外 `sd_format`（销毁性）**仅允许 127.0.0.1/::1** 且请求体必须带 `"confirm":"ERASE"`，
其余命令一律 `400 {"ok":false,"err":"cmd_not_allowed", "allowed":[...]}`。

板子离线/被透传占用时返回 `{"ok":false,"err":"board_offline"|"passthrough_active"|"board_busy"}`，
页面据此回滚开关并提示，不会假装成功。

### 5.6 已移除：手机传图接口（2026-10-18 当天撤掉）

曾加过 `GET /upload` + `POST /api/upload` + `GET /api/inbox` 给手机传图用，
但实际价值不大：把图片描述成「坐标文字」再交给 `tools\draw-image.py --from-plan`
更直接，也少一个对外写文件的入口。已从 hub / 页面 / 文档里全部删掉
（需要时可从 git 历史 `06cdfd0` 取回）。

---

## 6. 数据来源与开销（诚实说明）

### 板子（串口）

- 每 1 s 发 `{"cmd":"stats","id":N}`，等响应 ≤0.9 s；同一时刻只有一条在途，不会堆积。
- `cpu.*_pct` 是**两次 stats 之间的窗口占用**（固件用 idle 计数差计算），不是瞬时值；`window_ms` 会显示实际窗口。
- 首次调用窗口不足时固件返回 0（页面显示 0% 而不是错误）。
- 串口只打开一次；掉线/被拔出后 hub 每 2 s 重试重开（重开会复位板子，属 CH340 行为）。

### 本机（全部标准库）

| 指标 | 实现 | 开销 |
|---|---|---|
| CPU 每核/总占用 | `ctypes` 调 `NtQuerySystemInformation(SystemProcessorPerformanceInformation)`，两次采样差 | 微秒级 |
| 内存 | `GlobalMemoryStatusEx` | 微秒级 |
| 开机时长 | `GetTickCount64` | 微秒级 |
| CPU 主频 | 注册表 `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\~MHz`（标称值） | 微秒级 |
| GPU | `nvidia-smi --query-gpu=...`（找不到则返回空数组） | ~60–100 ms/秒 |
| 进程 | **常驻一个 PowerShell 子进程**，每 1 s 输出一行 JSON；CPU% 由进程 `CPU` 时间差 / 核数 计算 | ~100–300 ms/秒 |
| NPU | 启动后后台探测一次 `Get-PnpDevice`（匹配 `NPU/Neural/AI Boost/Ryzen AI`） | 一次性 |

- 所有本机结果缓存 **1 s**，HTTP 请求只读内存缓存，不会因为手机多开几个页面而增加采样开销。
- 进程 `cpu_pct` 第一轮为 `null`（还没有差值），页面显示 `—`；第二轮起有值。
- GPU 若出现 `N/A`（部分笔电独显空闲时不上报利用率），对应字段为 `null`，页面显示 `—`。
- Windows 控制台子进程输出统一用 `_decode_console()`：先 UTF-8、再 GBK、最后 replace 解码，
  避免 PowerShell/netsh 输出编码不一致时把采样线程打挂（旧版 `text=True` 严格 GBK 解码崩过）。
- hub 的 stdout/stderr 强制 `line_buffering=True`，后台重定向到文件时日志不会攒 8 KB 才出。

---

## 7. 自检与证据

```powershell
# 不需要真板子、不需要浏览器；会自己拉起/关闭一个离线模式的 hub
$PY tests\web_selftest.py
```

最近一次结果：**70/70 PASS**（脚本在 `tests/web_selftest.py`，随仓库分发，谁都能跑）。覆盖：

1. **前端静态检查**：三个文件存在、相对路径引用、69 个关键元素 id、app.js 引用的 id 全部存在、
   `api/board`/`api/pc`、1 s 轮询、超时标灰、三种离线原因、透传中提示、v1.2 stats 全字段
   （含可选 `temp_c/temp_warn`）、无外网 CDN/外部字体/外部脚本、手机媒体查询、JS 括号配平。
2. **hub 离线路径**（`--serial COM99 --no-lock --no-firewall --port 0 --tcp-port 0`）：
   `/`、`/app.js`、`/style.css` 200 且 Content-Type 正确；`/api/board` = `board_offline`；
   `/api/pc` 真实数据（32 核 CPU、内存、GPU、进程 CPU%、NPU、开机时长）；
   `/api/health`；OPTIONS/CORS；未知路径 404；透传端口在串口离线时立即断开；
   `/api/shutdown` 后进程 exit 0；`--no-lock` 不碰别人的 `.board.lock`。
3. **解析器单测**：`extract_stats` 兼容嵌套/平铺两种 stats 响应，错误响应返回 None。
4. **透传回归压测（假串口 + 真 TcpBridge，无硬件依赖）**：3 轮 ping+stats 长行往返、
   连上即断的恶意客户端不影响 accept 循环、第二个客户端被拒绝、断开后 stats 自动恢复
   —— 专门锁住旧版「8766 挂死 / CloseWait 堵死 accept」缺陷。

真机联调（live hub + COM7 + v1.2 固件）。下面的原始记录跑在真机上、里面有桌面截图，
所以**留在那台机器上、不随仓库分发**（`docs/evidence/` 被 gitignore），这里只留结论：

- `web-board-online.txt`：**22/22 PASS**。`/api/board` ok=true、
  `stats.proto=v1.2`、CPU/内存/任务/SD/网络/HID/UART 字段齐全、页面读取的 30 条字段路径全在顶层；
  8766 透传 ping+stats 两轮真实回包（原始字节 1341 B，>512 B 长行不截断）；断开后轮询恢复；
  `/api/pc` 同时可用。
- `web-board-raw.json`：真机 `/api/board` 原始响应（可直接当解析样例）。
- `web-hub-fixes.txt`：GBK 解码、NPU/GPU 探测、`lan_ips`、防火墙提示、行缓冲等修复的本地验证输出。

其它原始证据（同样不随仓库分发）：

- `web-hub-curls.txt`：离线路径真实 curl 输出（静态页头、两个 API、health、OPTIONS、404、shutdown）。
- `web-preview.txt`：`python -m http.server` 纯静态预览（此时 api 404，页面进离线态）。

**边界（诚实说明）**：

- 板子在线路径已用真机 + v1.2 固件验证（见上）；透传已用真实 socket 往返验证。
- 本模型没有浏览器/视觉能力，**没有**做像素级渲染/截图验证；页面正确性用 HTML id/字符串/接口断言代替。
- 透传的「brain.py `--transport tcp` 全流程」由 draw 侧联调；hub 侧 8766 的字节级收发已验证。

---

## 8. 故障排查

| 现象 | 处理 |
|---|---|
| `绑定 0.0.0.0:8765 失败` | 8765 被占用（临时预览/旧 hub）→ 先停掉，或 `--port 8767` |
| 页面打开是「板子离线」 | 板子没插 / COM 号不是 COM7 → 设备管理器确认后 `--serial COMx` |
| 页面是「板子被占用」 | `.board.lock` 被 fw-core/draw 持有 → 等它结束；`python tools/board_lock.py status` 看持有者 |
| 页面是「固件无 stats」 | 串口通了但固件旧 → 烧录 v1.2 固件（protocol.md §3.6） |
| 手机打不开 | 同一 Wi-Fi？防火墙入站规则？见 §3；本机 `curl 127.0.0.1:8765/api/health` 对照 |
| 板子数据一直 0% | 固件首次 stats 窗口不足；连续两次调用后会正常 |
| `/api/pc` 进程为空 | PowerShell 启动慢/被杀软拦；看 hub 日志；10 s 内会恢复 |
| 透传连上立刻断开 | 串口未打开（看 `/api/board` 的 `err`）或已有另一个透传客户端 |
| 页面显示「板子透传中」 | brain.py 正占用 8766，属正常；透传结束后自动恢复 |
| hub 退出后板子还在复位 | CH340 打开/关闭会拉 DTR/RTS，属正常硬件行为 |
| hub 日志看不到实时输出 | 代码已强制行缓冲；若仍不显示，启动时加 `PYTHONUNBUFFERED=1` |

---

## 9. 文件清单

| 文件 | 作用 |
|---|---|
| `web/index.html` | 页面骨架（板子面板 + 本机面板），文件名固定，相对路径引用 `app.js`/`style.css` |
| `web/app.js` | 轮询、渲染、canvas 仪表/曲线、超时与离线态（纯原生 JS，无构建） |
| `web/style.css` | 暗色主题、手机竖屏自适应（媒体查询/流式布局） |
| `pc/hub.py` | HTTP + 静态页 + 板子串口采集 + 本机采集 + TCP 透传（标准库，零第三方依赖） |
| `pc/transport_uart.py` | 复用其 DCB/COMMTIMEOUTS 结构与端口名规范化（hub 自己管常驻句柄） |
| `tools/board_lock.py` | 串口跨进程互斥锁（hub 进程级持有） |
| `docs/evidence/web-selftest.py` | 自检脚本（`web-*` 证据命名） |
| `docs/evidence/web-board-online.py` | 真机联调脚本（需要 hub 已在跑 + 板子在线） |
| `docs/evidence/web-hub-fixes.py` | GBK 解码 / NPU·GPU 探测 / 行缓冲修复验证 |

---

## 10. 与「板子托管 HTTP」方案的差异

最初设计是板子自己跑 `esp_http_server` 托管页面（固件曾计划 `EMBED_FILES` 嵌入这三个文件）。
用户不插网线后板子没有网络出口，方案改为 **PC 当服务器**：

| 项 | 旧方案（已放弃） | 当前方案 |
|---|---|---|
| 页面托管 | 板子 :80（EMBED_FILES） | PC `pc/hub.py` :8765 |
| 板子数据 | 板子 `GET /api/stats` | hub 经 COM7 每 1 s 采 `stats` 后 `GET /api/board` |
| 页面地址 | `http://<板子IP>/` | `http://<PC IP>:8765/` |
| 跨域 | 页面 fetch PC 的 8765（CORS） | 全部同源，无跨域 |
| 串口独占 | 无（PC 其他脚本可随意开 COM7） | hub 持锁独占，其它程序走 8766 透传或等锁 |

`web/` 下三个文件名保持不变（便于将来板子恢复网络出口时直接复用）。

### 10.1 2026-10-17 联调修复记录

真机联调（v1.2 固件 + live hub）暴露并修复了 3 个 hub 缺陷：

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 8766 透传：客户端连上收不到数据，socket 停 CloseWait，accept 被占死、backlog 满后新连接超时 | ① `_manager` 把 `_poll_lock` 连同 1 s sleep 一起持有，透传线程抢锁被饿死；② accept 循环里直接 `_serve_client`，一个卡住的客户端堵死所有后续连接；③ `TcpBridge._stop` 覆盖了 `Thread._stop()` 方法 | 轮询只在这个锁里做「发命令→等响应」，sleep 移到锁外；透传前先 `passthrough_requested` 举手，再等 ≤0.9 s 在途往返；每条连接起独立线程、accept 只管接客；连接生命周期 try/finally 必恢复轮询；`_stop` 改名 `_stop_event` |
| 2 | 后台管道线程 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x87`，NPU/PowerShell 采样线程挂 | `subprocess.run(..., text=True)` 用系统 GBK 严格解码，而 PowerShell 输出是 UTF-8 | 新增 `_decode_console()`（UTF-8 → GBK → replace），所有子进程改为 `capture_output=True` 拿 bytes 再解码 |
| 3 | 后台跑时 stderr 攒够 8 KB 才输出，排障困难 | Python stdout/stderr 重定向到文件时默认块缓冲 | `reconfigure(errors="replace", line_buffering=True)`；`logging` 的 StreamHandler 本就每条 flush |

修复后回归：`docs/evidence/web-selftest.py` 69/69 PASS（含假串口透传压测），
真机 `docs/evidence/web-board-online.py` 22/22 PASS（stats + 8766 ping/stats 往返）。
**注意**：修复在 hub 进程启动时加载，改完代码需重启 hub 生效（`curl http://127.0.0.1:8765/api/shutdown` 后再启动）。


### 10.2 2026-10-18：页面单栏化 + 全端适配 + 后台不再弹 CMD

**1) 本机面板移除后的布局适配（页面只剩板子面板）**

| 问题 | 修复 |
|---|---|
| `.grid` 仍是两列（`1.18fr 0.82fr`），单面板只占约 59% 宽，右侧大片空白 | 改为 `grid-template-columns: minmax(0, 1fr)` 单栏满宽 |
| 板子内 `.cards-2` 固定 2 列，宽屏下每张卡被拉到 700+ px，很空 | 改为 `repeat(auto-fit, minmax(280px, 1fr))`：宽屏 4 列 / 平板 2 列 / 手机 1 列 |
| DFS 开关 46×26 px，手机上偏小不好点 | 放大到 52×30，圆钮 24 px，触控目标 ≥ 28 px |
| 浏览器请求 `/favicon.ico` 返回 404（页面 console 里一条报错） | 新增本地 `web/favicon.svg`（内联图形，无外网依赖）并在 `<head>` 引用 |

**2) 全端适配与页面功能自检（新工具）**

```bash
python tools/web-layout-check.py --url http://127.0.0.1:8765/
# headless Edge + CDP，纯标准库；退出码 0 = 全过
```

- 视口：1920×1080 / 1440×900 / 1024×768 / 820×1180 / 390×844 / 360×740，全部 **PASS**；
- 每个视口断言：无横向溢出、板子面板填充率 ≥ 92%、关键元素可见、
  无元素越界、无 console error / 未捕获异常 / 资源 404、触控目标 ≥ 28 px；
- 功能段（真实点击）：读 `api/board` → 点 DFS 开关 → 后端 governor 变 `adaptive`、
  UI 与后端一致、提示文案更新、`measured_mhz` 变成 83 → 再点回 `fixed360` →
  点刷新按钮 → 页面仍在线。**全部 PASS**；
- 回归：`docs/evidence/web-selftest.py` **70/70 PASS**。

**3) Windows 老弹 CMD 窗口（根因与修复）**

- 根因：后台是计划任务跑的 —— `dsh-hub` 直接跑 `start-hub.cmd`、旧 `dsh-hub-watch` 每分钟跑
  `powershell.exe`，两者都会在用户会话里创建**可见的控制台窗口**，所以每分钟闪一下；
  另外 00:28 启动的旧 hub 还留了一个可见 `cmd` 窗口在桌面上。
- 修复：
  - 两个任务都改成 `wscript.exe //B "C:\ProgramData\dsh\run-hidden.vbs" "<脚本>.cmd"`，
    VBS 用 `WScript.Shell.Run(..., 0, False)` 以**隐藏窗口**方式拉起；
  - 看门狗改用 Python 重写（`C:\ProgramData\dsh\watchdog-hub.py`）并由 `pythonw.exe` 运行，
    彻底没有控制台（旧 PowerShell 看门狗脚本已废弃）；
  - 删掉遗留任务 `dsh-soak`、`dsh-preview`；旧的可见 `cmd`+`python`（hub）进程已终止并按隐藏方式重启；
  - 实测：看门狗跑完一轮后枚举桌面窗口，`cmd/powershell/python/conhost` **可见窗口 0 个**。

**4) 顺手修掉的真 bug：僵尸板子锁把 hub 卡死 15 分钟**

- 现象：hub 被强杀后 `.board.lock` 残留（`pid` 已不存在），新 hub 一直报
  `board_busy: 板子锁被占用 ... for 812s`，最长要等 `STALE_SECONDS=900` 才自愈。
- 修复（`tools/board_lock.py`）：新增 `_pid_alive()`（Windows 用 `OpenProcess` +
  `GetExitCodeProcess`，**不**用有杀进程语义的 `os.kill`）与 `_is_stale()`：
  锁持有者进程已死且超过 3 s 宽限 → 立即判为僵尸锁并抢占；`status()` 现在返回
  `stale / stale_reason / holder_alive`。
- 实测：`held by web-ui:hub` 死 pid 64416 → `status` 报
  `"stale": true, "stale_reason": "holder pid 64416 is dead"`；`acquire` 立即破锁成功，
  hub 重启后板子恢复在线（`ok=true`）。
