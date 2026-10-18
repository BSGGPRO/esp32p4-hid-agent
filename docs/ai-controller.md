# 板子控制器 · 给 AI 的说明书

> 你被叫来操作这台 Windows。这台机器上接了一块 **ESP32-P4 开发板**，它是一副**真的 USB 键盘鼠标**。
> 你负责看和判断，板子负责按键和移鼠标——因为没有驱动、没有注入，目标程序看到的就是真人在操作。

**版本** 1.0.0 · 26 个操作 · 源码 `github.com/BSGGPRO/esp32p4-hid-agent`
**这份文件就是全部知识**：`esp32p4-agent.exe --manual` 打印的就是它。

---

## 0. 三十秒上手

```bash
esp32p4-agent.exe --services     # hub / 视觉服务 / 板子 三处在不在
esp32p4-agent.exe --selfcheck    # 屏幕 / OCR / 板子 / 指针 / 视觉 五项
esp32p4-agent.exe --caps         # 机器可读的能力清单（操作表 + 规矩 + 服务状态）

# 然后就能用：读屏幕左上角 600×200 那块的文字
echo '{"steps":[{"op":"ocr","rect":[0,0,600,200]}]}' | esp32p4-agent.exe
```

输出永远是 JSON；**任何一步核对不上就整单失败并停手**，不会带着错误继续按键。
退出码：`0` 成功、`1` 作业失败（看 `err`）、`2` 输入有问题。

---

## 1. 硬规矩

违反其中任何一条，这个任务就算失败。

| # | 规矩 | 具体意思 |
|---|---|---|
| 1 | **禁止改固件** | `firmware/` 是**只读**的。不许编译、烧录、刷写，不许改任何 `.c` / `.h` / `sdkconfig` / `partitions.csv`。需要改固件就先停下来问人，别自己动手。固件已经在板子上跑着，改动等于把整台机器的"手"弄断。 |
| 2 | **不动用户窗口** | 只能 `restore`（从最小化还原）和 `focus`（置前）。**不许**最大化、移动、缩放别人的窗口。 |
| 3 | **全屏不投键鼠** | 前台是全屏应用（游戏、全屏视频）时不发任何输入，除非那一步显式写 `"force": true`。 |
| 4 | **不可逆先问** | 删除、发送、支付、关机、给真人发消息——先停下问人，别替人决定。 |
| 5 | **一条一条来** | 动手前先看（`ocr` / `find` / `ui`），动完核对（`title` / `ocr`）。核对不上就停，**不重试输入动作**——宁可报错，也不赌第二次点击。 |
| 6 | **别猜坐标** | 坐标只能来自 `ocr` / `find` / `ui`。视觉模型（`see`）报的坐标是编的，只用它回答是/否和"这是什么"。 |
| 7 | **一次一个客户端** | 串口同时只能一个进程占。先跑 `--services`，`board_tcp.up` 或 `hub.serial_open` 为真就行。 |

---

## 2. 拓扑

```
   ┌──────────────┐    USB HID（键盘+鼠标，VID_303A）   ┌──────────────┐
   │ ESP32-P4 板子 │ ────────────────────────────────▶ │  这台 Windows │
   └──────┬───────┘                                   └──────▲───────┘
          │ UART 115200（COM7 之类）                          │ 截图 / OCR
   ┌──────▼──────────────────────────────────────────────────┴───────┐
   │ hub（8765 网页 + 8766 TCP 透传）  ←  控制器用它把作业送到板子      │
   │ visiond（8771 OCR / 视觉问答，常驻，别自己起第二个）              │
   └─────────────────────────────────────────────────────────────────┘
```

| 服务 | 地址 | 作用 | 不在会怎样 |
|---|---|---|---|
| hub | `http://127.0.0.1:8765`，TCP `127.0.0.1:8766` | 串口桥 + 网页仪表盘 | 控制器自动改成直连串口（`transport=serial`） |
| visiond | `http://127.0.0.1:8771` | 常驻 OCR、视觉问答 | `ocr` / `find` / `ui` / `see` 不可用；其它操作照常 |

