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
| `point` | `x y [--tol]` | **绝对坐标**，闭环收敛（实测 2~5 次迭代命中 ±2 px） |
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

HID boot mouse 只能"相对移动 N 像素"。所以 `point(x, y)` 是一个**闭环**：

```
loop:
    读 GetCursorPos() -> (cx, cy)
    误差 = (x-cx, y-cy)
    若 |误差| <= 2: 完成
    发一次 mouse_move(step(误差))
step(e):  |e| <= 4  -> e                  # 1:1 精修区
          |e| >  4  -> clamp(e/3, ±120)  # 加速区，防过冲
```

为什么要除以 3：Windows「提高指针精确度」会按速度放大位移（实测发 120 走 233、发 40 走 107、发 5×20 走 204）。
大误差时按 1/3 走，放大后正好吃掉大部分误差；进入 ±4 的 1:1 区后逐像素精修。

实测：`(822,820) → (600,400)` 用 **2 次迭代**；`(600,399) → (2000,1000)` 用 **5 次**，误差 (0,0)。
卡死保护：连续 4 次光标不动（撞屏幕边缘/被应用捕获）就返回 `ok:false` 并给出残差。

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
