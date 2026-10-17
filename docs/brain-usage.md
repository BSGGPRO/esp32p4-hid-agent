# 大脑层：让模型真的操作这台电脑

> 三层结构：**眼**（截屏 + 结构化观测）→ **脑**（模型决策）→ **手**（板子 USB HID）。
> 本文件说的"大脑层"是第三个模块 `pc/brain.py`：它把"眼"和"手"封装成**可编排的原子动作**，
> 让模型一次调用就能完成"看→想→做→验"的闭环。

---

## 1. 为什么还要一层

板子只提供 15 条协议命令（`key`/`mouse_move`/`text`…）。直接拿这些命令写策略有三个坑：

| 坑 | 大脑层的解法 |
|---|---|
| HID 鼠标只有**相对**移动，没有"跳到 (x,y)" | `point` 用 `GetCursorPos()` 做反馈，闭环收敛到绝对坐标（见 §3） |
| 每条命令开一次串口，1.5 s/条 | `run --script` 一次连接执行整组动作 |
| `ctrl`/`alt`/`shift`/`win` 不是协议键名（规范名 `leftctrl`…） | 内部 `norm_combo()` 归一，上层只写 `ctrl+s` |
| 命令失败就静默，模型不知道 | 每步返回 `{ok, ...}`，失败即中止并可回读证据 |

**底线**：所有输入位移/按键**必须**由板子发出。大脑层只读 Win32 状态做观测，绝不调 `SetCursorPos`/`SendInput` 抄近路。

---

## 2. 动作清单

CLI 子命令与 `run --script` 的 `op` 一一对应。

| op / 子命令 | 参数 | 说明 |
|---|---|---|
| `look` | `--region x,y,w,h` | 截屏 + 返回尺寸/光标/前台窗口/可见窗口列表 |
| `point` | `x y [--tol]` | **绝对坐标**，闭环收敛（默认 tol=2；实测 4~6 次迭代） |
| `locate` | `x y [--tol 1]` | 更严的闭环定位：**1 px 容差**，每步读光标做抗打断判定（绘图用） |
| `drag-path` | `x1,y1;x2,y2;...` | 一次按下 → 逐点闭环移动 → 抬起，画连续折线/弧线（不是"两点一线"） |
| `move` | `dx dy` | 相对移动 |
| `click` | `[x y] [--button] [--count]` | 给坐标则先 `point`；支持 right/middle、双击 |
| `drag` | `x1 y1 x2 y2` | 定位→按下→定位→抬起 |
| `scroll` | `amount` | 正=向上（协议 wheel 正值） |
| `type` | `text [--cps]` | 走协议 `text`，仅 ASCII 可打印 |
| `key` | `combo` | 组合键，`ctrl+s` / `win+r` / `alt+f4` |
| `tap` | `key` | 单键；含 `+` 自动当组合键 |
| `hold` / `release` | `keys` | 按住 / 全部释放 |
| `read_text` | `--no-select-all` | **文本读屏**：ctrl+a → ctrl+c → 读剪贴板，读完还原用户剪贴板 |
| `wins` | — | 可见窗口（标题/矩形/pid/前台/最小化） |
| `focus` | `title` | 按标题子串切前台（含最小化恢复、前台锁解锁） |
| `launch` | `argv...` | 启动本机进程（**不走 HID**，用于准备靶子） |
| `clip` | `--set TEXT` | 读/写剪贴板 |
| `diff` | `a b` | 两张截图差分 |
| `wait_change` | `timeout_ms` | 等屏幕变化（轮询差分） |
| `sleep` | `ms` | 等待 |
| `info` | — | 板子状态 + 屏幕/光标 |
| `run` | `--script plan.json [--keep-going]` | **一次连接跑整组动作** |

`run` 的 JSON 就是一串动作：

```json
[
  {"op": "focus", "title": "Notepad"},
  {"op": "click", "x": 1200, "y": 400},
  {"op": "type",  "text": "HidBrain AbC 2026!", "cps": 20},
  {"op": "look",  "path": "after.png"},
  {"op": "diff",  "a": "before.png", "b": "after.png"}
]
```

---

## 3. 绝对坐标是怎么做到的

HID boot mouse 只能"相对移动 N 像素"。所以 `locate(x, y)` 是一个**闭环**：

```
loop:
    读 GetCursorPos() -> (cx, cy)
    误差 = (x-cx, y-cy)
    若 |误差| <= tol（locate 默认 1）: 完成
    发一次 mouse_move(step(误差))
step(e):  |e| <= 3  -> e                       # 1:1 精修区（此区间增益≈1）
          |e| >  3  -> clamp(e/gain(|e|), ±200)  # 加速区：用实测增益反推，防过冲
```