屏幕是 **2560×1440**，**所有坐标都是物理像素**，不是缩放后的逻辑像素。

---

## 3. 怎么调用

| 命令 | 用途 |
|---|---|
| `esp32p4-agent.exe --manual` | 打印这份说明书 |
| `esp32p4-agent.exe --caps` | 能力清单 JSON（给程序读） |
| `esp32p4-agent.exe --services` | 三处服务状态 JSON |
| `esp32p4-agent.exe --selfcheck` | 五项自检，`ok` 为真才算能用 |
| `type job.json \| esp32p4-agent.exe` | 执行作业（推荐） |
| `esp32p4-agent.exe job.json` | 同上，从文件读 |
| `esp32p4-agent.exe --transport serial` | 绕过 hub 直连串口 |

作业也可以直接把参数给 Python 侧的 `connector.run_job(dict)`，返回同样的字典。

### 3.1 用 MCP 接（AI 直接把板子当工具调）

`python tools/mcp_server.py` 是一个 **stdio MCP 服务器**（每行一条 JSON-RPC，日志走 stderr，不污染协议）。
客户端配置仓库里都带了：`.mcp.json`（Claude Code）、`.cursor/mcp.json`（Cursor）、`.vscode/mcp.json`（VS Code / Copilot）。
Claude Code 也可以一行加上：

```bash
claude mcp add esp32p4-hid-board -- python tools/mcp_server.py
```

十个工具，名字固定：

| 工具 | 参数 | 等于 |
|---|---|---|
| `board_caps` | 无 | `--caps` |
| `board_services` | 无 | `--services` |
| `board_manual` | 无 | `--manual`（就是这份） |
| `board_run_job` | `job`（对象或字符串，可带 `transport`: auto/tcp/serial） | 任意作业 |
| `board_ocr` | `rect`（物理像素 `[x1,y1,x2,y2]`）、`as` | `ocr` |
| `board_point` | `x`、`y` | 移指针 |
| `board_click` | `x`/`y`，或 `text`（按文字找），`button` | `click` / `find` + `click` |
| `board_type` | `text` | `type` |
| `board_key` | `combo`，如 `ctrl+s` | `key` |
| `board_see` | `question` | `see`（只回答是/否/"这是什么"；它报的坐标别信） |

返回统一是 `{"content":[{"type":"text","text":"<JSON 字符串>"}],"isError":false}`；
出错时 `isError:true`，text 里是中文说明 + 原始 JSON。
**注意**：MCP 底下跑的还是同一套 connector，所以第 1 节那七条硬规矩一条都不少——
工具描述里也写了，别因为"是工具调用"就放松。

---

## 4. 作业格式

```json
{"steps": [
  {"op": "restore", "exe": "cloudmusic"},
  {"op": "find", "text": "下一首", "rect": [0,0,2560,1440], "as": "next"},
  {"op": "click", "on": "next"},
  {"op": "title", "exe": "cloudmusic", "require": "Daylight"}
]}
```

- 顶层可以是 `{"steps": [...]}` 或直接 `[...]`。
- 每一步都能带 **`require`**：把这步结果 JSON 转成字符串后必须包含这个子串，否则整单失败。
- **`as=名字`** 把结果存进变量；后面用 **`on=名字`** 引用（`click` / `move` / `point` 都认），
  `find` 存的是带 `cx/cy` 的框，所以 `on=` 会自动点在中心。
- 返回值：

```json
{"ok": true,  "steps": [{"i":0,"op":"ocr","ok":true,"rows":["..."],"boxes":[{"text":"...","x":0,"y":0,"w":120,"h":22,"cx":60,"cy":11}],"ms":120}], "vars": {}}
{"ok": false, "steps": [{"i":2,"op":"click","ok":false,"err":"RuntimeError: …"}],
 "err": "第 2 步 click 失败：…"}
```

