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
{"ok": true,  "steps": [{"i":0,"op":"ocr","ok":true,"rows":["..."],"ms":120}], "vars": {}}
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
| `ocr` | `rect=[x0,y0,x1,y1]`，`as=`，`fresh=true` | 逐行文字 + 坐标 |
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

## 9. 交接须知

- 你**不是**这个仓库的主人。提交、推送、发布、改 README 之前先问人。
- **不许**把桌面截图、日志、剪贴板内容贴到公开地方（聊天、gist、issue）。
- 要改代码，先读 [docs/operations.md](operations.md) 的规矩，再读 [docs/field-guide.md](field-guide.md)。
- 这套东西的价值在**核对**：每一步都要能说清"我怎么知道它成功了"。做不到就别动。

---

## 10. English quick reference

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