Windows「提高指针精确度」会按速度非线性放大位移，`gain(m)` 来自
`tools/calibrate-pointer.py` 的实测增益表（`brain._GAIN_TABLE`）：

| 发 | 1 | 5 | 12 | 20 | 40 | 80 | 120 | 200 |
|---|---|---|---|---|---|---|---|---|
| 实测走 | 1 | 4 | 15 | 40 | 107 | 241 | 376 | 618 |
| 增益 | 1.0 | 0.8~0.9 | 1.28 | 2.00 | 2.67 | 3.02 | 3.13 | 3.09 |

> 小位移（1~8 px）落在加速曲线拐点上，会有"发 6 走 8/19/111"这类量化噪声，
> 所以小误差走 1:1 精修，不套增益。

### 3.1 精度复测结论（2026-10-17 晚，真板子）

`tools/calibrate-pointer.py` 重写为两段测量，证据 `docs/evidence/calib-20261017-213345.txt`：

| 指标 | 结果 |
|---|---|
| 闭环落点（10 个随机目标，tol=1） | **全部命中，平均/最大误差 = 1.00 / 1 px，P90 = 1 px** |
| `<=1 px` 命中率 | **100%** (10/10) |
| 平均迭代 | 4.4 次（最多 6） |
| 重复性（同一目标连续 5 次，每次从别处出发） | \|err\| = [0,1,1,1,1] |
| 开环增益 | 与内置表吻合（40→107、120→375.7、200→618） |

**结论：闭环精度已到单像素级，不需要改 Windows 指针设置（用户明确选择"只做闭环校准"）。**
继续加精度只能靠绝对定位 HID（boot mouse 只有相对位移），收益不大。

### 3.2 抗打断：用户动了真鼠标怎么办

模型在画图/操作时，用户可能随时抢鼠标。`PointerGuard` 每一步比较：

```
我们发的 (sx, sy) --增益表--> 期望位移 exp
光标实际位移 delta = after - before
偏差 = |delta - exp|
判定介入:  偏差 > max(15 px, 30% * |exp|)   或   delta 方向与 exp 相反且 |delta| > 15
```

- 判定介入 → **不硬拽**：抬笔、把当前真实光标位置当**新锚点**，从下一个规划点续画
  （宁可留个小缺口，也不把用户的乱动画成长线），事件记进返回值和证据。
- **连续 3 次介入** → 认定用户在抢鼠标，暂停并返回 `ok:false, reason=user interference`。
- 阈值可配：`PointerGuard(tol_px=15, rel_tol=0.30, max_consecutive=3)`。

### 3.3 传输选择：直连串口 / 走本地 hub

板子只有一条 UART 命令通道（COM7），多工具同时用会互相踢掉。两种接法：

```powershell
# A. 直连串口（独占 COM7，必须先持板子锁 tools/board_lock.py）
& $PY pc\brain.py --transport uart --port COM7 look

# B. 走本地 hub（hub 独占 COM7，监听 127.0.0.1:8766 做字节级透传，不抢串口）
python pc\hub.py                                     # hub 端（web-ui 维护）
& $PY pc\brain.py --transport tcp --host 127.0.0.1 --tcp-port 8766 look
```

- `--transport tcp` 连不上时**先打印原因，再自动回退 UART**；`--no-fallback` 则直接报错。
- `pc/transport_tcp.py` 与 `UartTransport` 同接口（`open/write/readline/close/drain/probe`），
  帧格式逐字一致，协议层零改动。
- 注意 `--port` 始终指**串口**；hub 端口用 `--tcp-port`（默认 8766）。

### 3.4 卡死保护

连续 4 次光标不动（撞屏幕边缘/被应用捕获）就返回 `ok:false` 并给出残差。

---

## 4. 文本模型怎么"看"

`look` 产出的是 PNG。**如果模型没有图像输入能力，这张图就用不上** —— 这不是小事，
要如实说明，并给出替代通道：

| 通道 | 依赖 | 能拿到什么 |
|---|---|---|
| `look` | 视觉模型 | 像素级一切 |
| `wins` | 无需视觉 | 窗口标题、位置、前台窗口（**标题本身就含大量语义**） |
| `read_text` | 无需视觉 | 前台控件里的**文字**（ctrl+a/ctrl+c 走剪贴板） |
| `diff` / `wait_change` | 无需视觉 | "屏幕变了没有" + 变化区域包围盒 |
| `clip` | 无需视觉 | 剪贴板内容 |

