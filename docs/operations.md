# 操作手册：用这块 ESP32-P4 板子操控 Windows（USB 键鼠）

> 5 分钟上手。读完你就能：截屏 → 定位 → 点击/拖拽 → 打字 → 验证结果。
> 板子是 **USB HID 复合设备（键盘+鼠标）**，插在这台 Windows 上，等于一只"电子的手"。

---

## 0. 三条硬前提（先确认，否则一定失败）

| # | 前提 | 怎么确认 |
|---|---|---|
| 1 | **必须能在这台 Windows 上执行命令**（本地 shell / 终端工具调用） | 下面「自检」5 条命令都能跑通 |
| 2 | hub 在跑（它占着 COM7 并开 TCP 透传） | `curl http://127.0.0.1:8765/api/health` → `{"ok":true,...}` |
| 3 | **一律用 `--transport tcp`**（走 hub 的 8766 透传），不要直连 COM7 | 直连会撞板子锁 `board_busy` |

> 板子串口**全局只有一把锁**：同一时刻只能有一个进程用。hub 常驻占用 → 你走 tcp 透传，
> 由 hub 帮你转发（一次只服务一个客户端，用完即放）。

### 环境（绝对路径，照抄）

```
仓库     <repo>
Python   %USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
         （3.12；有 Pillow；没有 pyserial —— 用 tcp 透传不需要）
hub      http://127.0.0.1:8765        （局域网/手机：http://192.168.1.221:8765）
透传     127.0.0.1:8766                （brain.py 的 --transport tcp 就连这里）
板子     JC-ESP32P4-M3-DEV，固件 v1.2.0 / proto 1.2，USB VID_303A PID_4004
串口     COM7 @115200，JSON-lines（不要直接抢）
```

---

## 0.5 操作约定（这台机器的固定做法）

| 角色 | 用什么 | 说明 |
|---|---|---|
| **手** | 板子（USB HID 键鼠，`brain.py --transport tcp`） | 所有键鼠操作都走硬件，不用软件注入 |
| **眼** | 本地视觉模型（可选，见下方） | 回答「这块区域是什么」这类判断题 |
| **事实** | Win32 窗口列表 + RapidOCR | 标题、坐标、数字以它为准；模型会编 |

### 唯一入口：`tools/connector.py`

要做的事写成一份 JSON，从 stdin 喂进连接器，执行完即结束，**不落任何文件**：

```json
{"steps": [
  {"op": "restore", "title": "微信"},
  {"op": "find", "text": "文件传输助手", "rect": [1200, 120, 2290, 400], "as": "hdr"},
  {"op": "see", "q": "这块区域是什么界面？", "rect": [1200, 120, 2290, 500]},
  {"op": "recipe", "name": "wechat-send", "to": "文件传输助手", "pieces": ["py:nihao"]},
  {"op": "recipe", "name": "open-url", "url": "https://www.bilibili.com", "expect": "哔哩哔哩"}
]}
```

操作：`restore` / `focus` / `title` / `ui` / `key` / `type` / `click` / `point` / `move` / `drag` /
`drag_path` / `hold` / `scroll` / `wait` / `look` / `ocr` / `find` / `see` / `check` / `vision` /
`diff` / `wait_change` / `read_text` / `clip` / `launch` / `recipe`。

- **找不到窗口就按进程名找**：播放器/编辑器的标题是当前内容（网易云的标题就是歌名），
  `title` 子串永远匹配不上。所有窗口操作都认 `exe=`：`{"op":"focus","exe":"cloudmusic"}`。
- **陌生界面先 `ui`，不要猜坐标**：`{"op":"ui","exe":"cloudmusic"}` 返回这个窗口的
  `texts`（文字行 + 屏幕中心）和 `buttons`（亮块找出的按钮 + 屏幕中心）。用 `PrintWindow`
  渲染，窗口被挡住也能读。图标没有文字，OCR 看不见——这一步就是为了解决那个。
- 视觉模型和 OCR 引擎都跑在**常驻服务**里（`tools/visiond.py`）。OCR 启动即用（冷启动约 5 秒）；
  视觉模型**第一次 `see` 才加载**（另加约 11 秒，2.5 GB 显存），不用就不占。
- **显存会还**：模型空转 3 分钟自动卸载（进程和 OCR 留着），下次用自动装回。
  打游戏前想立刻还：`python tools/visiond.py --unload`（实测全卡 3780 → 1171 MiB）。
- 读字要快：`{"op":"ocr","rect":[x0,y0,x1,y1]}` 小框约 0.12 秒；框越小越快，别整屏 OCR。
- 答题要快：`{"op":"see","q":"...","rect":[...],"speed":"fast"}` 约 1.2 秒（fast=480 长边），
  默认 640 约 1.7 秒，`speed:"fine"` 才用 1280（3~4 秒）。