---

## 5. 26 个操作

| 操作 | 参数 | 说明 |
|---|---|---|
| `restore` | `title=` 或 `exe=`，`wait=` | 从最小化/托盘还原（不移动、不改大小） |
| `focus` | `title=` 或 `exe=`，`wait=` | 还原并置前 |
| `title` | `title=` 或 `exe=`（可省） | 报窗口标题，Win32 为准 |
| `ui` | `title=`/`exe=`，`buttons=true`，`as=` | 整个窗口读成结构：文字行 + 可疑按钮，都带屏幕坐标。PrintWindow 渲染，被挡住也能读 |
| `key` | `combo=ctrl+t` | 组合键 |
| `type` | `text=` | 敲字（只支持能直接映射的键） |
| `click` | `x=` `y=` 或 `on=`，`button=left\|right` | 点击 |
| `move` | 同上 | 移动光标（不点） |
| `point` | 同上，`tol=2` | **闭环定位**：移动后自己纠偏到目标像素，比 `move` 准 |
| `drag` | `x1=` `y1=` `x2=` `y2=`，`button=` | 拖 |
| `drag_path` | `points=[[x,y],…]`，`button=` | 沿路径拖（画笔） |
| `hold` | `keys=["ctrl","shift"]` | 按住不放 |
| `scroll` | `amount=-3` | 滚轮（负=向下） |
| `wait` | `ms=500` | 等 |
| `ocr` | `rect=[x0,y0,x1,y1]`，`as=`，`fresh=true` | `rows`（逐行文字）+ `boxes`（每行的 `x/y/w/h` 和中心 `cx/cy`，可直接拿去点） |
| `find` | `text=`，`rect=`，`thresh=0.7`，`as=` | 在区域里找一行字，返回框和中心点 |
| `look` | `path=`，`region=`，`as=` | 截图存变量（配合 `diff`） |
| `read_text` | `select_all=true`，`restore_clip=true` | 全选复制取文字（不改剪贴板） |
| `diff` | `a=` `b=`，`threshold=0` | 比较两张 `look` 的图，返回差异比例 |
| `wait_change` | `timeout_ms=3000`，`threshold=0` | 等画面变化，等不到就报错 |
| `see` | `q=`，`rect=`，`speed=fast\|normal\|fine`，`longest=`，`max_new_tokens=` | 问视觉模型一句话（默认 20 字内） |
| `check` | `q=`，`rect=`，`speed=fast`，`max_new_tokens=8` | **是非题**，结果在 `yes`，比开放问答快 |
| `vision` | `action=status\|start\|stop\|unload` | 视觉服务；`unload` 立刻还显存 |
| `clip` | `set=`（写）或不给（读） | 剪贴板 |
| `launch` | `cmd=` | 启动程序（不经过板子） |
| `recipe` | `name=wechat-send` / `open-url` … | 带完整核对的成品动作 |

**找窗口**：`title=` 是标题子串，`exe=` 是进程名。播放器/编辑器的标题就是当前内容
（网易云的标题是歌名），所以按应用名永远找不到——用 `exe=cloudmusic` 这种。

### 两个 recipe

```json
{"op":"recipe","name":"wechat-send","to":"文件传输助手","pieces":["py:nihao"]}
{"op":"recipe","name":"open-url","url":"https://github.com","expect":"github"}
```

- `wechat-send`：核对聊天标题 → 清空输入框 → 拼音选字/直接敲 → 发前核对 → 回车 → 发后核对气泡。
  `pieces` 里 `py:` 是拼音（要选字），`raw:` 是直接敲的 ASCII。
- `open-url`：交给系统 Win+R 打开（不走地址栏，地址栏的自动补全会把回车吃掉），最后核对标题。

### 画图类应用：坐标校准与「画不出线」诊断