`wins` 的威力常被低估：窗口标题就能回答"现在前台是什么程序/打开了哪个文件/有没有弹对话框"。

---

## 5. 真机演示记录（2026-10-17，本机 2560×1440）

执行者：Lead（模型本身）。全部动作用板子发出，坐标/状态用 Win32 读回。

| # | 动作 | 结果 | 证据 |
|---|---|---|---|
| 1 | `point 600 400` | ✅ 2 次迭代到 (600,399) | `GetCursorPos` 回读 |
| 2 | `point 2000 1000` | ✅ 5 次迭代到 (2000,1000)，误差 0 | 同上 |
| 3 | `key win+r` | ✅ 弹出「运行」 | `wins` 出现 `运行` 且 `foreground=true` |
| 4 | `type "notepad"` + `tap enter` | ✅ 记事本被启动 | `wins` 出现 Notepad |
| 5 | `click 270 1105` | ✅ 收敛到 (268,1105) 并点击 | ack + 光标回读 |
| 6 | `type` 进去的字 + `read_text` | ✅ 剪贴板读回 | 读回 `esp32p4-hid-brain-ok-20261017`（重复两遍，因为该步骤跑了两遍） |
| 7 | `launch notepad <tmp>` + `click` + `type` | ✅ 文档被修改 | 标题变成 `* *hidbrain.txt - Notepad` |
| 8 | `hold leftshift` → `tap a` → OS 探针 | ✅ shift 保持正确 | `GetAsyncKeyState(SHIFT)` 期间恒为 True |
| 9 | `release_all` | ✅ 全部抬起 | `GetAsyncKeyState` 归零 |
| 10 | `tools/draw-rage.py`（12 笔 / 21 次真实拖拽） | ✅ 画图里画出暴走漫画脸（Y U NO 风格） | 画布内新增 **14586 px**、墨迹 bbox `[1692,443]-[2091,864]`、21.6 s、用户介入 0 次 → PASS，见 §7 |

### 演示中撞到的真问题（都已定位，部分未修）

| 问题 | 现象 | 结论 |
|---|---|---|
| **Win+R 自动补全** | 打 `notepad` 回车，却打开了 `README.md`（补成上次的 `notepad ...README.md`） | ⚠️ 真实风险：**盲打前必须 `wins` 验证**。规避：`ctrl+a` 后打字再 `Delete` 清掉补全尾巴 |
| **Win11 记事本恢复标签页** | `launch notepad.exe` 把上次的 README.md 又打开了 | ⚠️ 必须显式传文件路径，别裸启动 |
| **运行框大小写异常** | `type "AbC"` 读回 `aBc`（shift 晚一格） | 🔍 OS 探针证明固件修饰键保持**正确** → 判定为运行框自动补全组合框的假象，**不是固件缺陷**；但需在干净目标里复测 |
| **`ctrl+a` 在 Win11 记事本不选中** | `read_text` 只读到尾部 6 字符 `' 2026!'`，在「运行」框里却正常 | ⚠️ **按应用而异**，`read_text` 还不可靠；后续应改用 UI Automation 或按应用定制 |
| **64 位指针截断** | `clip_get()` 崩 `access violation` | ✅ 已修：`GlobalLock/GetClipboardData` 必须声明 `restype=c_void_p` |
| **DPI 缩放** | 不建连接时 `wins` 的 rect 比实际小 1.25 倍（点击会打偏） | ✅ 已修：模块级 `enable_dpi_awareness()` |
| **`launch` 参数冲突** | `--json` 报 `unhandled cmd` | ✅ 已修：子命令 `dest` 与位置参数 `cmd` 撞名，改名 `argv` |
| **`--json` 位置** | 放在子命令后面报 `unrecognized arguments` | 用 `brain.py --json --port COM7 run ...`（全局参数在子命令前） |

---

## 6. 安全须知

- 进程退出时（含异常）**无条件 `release_all`**，不会留按住不放的键。
- 固件另有 `safety_timeout_ms=1500` 兜底：停发报文 1.5 s 自动全抬。
- `run` 默认**某步失败即中止**（`--keep-going` 才继续），避免在错误状态上继续盲操作。
- **动用户文件之前先 `wins` 确认前台窗口标题**。演示里就靠这一步拦住了往 README.md 灌字。
- **操作有副作用的窗口（画图/编辑器）前，必须复核 `GetForegroundWindow()` 的 hwnd 就是要操作的那个窗口**；
  绘图这类工具每一步动笔前都复核一次，前台不是画图就立即停手（见 §7）。

---

## 7. 用板子画图：`tools/draw-rage.py`