- 同一作业里多次 `ocr` / `find` 复用同一张截图；中间只要有按键/点击/等待就会重截，不会看到旧画面。
- 能用 `ocr` / `find` 回答的别问模型：文字和坐标以 OCR 为准，模型只做判断题。
- 模型会把「任务栏里有某图标」说成「前台是某程序」——前台窗口这类事实用 Win32 查。

### 安全红线

1. **不动用户窗口**：不移动、不缩放、不最大化；只为目标窗口做还原 / 置前。
2. **动手前核对目标**：窗口标题、会话标题必须先 OCR/Win32 确认，对不上就停手。
3. 一次只用一个透传客户端，用完立刻 `release()`；ESC 必须能立刻停。
4. 中文走输入法：`py:<拼音>` 出候选再选字，`raw:<ascii>` 直接敲字母；微信里 Ctrl+V 粘不进去。
5. 不可逆动作（发消息、删文件、转账）一律先核对再落键。
6. 独占全屏程序（游戏等）在前台时不投键鼠：按键会被吞掉。**这条现在由代码强制**——
   `connector` 每条输入动作前都会判断前台是否全屏，是就直接报错说清楚；确实要操作加 `"force": true`。
   同一时刻视觉模型也会在 5 秒内自动卸载，把显存还给游戏（`DSV_GAME_MODE=0` 可关）。

---

## 1. 自检（先跑一条，再按需看单条）

```bat
cd /d <repo>
:: ⓪ 一条命令看五项（屏幕 / OCR / 板子 / 指针 / 视觉），全过则退出码 0
"%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" tools\connector.py --selfcheck
```

然后是按需的单条：

```bat
cd /d <repo>

:: ① 板子活着 + 屏幕/光标信息
"%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" pc\brain.py --transport tcp --json info

:: ② 截一张屏（眼睛）
...\python.exe pc\brain.py --transport tcp --json look
::  → 生成 pc\shot.png（2560x1440），JSON 里有 path/宽高/光标/前台窗口/窗口列表

:: ③ 列窗口（找画图 / 找目标窗口）
...\python.exe pc\brain.py --transport tcp --json wins

:: ④ 把光标移到绝对坐标（闭环，误差 ≈1px）
...\python.exe pc\brain.py --transport tcp --json point 640 360

:: ⑤ 读 HID 状态（确认没有按住的键）
...\python.exe pc\brain.py --transport tcp --json info
```

> 上面 `...\python.exe` 就是那条完整 Python 路径。**注意 `--json` 要放在子命令前面**
> （`brain.py --transport tcp --json info`），否则 argparse 报错。

---

## 2. 三种入口，按需选

| 入口 | 用途 | 限制 |
|---|---|---|
| `pc/brain.py <子命令>` | **推荐**：高层动作 + 闭环定位 + JSON 输出 | 一次一个动作，每次连一次透传（约 0.1–0.3 s） |
| `pc/brain.py run --script plan.json` | **批量**：一次连接串联几十步（画画用这个） | 某个 op 失败默认中止（`--keep-going` 可继续） |
| `pc/agent_bridge.py <命令>` | 低层协议调试（ping/mouse-move/key-combo…） | `--transport uart` 要板子锁；hub 在跑时用不了。**画画别用它** |
| `GET /api/board` `/api/pc` | 只读状态（CPU/TF 卡/温度…） | 只读 |
| `POST /api/cmd` | 网页那个开关（白名单：ping/stats/set/sd_*/mouse_move） | **`mouse_move` 被安全限制为 `dx=dy=0`**，且没有真实移动，**不能用来画画**；远程 HTTP 不能操作桌面 |

---

## 3. 动作清单（brain.py 子命令 / run 的 op）

| 子命令 | 等价 op | 参数 | 例子 |
|---|---|---|---|
| `look` | `look` | `[--region x,y,w,h]` | `--json look` |
| `point` | `point` | `x y [--tol 2]` | `point 100 200` |
| `locate` | `locate` | `x y [--tol 1]` | `locate 100 200` |
| `move` | `move` | `dx dy`（相对） | `move 10 -5` |
| `click` | `click` | `[x y] [--button left/right/middle] [--count N]` | `click 300 400` |
| `drag` | `drag` | `x1 y1 x2 y2` | `drag 100 100 400 400` |
| `drag-path` | `drag_path` | `"x1,y1;x2,y2;..."` 或 JSON 数组文件 | 见下 |
| `scroll` | `scroll` | `amount`（正=向上） | `scroll -3` |
| `type` | `type` | `text [--cps 40]`（仅 ASCII 0x20-0x7E） | `type "hello"` |
| `key` | `key` | `combo`（`ctrl+s` / `win+r`） | `key ctrl+z` |
| `tap` | `tap` | `key [--hold-ms]` | `tap enter` |
| `wins` | `wins` | — | `--json wins` |
| `focus` | `focus` | `title`（子串） | `focus 画图` |
| `read-text` | `read_text` | `[--no-select-all]` | 读前台窗口文字 |
| `clip` | `clip_get/clip_set` | `get` / `set "..."` | |
| `info` | `info` | — | `--json info` |
| `run` | — | `--script plan.json [--keep-going]` | 批量 |