画图窗口里有**两套坐标**，混了就怎么画都不对：

| | 是什么 | 从哪来 |
|---|---|---|
| 物理像素 | 板子和 `click` / `point` / `ocr` 用的坐标（全屏 2560×1440，见第 2 节） | 你写的 `x`,`y`；`point` 返回的 `final` |
| 逻辑像素 | Win11 画图**状态栏**报的位置，随 Windows 显示缩放变小 | `ocr` 读状态栏 |

换算（缩放 = Windows 显示缩放，180% 就是 1.8）：

```
屏幕x = 画布左上物理x + 状态栏x × 缩放
屏幕y = 画布左上物理y + 状态栏y × 缩放
```

**两锚点标定法**（最稳，不猜缩放）：

1. `point` 到画布上两个相距较远的点；
2. 每次读两样东西：`point` 返回的 `final`（物理像素）+ `ocr` 读状态栏（逻辑像素）；
3. 联立两对，解出缩放和原点。

真实一组（2026-10 实测，2560×1440 @180%，画图窗口）：

| 光标物理（`point.final`） | 状态栏逻辑（`ocr`） |
|---|---|
| (550, 761) | (64, 176) |
| (730, 781) | (164, 187) |

→ 缩放 = (730−550) ÷ (164−64) = **1.8**；
→ 原点 x = 550 − 64×1.8 ≈ **435**，原点 y = 761 − 176×1.8 ≈ **445**，即**画布左上 ≈ (435, 445)**。

**「画不出线」三步诊断**（按顺序做，别跳）：

| 步 | 做什么 | 该看到什么 |
|---|---|---|
| 1 | `point` 到你**以为的**画布左上角，`ocr` 读状态栏 | 状态栏接近 (0,0)；差几十以上 = 映射错了，回去重标定 |
| 2 | `drag_path` 画一个已知矩形：`[[左,上],[右,上],[右,下],[左,下],[左,上]]` | 屏幕上出现这个矩形 = 映射对；出现了再用真图形开工 |
| 3 | 两点 `drag` 只出一个点 | 按下那一下落在**画布外**（标题栏 / 工具栏 / 窗口外），不是 `drag` 坏了——把起点往画布内收 20~40 px |

第 2 步可以直接抄（按上面那组标定：画布左上 435,445，内缩 20 px）：

```json
{"steps":[{"op":"drag_path","points":[[455,465],[800,465],[800,700],[455,700],[455,465]]}]}
```

**`drag_path` 的真实脾气**（照代码写的行为，别按想象调参）：

- 只认 `points` 和 `button`；**没有「点间 wait」这种参数**，塞 `wait` 不会变慢也不会更稳；
- 内部逐点**闭环定位**：容差约 **1 px**、每步 settle **10 ms**。两点之间花的时间是「光标收敛到那 1 px」的时间，不是等出来的；
- 所以：点密 = 拐角圆顺但慢，点稀 = 相邻两点连直线。按形状需要出点，别为省时间硬减；
- **断线**（本该连续却缺一截）多半是串口 / hub 抖动（`10053` / `10054`），**不是点太密**——别为了「稳」把点调稀；
- 抬笔在 `finally` 里：中途任何失败都会松开左键，不会把笔留在按下状态。

**清空画布**（画之前做，顺序照这个来）：

| 做法 | 结果 |
|---|---|
| `Ctrl+A` → `Delete` | 清空选区。新版画图（带图层 / Copilot）清成**透明棋盘格**，旧版清成白底——**别猜，清完用 `look` 看一眼再往下走** |
| 要白底 | 若看到棋盘格：先把前景色设成白色 + 选填充工具点一下（或直接涂白）再画 |
| `Ctrl+N` | **别用**：可能弹「保存更改」对话框，作业就卡在那里 |
| 开工前 `Ctrl+S` | 先存上一版，画坏了还有退路 |