一条命令：截屏 → 找画图窗口 → 像素分析定位白色画布 → 生成暴走漫画脸 → HID 逐笔拖拽
→ 每笔后截屏差分自证 → 存证。**模型没有图像输入能力，全程不靠"看"，只靠像素统计。**

### 7.1 窗口规则（用户明确要求，必须遵守）

| 规则 | 实现 |
|---|---|
| **绝不放大/最大化画图窗口** | 每次运行只 `GetWindowRect` 读当前 rect；代码里没有 SetWindowPos 改大小、没有 maximize（只有 `--detect` 的安全展示） |
| 支持用户随时手动挪窗口 | 每笔前重新读 rect；发现 rect 变了 → 立即中止，避免把线画到错误位置 |
| 不挡聊天窗口 | 与标题含 `DeepSeek Harness` 的窗口算重叠面积；**超过聊天窗口 25%** 才做一次**等尺寸最小移动**（挑四角中重叠最小的落点），并在日志/报告里写清 before/after；挪了也不会更好就不动 |
| 只画在画图里 | 每笔前 `GetForegroundWindow()==paint.hwnd` 才动笔；否则先尝试聚焦，仍不行就 `return 3` 停手 |

### 7.2 画布定位（不用眼睛）

1. 在画图窗口 rect 内按 8 px 网格找**纯白（RGB≥245）**像素，4 邻域连通块取最大的一块；
2. 用"白占比≥85%"的行/列把 bbox 细化到像素级；
3. 要求白占比 ≥ 0.90，否则说明几何算错/被遮挡 → `return 2`，**不画**；
4. 画前统计已有墨迹（非白像素）与 bbox，画后同法复算 —— 于是"新增像素"是可复算的。

### 7.3 图案：Y U NO 风格线条脸

纯折线/弧线，不依赖字体：圆头（56 点椭圆）+ 两只怒眼（椭圆+瞳孔）+ 倒竖粗眉 +
鼻子 + 大张嘴（上下唇 + 3 条牙齿线）= 12 笔。归一化坐标按画布**等比缩放、留 5% 边距**。
线宽：先画一笔探针**实测笔宽**，再按目标线宽（细 3 px / 中 5 px / 粗 7 px）做 1~2 条平行
偏移笔画加粗；探针用 `ctrl+z` 撤掉（残留墨迹 < 10 px 不碍事）。

### 7.4 用法

```powershell
python tools\draw-rage.py --detect             # 只检测窗口/画布，不动笔（安全）
python tools\draw-rage.py --mock               # 离屏渲染同一套几何，验证工具本身
python tools\draw-rage.py --transport uart --port COM7   # 真机（需板子锁，脚本自动取/放）
python tools\draw-rage.py --transport tcp      # hub 在跑时走 127.0.0.1:8766，不抢串口
python tools\draw-rage.py --plan               # 只打印每笔坐标
```

退出码：0 成功 / 1 找不到画图 / 2 画布不合法 / 3 前台不是画图（安全中止）/
4 没留下墨迹（默认工具不是画笔）/ 5 被用户打断暂停 / 6 形状验证不通过。

### 7.5 实测证据（2026-10-17 21:30，真板子 COM7）

| 项 | 值 |
|---|---|
| 窗口 | `无标题 - 画图` rect `[1408,102,2378,1038]`（970×936，**未最大化**）；与聊天窗口重叠 0.7% → 未移动 |
| 画布 | `[1627,389]-[2157,919]`，530×530，白占比 1.000，画前墨迹 0 |
| 作画 | 12 笔 / 21 次拖拽 / **21.6 s** / 用户介入 **0** 次 |
| 结果 | 画布内新增 **14586 px**，墨迹 **13808 px**，bbox `[1692,443]-[2091,864]`（完整落在画布内） |
| 几何自证 | 头 1.00 / 双眼 1.00 / 眉 1.00 / 鼻 1.00 / 嘴 1.00（沿设计曲线采样 ±7 px 内有墨） |
| 安全自证 | 画布外差分 1704 px 逐连通块核对 → 全是聊天窗口 UI/动画变化，无一处是笔迹 |
| 证据 | `docs/evidence/rage-before.png`、`rage-after.png`、`rage-ascii.txt`（墨迹字符图）、`rage-report.txt`（JSON） |

**诚实边界**：`--mock` 的 PASS 只证明几何/分析代码自洽（离屏 Pillow 渲染），不代表真机；
真机结果以上表为准。模型看不到图，所以"像不像"由几何采样 + ASCII 墨迹图 + 像素差分三者交叉证明，
读者可用同一份 PNG 复算。