### `run` 脚本格式（一次连接画完整幅画）

```json
[
  {"op": "launch", "cmd": "mspaint"},
  {"op": "sleep",  "ms": 1200},
  {"op": "focus",  "title": "画图"},
  {"op": "drag_path", "button": "left",
   "points": [[300,300],[400,300],[400,400],[300,400],[300,300]]},
  {"op": "look", "path": "after.png"},
  {"op": "diff", "a": "before.png", "b": "after.png"}
]
```

`drag_path` 是**一次按下-连续移动-抬起**，才是连续线条；`drag` 是"定位→按下→定位→抬起"，会断。

---

## 4. 画画推荐流程（多模态 AI 版）

1. `look` 截图 → 你（能看图）自己看画布在哪、有没有遮挡；
2. `wins` 找窗口 → `focus 画图`（**不要最大化、不要移动窗口**，用户要同时看聊天窗）；
3. 在截图上量出画布矩形 → 规划折线（每笔点列）；
4. 写成 `plan.json` → `run --script plan.json`（几十步一次跑完，比逐个命令快 10 倍）；
5. 画完 `look` 再截一张 → 自己看图，或 `diff` 做像素差分验证；
6. 汇报：落点误差、耗时、画了多少笔。

现成参考实现（复杂图、含窗口检测/防遮挡/线宽自适应）：
`tools/draw-rage.py --detect`（只检测不动笔）、`--plan`（只打印笔画）、`--mock`（离屏验证几何）。

精度：**闭环落点 ≈1 px**；用的是相对鼠标 + `GetCursorPos()` 反馈闭环，
所以**不要自己算"发多少步能到目标"**，直接 `point/locate/drag_path`，让 brain 收敛。
细节见 `docs/pointer-calibration.md`（含 CNC 式四角归零与换设备自动适配）。

---

## 5. 安全红线（违反会直接干扰用户）

1. **只在用户明确要求时操作**键鼠；不要"顺手"点一下或打字。
2. **不要往聊天窗口打字**（历史事故：把 `a` 打进了用户正在用的窗口）。
3. 每条命令前/后都有 `release_all`；自己写脚本时开头结尾也要各来一次。
4. **不最大化、不移动、不缩放**用户的画图/聊天窗口。
5. 不点未知按钮、不关窗口、不改系统设置（不碰注册表/指针加速设置）。
6. 一次只跑一个客户端：透传是独占的，别并发开两个。
7. 板子异常（`board_busy` / 超时）就**停手报告**，不要反复重试烧串口。
8. **别动固件代码**：不要改 `firmware/` 下任何文件、不要重新编译、不要烧录/刷板子，
   也不要改 `web/` 网页代码。固件（v1.2.0，功能/稳定性/动态调频开关实机验证）和网页
   （6 视口 + 70 项自检全过）都已确认无已知问题——它们是**工具**，不是待修的项目。
   要报问题只说现象和复现步骤，不要自己上手改。

---

## 6. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `WinError 5 端口被占用` | 直连 COM7，hub 占着 | 加 `--transport tcp` |
| `board_busy: 板子锁被占用` | 有别的进程/僵尸锁 | 等下一轮看门狗（≤1 min）；或 `python tools/board_lock.py status` |
| 截图尺寸是 2048×1152 而不是 2560×1440 | 进程没开 DPI 感知（125% 缩放） | 用 `brain.py`/`eyes.py`（内部已 `enable_dpi_awareness`）；自己写代码要先开 |
| 移动"少走/多走" | Windows「提高指针精确度」非线性 | 用闭环 `point/locate`，别开环算步数 |
| 透传期间网页数字不动 | 透传独占串口，hub 暂停 stats 轮询 | 正常现象，断开后自动恢复（≤1 s） |
| 换了一台 1080p 电脑 | 指针增益变了 | `python tools/home-calibrate.py --transport tcp --apply`（10 s，会短暂占用鼠标），之后自动生效 |

---

## 7. 文档索引（全部在本仓库 `docs/`）

| 文件 | 内容 |
|---|---|
| `docs/agent-handoff.md` | 本文（交接说明） |
| `HANDOFF-TO-AI.txt`（仓库根） | **全量单文件版**：一个文件包含交接/接口/画图流程/协议速查/板子事实/排障，直接发给另一个 AI |
| `docs/protocol.md` | 串口 JSON 协议（帧格式/命令/错误码/安全语义） |
| `docs/brain-usage.md` | brain.py 动作层详解与示例脚本 |
| `docs/pointer-calibration.md` | 指针精度、四角归零、换设备适配、绝对坐标 HID 路线 |
| `docs/web-dashboard.md` | hub/网页/接口/TCP 透传/部署（含看门狗与计划任务） |
| `docs/hardware-facts.md` | 板子硬件事实（引脚/TF 卡/USB/电源等） |
