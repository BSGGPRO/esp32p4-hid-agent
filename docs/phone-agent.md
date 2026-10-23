# 手机控制（phone agent）：拿这块板子操作 iPhone / 安卓

> 一句话：**板子的原生 USB 口插手机 → 手机看到的是一副真 USB 键鼠；CH340 口插电脑 → 电脑下命令。
> 固件一行不改。** 眼睛（手机画面）走 PC 上的 AirPlay 接收器。

移植自 [ESP32 iPhone Agent by Henry Yuan](https://github.com/onehumanbeing/esp32-iphone-agent)
（MIT）的 `server/hidagent/{phone,vision}.py`——原版那套"相对指针 + 标定 + 归零 + 看屏纠偏"
是对的，这里只把传输层从 BLE 换成这块 P4 板子的 JSON-lines 协议。

---

## 1. 接线

```
iPhone ──USB-C──▶ 板子「原生 USB 口」（枚举成 HID 键盘+鼠标）
PC     ──USB-C──▶ 板子「转 UART 口 / CH340」（COM7，JSON 命令）
```

两个口是**独立的**（本机实测：CH340 直连 PC 根集线器 `Port_#0001.Hub_#0001`，
P4 HID 走另一颗 Genesys 集线器 `Port_#0002.Hub_#0008`），所以能同时插两台主机。

> 代价：把 HID 口插到手机时，**"控制 Windows"那套功能暂停**（板子的键鼠不在 PC 上了）。
> 换回 PC 即恢复，两套功能互不影响。

手机侧前置：

| 机型 | 需要什么 |
|---|---|
| iPhone 15 及以后（USB-C） | 直接插；**设置 → 辅助功能 → 触控 → 辅助触控 打开**（不开就没有屏幕指针） |
| iPhone 14 及以前（Lightning） | 外加原装/MFi 的 *Lightning 转 USB 相机转接头*，再开辅助触控 |
| 安卓 | 一般走 OTG，多数系统直接出现指针（无辅助触控概念） |

---

## 2. 眼睛：PC 上的 AirPlay 接收器

Windows 没有系统自带 AirPlay 接收器，必须装一个。本机已装并验证：

```
%LOCALAPPDATA%\esp32p4-phone\airplay\airplay-windows.exe
```

这是 [`moieric11/AirPlay-Windows`](https://github.com/moieric11/AirPlay-Windows) v0.4.1
（MIT，基于 UxPlay）——**免 Apple Bonjour、免管理员**，用 Windows 原生 mDNS 广播。
启动（用户态，不要管理员）：

```powershell
Start-Process "$env:LOCALAPPDATA\esp32p4-phone\airplay\airplay-windows.exe" -ArgumentList '--log'
```

手机：控制中心 → 屏幕镜像 → 选 **AirPlay-Windows**。

验证它在广播（不接手机也能查）：

```bash
python - <<'PY'
from zeroconf import Zeroconf, ServiceBrowser
zc = Zeroconf(); ServiceBrowser(zc, ["_airplay._tcp.local."], type("L",(object,),{
  "add_service": lambda s,z,t,n: print("FOUND:", n), "update_service": lambda *a: None,
  "remove_service": lambda *a: None})()); import time; time.sleep(6); zc.close()
PY
```

实测输出：`AirPlay-Windows._airplay._tcp.local.`，`model=AppleTV3,2`，监听 `[::]:7000`。

> 本机还开着 **Radmin VPN（26.x 网段）**，Windows 的 mDNS 会把广播地址选到 VPN 上，
> 手机就会连不上。镜像时**先退出 Radmin VPN**，或确认广播里出现 `192.168.1.221`（WLAN 地址）。

---

## 3. 三步用起来

```bash
# 0) 看状态：板子通不通、接收器窗口找没找到、标定过没有
python tools/iphone.py status

# 1) 看手机画面（存 PNG；会报视频区的真实屏幕坐标 video_box）
python tools/iphone.py see
python tools/iphone.py box                    # 看已存的视频区偏移
python tools/iphone.py box --set 468,177,968,1261   # 量一次存下来（窗口客户区内的 dx1,dy1,dx2,dy2）

# 2) 标定：让引擎知道"跨过整个屏幕要多少鼠标单位"
python tools/iphone.py calibrate            # 自动：归零后走两段已知步数，看指针落点
python tools/iphone.py calibrate --finish 0.5,0.45   # 自动找不到指针时：手工报指针所在比例

# 3) 干活（坐标是屏幕比例 0..1，左上原点）
python tools/iphone.py tap 0.5 0.85         # 点
python tools/iphone.py tap 0.5 0.85 --aim   # 点，且点前看屏纠偏（慢、准）
python tools/iphone.py swipe 0.5 0.9 0.5 0.3
python tools/iphone.py drag "0.2,0.8;0.5,0.5;0.8,0.2"
python tools/iphone.py type "hello world"   # 仅 ASCII 0x20-0x7E
python tools/iphone.py key enter
python tools/iphone.py home                 # 底部上滑回主屏
python tools/iphone.py back                 # 左缘右滑返回
```

批量（一次连接跑完，比逐条快）：

```json
[
  {"op": "tap",  "x": 0.5, "y": 0.85},
  {"op": "type", "text": "hello"},
  {"op": "wait", "ms": 600},
  {"op": "see",  "path": "inbox/phone-1.png"}
]
```

```bash
python tools/iphone.py run plan.json --aim
```

MCP 客户端里只有一个新工具：

```
board_phone  {"op": "tap", "x": 0.5, "y": 0.85, "aim": true}
```

`op` 取值：`status / see / calibrate / tap / swipe / drag / scroll / type / key / home / back / reset / run`。

---

## 4. 原理（为什么必须标定）

手机只认**相对移动**：只能告诉板子"往右 X、往下 Y"，既没有绝对坐标，也读不回指针在哪。
所以引擎做了四件事（`pc/phone.py`）：

| 机制 | 作用 | 代码 |
|---|---|---|
| 标定 `span` | 归零后走 150 / 350 单位，看指针落点 → 算出跨整屏的鼠标单位数 | `start_calibration()` |
| 记账 `_pos` | 每次 move 后本地累加，避免每步归零 | `_move_to()` |
| 归零 `home` | 每 10 个手势大力甩到左上角（手机侧夹住），清漂移 | `home()` |
| 纠偏 `_aim` | 点前微抖 12 单位、两帧截图找指针，补误差并记进线性拟合 | `_aim()` / `_fix()` |

`_fix()` 那条线性拟合同时吃掉了**固定偏移**（窗口标题栏、黑边裁切残差），
所以标定两次之后越用越准；落点误差上限靠 `AIM_TOLERANCE=0.008`（屏幕 0.8%）控制。

镜像有 0.3~1 s 延迟，取帧前默认等 `--latency 0.35` 秒，可用 `--latency` 调。

---

## 5. 为什么不能"完美照搬"原版

原版是三层：**板子固件做 AirPlay 接收 + BLE**、**手机 App 做大脑**、**USB HID 做手**。逐层对照本机：

| 原版组件 | 这块 P4 板子 | 结论 |
|---|---|---|
| USB HID 手 | 已经是真 USB 键鼠，协议里有 `mouse_move / mouse_btn down\|up / mouse_scroll / text / key_tap` | ✅ 原样可用，**手势引擎不需要改固件** |
| 指针闭环逻辑 | 纯 Python，命令词与本板协议一一对应 | ✅ 整段移植（`pc/phone.py`） |
| 大脑（视觉模型 + 规划） | 本仓库已有 MCP/CLI 工具面，AI 直接当大脑 | ✅ 做成 `board_phone` 工具，不用塞 App |
| 板子端 AirPlay 接收 | 固件只读（红线：不编译不烧录）；且 **P4 本身无 Wi-Fi 射频**（板上虽有一颗 C6 走 SDIO，用起来=改固件） | ❌ 搬不了 |
| 手机 App 侧 | 需 Mac + Xcode + 签名 | ❌ 本机做不到 |

替代方案就是本文这套：**眼睛搬到 PC（AirPlay 接收器）+ 手仍然是板子的 HID + 大脑是本机 AI**，
效果等价，且一行固件都没动。

---

## 6. 限制（先知道，别踩）

- **`type` 只发 ASCII 0x20–0x7E**（固件限制）。中文得用系统输入法的候选，或先切输入法再逐字拼音。
- **首屏/锁屏点不亮**：屏幕灭了先用 `key` 发一个键或 `tap` 一下唤醒；iOS 的锁屏密码要人工过。
- **不要盲点支付/删除/发送**：仓库安全红线同样适用（不可逆动作先问人）。
- **指针归零靠"撞左上角"**：iOS 会把指针夹在屏幕边，所以 `home` 之后位置确定；但**中途切 App 或旋转屏幕**会让 `_pos` 失效 → 重要操作前先 `reset`。
- **滚轮方向**是 HID 语义（正=上滚），与"内容方向"可能相反。
- **长按/拖拽**靠 `HOLD=0.25s`，太短 iOS 会当"甩"，太长会触发菜单。
- **镜像延迟**：`--latency` 给小了，`_aim` 会读到旧帧、纠偏反而更差。
- **竖屏手机塞进横窗口**会自动裁黑边（`trim_letterbox`）；如果接收器窗口本身被拖出屏幕，本工具会把它挪回屏幕内（**只动我们自己拉起的那个窗口**，别人的镜像窗口一律只报错）。
- **指针光标不进 AirPlay 投屏流**（2026-10-23 实测，iPhone + AirPlay-Windows）：手机侧点击、上滑、长按都会生效（点击 12.5 万像素变化、上滑 20.6 万像素变化），但**纯粹移动指针时截帧一像素都不变**——iOS 把光标画在投屏流之外。所以：
  - 自动标定（靠"看见指针"）在真机上会报「没在屏幕上找到指针」，这不是 bug，是看不到；
  - 判断输入是否生效只能看**点击后的画面变化**，不能看光标；
  - 也**不要为此改固件**：iOS 已经正常挂载并接受 HID（`host_mounted:true`、点击有反应），"iOS 不认这个描述符"这条假设已证伪，改描述符解决不了看不见光标的问题。
- **一条 `mouse_move` 的耗时与位移成正比**：固件按 ≤127 单位/报告拆分，而手机侧约 100 ms 才取走一个报告，所以 4000 单位的甩动约 3.2 s。`AgentBridge.mouse_move` 已按报告数自动放宽超时，别手工调小 `timeout`。

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `找不到手机镜像窗口` | 接收器没起，或窗口标题不含 airplay/mirror；用 `--rect x1,y1,x2,y2` 直接指定 |
| 手机镜像列表里看不到 PC | Radmin VPN 抢了 mDNS；或防火墙没放行；或不在同一网段 |
| `指针还没标定` | 先 `calibrate`；自动失败就用 `--finish x,y` |
| 点击位置偏 2~3% | 再跑一次 `calibrate`；或用 `--aim`；确认窗口没被缩放 |
| 点着点着越来越偏 | 中途切过 App/转屏；`reset` 后重来 |
| `host_mounted:false` | 手机没认到 HID：换数据线/转接头，或 iOS 需要先解锁再插 |
| 移动了但投屏画面没变化 | **正常**：iOS 的指针层不进 AirPlay 流。用点击/上滑后画面是否变化来判断输入是否生效 |
| 自动标定报「没在屏幕上找到指针」 | 同上：看不到光标。用 `calibrate --finish x,y` 人工给一个落点，或改用点击反馈估灵敏度 |

---

## 8. 文件

| 文件 | 作用 |
|---|---|
| `pc/phone.py` | 手势引擎：相对指针记账、标定、归零、看屏纠偏、镜像窗口定位与裁黑边 |
| `tools/iphone.py` | CLI（`status/see/calibrate/tap/swipe/drag/scroll/type/key/home/back/reset/run`） |
| `tools/mcp_server.py` | 新增 `board_phone` 工具（MCP 客户端可用） |
| `tests/phone_pointer.py` | 离线自检：指针识别、归零记账、线性纠偏、拖拽按下抬起、黑边裁切（不需要板子和手机） |

```bash
python tests/phone_pointer.py     # 期望 6/6 passed
```

---

## 9. 致谢 / 许可

本目录的手机控制部分是**移植件**，按上游要求保留署名：

- **ESP32 iPhone Agent by Henry Yuan** — <https://github.com/onehumanbeing/esp32-iphone-agent>（MIT）。
  `pc/phone.py` 的指针标定/归零/纠偏算法、`tools/iphone.py` 的手势语义都来自它的
  `server/hidagent/phone.py` 与 `vision.py`。
- **[AirPlay-Windows](https://github.com/moieric11/AirPlay-Windows)**（MIT，基于
  [UxPlay](https://github.com/FDH2/UxPlay)，UxPlay 为 GPL-3.0）—— 仅作为独立进程安装在本机，
  不并入本仓库源码。