**换电脑 / 换缩放之后**：指针档案是**按机器**存的
（`C:\ProgramData\dsh\pointer-profile.json`，按 分辨率 | 虚拟桌面 | DPI 模式 分 key）。换了机器或改了显示缩放：

1. 重标：`python tools/home-calibrate.py --transport tcp --apply`（四角撞边归零，约 8 s）；
2. 自检：`--selfcheck` 看「指针校准」这一项是不是 `ok`——它是软项，缺了不会把整单判红，但 `point` 会不准；
3. 上面那组缩放 / 原点**要按新机器重算**——`point` 的闭环能兜住一两像素的偏差，兜不住"逻辑像素 × 错缩放"这种系统性错位。

---

## 6. 四个能直接抄的作业

**① 按文字点按钮**

```json
{"steps":[{"op":"focus","exe":"cloudmusic"},
          {"op":"find","text":"下一首","rect":[0,700,2560,1440],"as":"b"},
          {"op":"click","on":"b"},
          {"op":"title","exe":"cloudmusic"}]}
```

**② 陌生软件里找图标按钮**（图标没文字，OCR 看不见，`ui` 用亮度块找）

```json
{"steps":[{"op":"ui","exe":"cloudmusic","as":"ui"},
          {"op":"click","x":313,"y":1022}]}
```

`ui` 返回的按钮是带 `cx/cy` 的方框，抄一个用；也可以先 `see` 问"哪个是下一首"再点。

**③ 发一条微信**

```json
{"steps":[{"op":"recipe","name":"wechat-send","to":"文件传输助手","pieces":["py:nihao"]}]}
```

**④ 读屏幕并核对**（最基础的眼睛）

```json
{"steps":[{"op":"look","path":"before.png"},
          {"op":"click","x":1200,"y":600},
          {"op":"wait","ms":600},
          {"op":"look","path":"after.png"},
          {"op":"diff","a":"before","b":"after","threshold":2},
          {"op":"ocr","rect":[0,0,1200,300]}]}
```

---

## 7. 出错怎么办

| 看到 | 原因 | 对策 |
|---|---|---|
| `PortDisconnected` / `10054` / `10053` | 板子连接被掐断 | 重跑整条命令即可；只读作业会自动重试一次，有输入动作的绝不重试 |
| `第 N 步 X 失败：RuntimeError: 区域里没找到 '…'` | 文字不在这个区域 / OCR 没读准 | 放大 `rect`、或改用 `ui` 拿按钮、或换成"模糊"的关键词 |
| `OCR 不可用` / `No module named rapidocr` | 视觉服务没起，且本地没有 OCR 引擎 | 起 `visiond`：装了依赖的机器上 `python tools/visiond.py`；或 `--services` 看 `vision.up` |
| `前台是全屏应用…不发送键鼠` | 有全屏窗口 | 先退出全屏，或确认后用 `"force": true` |
| `没找到窗口（title=… exe=…）` | 标题猜错 | 用进程名 `exe=`，先 `title` 探一下 |
| `require 不满足` | 这步结果没有预期的文字 | 说明前面那步没生效；**停**，不要硬往下走 |
| `找不到说明书文件` | 只拷了 EXE 以外的东西 | 用官方发布的那个文件；说明书内嵌在里面 |

---

## 8. 写策略要用的耗时（2026-10 实测）

| 动作 | 耗时 |
|---|---|
| 一次调用额外开销（进程 + 连板子） | 0.12 ~ 0.20 s |
| 敲一个字 | 约 68 ms |
| 移动一次光标 | 约 26 ms |
| `ocr` 小块（约 5 行） | 0.12 s（服务内）／0.62 s（含进程启动） |
| `ocr` 大块（约 34 行） | 1.4 s |
| `ui` 热路径 | 0.13 ~ 0.7 s（冷启动 6.6 s，只发生一次） |
| 首次 `see`（装模型） | 另加约 11 s |
| OCR 占显存 | 约 1157 MiB（纯 CPU 跑 OCR 时为 0） |

**所以**：一次作业里多放几步，比来回起很多次进程划算；但别把整段流程塞成一条——
中间要决策的地方（看到什么再决定点哪）必须断开，否则就是在盲操作。

---

## 9. 只能聊天的 AI（豆包 / Kimi / 通义 / 网页版）怎么干活

你读得到这份说明书，但**跑不了命令**。没关系——按这个格式回话就行，人会把你的话粘进窗口版
（`esp32p4-agent-ui.exe` 右下角那个「用板子运行作业」框），板子就会照做。

**你每次只回两种东西之一：**

① **一条作业**（要它动手时）。整个回复只放一个 ```json 代码块，别的解释放在代码块**上面**两行以内。
第一条作业永远用只读的（`ocr` / `ui` / `find`）先看清楚，别一上来就点：

```json
{"steps": [{"op": "ocr", "rect": [0, 0, 1200, 400]}]}
```

② **一次提问**（信息不够时）。比如："记事本现在开着吗？把窗口标题告诉我。"——不要靠猜往下走。

**铁规矩（同第 1 节，聊天场景下更容易踩）：**

- **坐标不能猜**。你不知道鼠标在哪、屏幕是什么样——先要一条只读作业（`ocr` / `ui` / `find`），
  让人把结果贴回来，再用结果里的 `cx`/`cy` 写下一步。宁可多一轮，不要瞎点。
- **一次一条作业，别自作聪明串十条**。每条作业 1～3 步，做完让人贴结果，你看完再给下一条。
- **不可逆的（删除、发送、支付、关机）先问人**，别直接写进作业。
- **不许编造结果**：人没贴回来就说"我还没看到结果"，不要假装已经执行成功。
- **禁止改固件**：你的作业里不该出现 `firmware/` 里的任何东西。

**人怎么用你的作业**：复制那段 ```json 整段 → 粘进窗口版的作业框 → 点「用板子运行」→
窗口会回一句人话（跑完了 / 没跑成 + 原因），人把这句话或截图给你，你再决定下一步。

---

## 10. 交接须知

- 你**不是**这个仓库的主人。提交、推送、发布、改 README 之前先问人。
- **不许**把桌面截图、日志、剪贴板内容贴到公开地方（聊天、gist、issue）。
- 要改代码，先读 [docs/operations.md](operations.md) 的规矩，再读 [docs/field-guide.md](field-guide.md)。
- 这套东西的价值在**核对**：每一步都要能说清"我怎么知道它成功了"。做不到就别动。

---

## 11. English quick reference

**What this is.** A single executable controller for an ESP32-P4 board that acts as a real USB
HID keyboard + mouse on this Windows machine. You (the AI) see via screenshots/OCR; the board
types and clicks. No driver, no injection, no cooperation from target apps.

**Hard rules.** (1) **Never modify the firmware** — `firmware/` is read-only: no build, no flash,
no source edits. (2) Never maximize/move/resize user windows; `restore`/`focus` only. (3) Do not
send input while a fullscreen app is foreground, unless the step says `"force": true`.
(4) Ask the human before anything irreversible (delete, send, pay, shut down). (5) Look before you
act, verify after; on mismatch stop — never retry an input action. (6) Coordinates come from
`ocr`/`find`/`ui` only, never from the vision model. (7) One serial client at a time.

**Call it.**

```bash
esp32p4-agent.exe --manual      # this file
esp32p4-agent.exe --caps        # machine-readable capability list
esp32p4-agent.exe --services    # hub / vision / board status
esp32p4-agent.exe --selfcheck   # five checks
echo '{"steps":[{"op":"ocr","rect":[0,0,600,200]}]}' | esp32p4-agent.exe
```

Exit codes: `0` ok, `1` job failed (see `err`), `2` bad input. Output is always JSON.
Coordinates are physical pixels on a 2560×1440 screen.
