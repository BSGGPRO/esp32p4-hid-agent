# 验收报告：hub 断连修复 + 测试 + 画图文档（task-19）

- **验收人**：code-reviewer（老板角色，独立审查；未改任何交付代码）
- **日期**：2026-10-18 20:05（Asia/Shanghai）
- **仓库**：`C:\Users\lu'hao\Documents\deepseek-harness\default-workspace\esp32p4-hid-agent`
- **基线**：`git HEAD d5266b1`；本轮工作树：`M pc/hub.py`、`M docs/ai-controller.md`、`?? tests/hub_serial.py`
- **快照**：
  - `pc/hub.py` 70,049 B，sha256 `40ca5d3f…9c5880`（2026-10-18 19:52）
  - `tests/hub_serial.py` 11,888 B，sha256 `88b5f052…6048c03`（19:53）
  - `docs/ai-controller.md` 20,521 B，sha256 `df1dcff7…0323f3`（19:52）
  - 参照实现 `pc/brain.py` 42,501 B（09:10，未改）

**一句话结论：通过（P0=0）。** 核心修复——WinError 分类、permanent 立即关闭、瞬时错有界重试、`--log` 默认不改行为、测试不碰真串口、文档公式与 `drag_path` 行为——全部对上；唯一 P1 是文档把 Paint 清空结果写成“透明棋盘格”，未实测且与常见行为不符，建议改；其余为 P2。

---

## 1. 验收范围与方法

范围：

1. `pc/hub.py`：`SerialError.code/permanent`、`SerialLink.read()` 分类、`_reader_loop` 有界重试、`main --log`
2. `tests/hub_serial.py`：27 项假串口测试，是否覆盖四条契约、是否真不碰真串口/真 hub
3. `docs/ai-controller.md` 新增的“画图类应用：坐标校准与「画不出线」诊断”（+70 行）

方法：**先读代码再跑**。除任务要求的三条命令外，另写了一个不落仓库的独立探针（`/tmp/hub_review_probe2.py`），直接替换 `ReadFile` 与假串口，验证分类/时延/默认日志；逐行对照 `pc/brain.py` 的 `drag_path`；对文档数字手算复核；未启动真 hub、未碰真板子、未拔插串口。

---

## 2. 实跑结果（命令 + 关键输出 + 退出码）

| 命令 | 结果 | 退出码 |
|---|---|---|
| `python tests/hub_serial.py` | **27/27 PASS**，`全部通过` | **0** |
| `python tests/smoke.py` | **22/22 PASS**，`全部通过` | **0** |
| `python tests/links.py` | `链接全部有效（14 个文件，按 git 跟踪文件核对）` | **0** |
| `python pc/hub.py --help` | usage 含 `[--log PATH]`；`--log PATH  日志文件路径（UTF-8），默认只打到 stderr` | **0** |
| 独立探针 `/tmp/hub_review_probe2.py` | **16/16 PASS**，`全部通过` | **0** |

`hub_serial.py` 的 27 项全过（含 2b 数据清零用例）；`smoke.py` 全过说明 `pc/hub.py` 语法/编译面没被改坏。

---

## 3. 代码审查：`pc/hub.py`

### 3.1 `SerialError.code/permanent`（`pc/hub.py:152`）

```python
def __init__(self, message="", code=None, permanent=False):
    super().__init__(message)
    self.code = code
    self.permanent = permanent
```

- 默认 `permanent=False`，旧调用 `SerialError("...")` 语义不变；全仓库其余抛错处无需改。
- 测试构造、`getattr`、`str(exc)` 都正常（27 项前 3 项 PASS）。

### 3.2 `SerialLink.read()` 分类（`pc/hub.py:248-263`）

`_PERMANENT_READ_ERRORS`（`:144`）恰好是任务要求的四项：

```python
ERROR_DEVICE_NOT_CONNECTED, ERROR_INVALID_HANDLE,
ERROR_BAD_COMMAND, ERROR_FILE_NOT_FOUND
```

分支顺序：先 `ERROR_OPERATION_ABORTED / ERROR_SEM_TIMEOUT` → `return b""`；再 permanent → `SerialError(code=err, permanent=True)`；其余 → `SerialError(code=err, permanent=False)`。

**独立探针直接替换 `ReadFile`**（不碰串口）逐码验证：

| WinError | 结果 |
|---|---|
| 1167 DEVICE_NOT_CONNECTED | `permanent=True, code=1167` |
| 6 INVALID_HANDLE | `permanent=True, code=6` |
| 22 BAD_COMMAND | `permanent=True, code=22` |
| 2 FILE_NOT_FOUND | `permanent=True, code=2` |
| 995 OPERATION_ABORTED | `b""`（不抛） |
| 121 SEM_TIMEOUT | `b""`（不抛） |
| **31 GEN_FAILURE** | **`permanent=False`（瞬时，符合任务）** |
| 5 ACCESS_DENIED | `permanent=False` |
| 87 INVALID_PARAMETER | `permanent=False` |

> 结论：分类**完全正确**；`GEN_FAILURE` 已从旧的 permanent 元组里移出，不会再“偶发抖一下就整条透传断掉”。

### 3.3 `_reader_loop()` 有界重试（`pc/hub.py:411-446`）

代码事实：

- `soft_errors` 局部计数；**读到非空数据后清零**（`:444`）。
- 瞬时错：`soft_errors += 1`；`< MAX_SOFT_READ_ERRORS(5)` 时 `time.sleep(0.1)` 后 `continue`；第 5 次记日志、`_close_serial()`、`return`。
- permanent：**不重试**，直接 `_close_serial()`、`return`。
- `b""`（空闲/ABORTED）只 sleep 2 ms，不计数。
- 关闭后由 `_manager` 的 `if self._serial is None:`（`:474`）重新打开，不会永久卡死。

独立探针实测（假串口，直接调 `_reader_loop`）：

| 场景 | 实测 |
|---|---|
| 一条 permanent | 只读 **1** 次，`<0.1s` 退出，`serial_open=False`，`close()` 被调 |
| 连续瞬时错 50 条 | 只读 **5** 次就关，不死循环；耗时约 **0.40s**（断言 0.30–0.70s，**<0.5s**） |
| 4 瞬错 + 1 数据 + 1 瞬错 + 1 真拔 | 共读 **7** 次，能读到最后的真拔 → 数据清零生效，没有把累计错当连续错提前关 |

> 结论：**不会死循环**；“真拔了”最坏 5 次瞬时错 → 约 0.4–0.5s 发现，permanent 则立即发现；符合“0.5s 内应能发现”。

### 3.4 `--log`（`pc/hub.py:1622-1650`）

- `--log` 默认 `""`；不加参数时**不创建** `FileHandler`，只保留原来的 `basicConfig` stderr。
- 加了参数：`FileHandler(args.log, encoding="utf-8")`；失败（如传目录、无权限）打印中文错误并 `return 1`，不会悄悄忽略。
- 启动时写一条 `日志同时写入 <绝对路径>`。

独立探针：

- `hub.main([... --once-board])`（不传 `--log`，`run_once_board` 打桩）→ 根 logger **没有新增 FileHandler**，退出码 **0**。
- 传一个目录作为 `--log` → 退出码 **1**，stderr 有 `打不开日志文件 ...`，不崩。
- `tests/hub_serial.py` 5/5b 也验证了真文件写入和 `hub.main --log` 接线。

> 结论：**默认行为不变**，`--log` 是纯增量。

### 3.5 代码层备注（不阻塞）

- `ERROR_GEN_FAILURE` 现在只在 `pc/hub.py:70` 被 import，文件内已无使用（`transport_uart.py:576` 才用它）——P2 清掉即可，不影响运行。
- `_reader_loop` 只 `except SerialError`；若 `link.read()` 抛出非 `SerialError`（ctypes/OSError），线程会死而 `_serial` 仍非空，`_manager` 不会重开——P2，属既有风险，非本轮引入。
- 关闭串口时没有把 `state` 从 `online` 改成 `offline`，重开前 `/api/board` 可能短暂返回 `serial_open=false` + 旧 stats——P2，既有行为。

---

## 4. 测试审查：`tests/hub_serial.py`

### 4.1 是否覆盖四条契约

| 契约 | 用例 | 结果 |
|---|---|---|
| 瞬时错不关串口 | 瞬错 → JSON → permanent；检查每次 read 时 `serial_open=True`、`_read_stop` 未置位、JSON 真喂给解析 | PASS |
| 真拔才关 | permanent 错误 → `serial_open=False`、`close()` 被调、只读 1 次 | PASS |
| 超限兜底 | 20 条连续瞬错 → 关闭、退出、不无限重试 | PASS（断言偏松，见 P2） |
| `--log` 写文件 | 直接 FileHandler 中文 marker + 真走 `hub.main --log`（`--once-board` 打桩） | PASS |

额外覆盖：**读到数据后计数清零**（4 瞬错 + 数据 + 1 瞬错 + permanent），这个用例能抓住“忘了清零”的回归；写得不错。

### 4.2 是否真不碰真串口 / 真 hub

逐行核对：

- `make_board()` 用 `hub.BoardLink("COM99", ..., use_lock=False)` 构造，**不调 `start()`、不取锁、不 `open()`**；只手动塞 `_serial = fake`。
- `run_reader()` 只启动 `_reader_loop` 线程，并把 `hub.SerialLink` 临时替成 `lambda: fake`；不建 `Hub`、不建 HTTP、不 bind 8765/8766。
- `hub.main` 用例把 `run_once_board` 打桩后立刻返回（`--once-board`），不会走到 `detect_port()`/`Hub`/串口。
- 实测运行 <1s，无串口占用、无板子锁等待、无端口监听。

> 结论：**测试不会在 CI/别人机器上碰真串口或起真 hub**；只 import 了 `hub` 模块（模块级无打开串口动作）。

### 4.3 测试的不足（P2）

1. `:222` 断言 `2 <= fake.transient_sent <= 6`，而实现是 **5**；如果实现退化成 2 次或 6 次，测试仍会过。建议改成 `fake.transient_sent == MAX_SOFT_READ_ERRORS`（或 5）。
2. 文件头 `:14` 写“连续 6 次瞬时错兜底”，与实现的 5 不一致；顺手改成 5。
3. 测试用 `SerialError(..., permanent=...)` 假对象，**没有直接测 `SerialLink.read()` 的 WinError 映射**；本轮我用独立探针补了 9 个码，建议把其中 4 permanent + ABORTED/SEM/GEN 固化成用例。
4. 没有断言“不加 `--log` 时不新增 FileHandler”；本轮我用探针补了，建议进测试。

---

## 5. 文档审查：`docs/ai-controller.md` 新节（181-252）

### 5.1 换算公式与锚点

公式（`:193`）：

```
屏幕x = 画布左上物理x + 状态栏x × 缩放
屏幕y = 画布左上物理y + 状态栏y × 缩放
```

与两锚点数据一致。用表内数字重算：

```
x: 550 − 64×1.8 = 434.8；730 − 164×1.8 = 434.8  → 435  ✅
y: 761 − 176×1.8 = 444.2；781 − 187×1.8 = 444.4  → ≈444（文中写 445）
```

- 缩放 **1.8** 正确；原点 x **≈435** 正确。
- 原点 y 文中 **≈445**，按表内数据是 **444.2 / 444.4**，差 0.6–0.8 px；属于**取整/数据不一致**，列 **P2**（不影响“先标定再画”的方法，但数字应自洽）。

### 5.2 `drag_path` 描述 vs `pc/brain.py`

逐条对照：

| 文档说法 | 代码事实 | 判定 |
|---|---|---|
| “逐点闭环定位” | `drag_path` 对起点和每个中间点调 `locate()`（`pc/brain.py:553,565`） | ✅ |
| “容差约 1 px” | `drag_path(..., tol=1)`（`:534`），`locate` 默认 `tol=1`（`:479`） | ✅ |
| “每步 settle 10 ms” | `_move_step(..., settle_ms=10)`（`:467`），`drag_path` 默认 `settle_ms=10` | ✅ |
| “抬笔在 finally” | `:601` `finally: if pen_down: mouse_btn(up)` | ✅ |
| “没有点间 wait；塞 wait 不会变慢” | `exec_step` 只取 `points/button/guard`，未知键被忽略（`:738-740`） | ✅（但见下条） |
| “只认 points 和 button” | 实际还认 **`guard`**（`:740` `step.get("guard", True)`） | ⚠️ P2：补一句 guard 开关 |
| “断线多半是 10053/10054” | 协议层可能，但本轮未实测 | ⚠️ P2：标成经验判断或实测后再写 |

> “点密=拐角圆顺但慢、点稀=相邻两点连直线”是行为经验，和逐点 locate 不矛盾；没有可证的硬错误。

### 5.3 “画不出线”三步诊断

1. point 到“以为的左上角”，OCR 读状态栏接近 (0,0) —— 与“两锚点/原点”自洽。
2. 用 `drag_path` 画已知矩形，出现矩形 = 映射对 —— 可执行，参数是合法 op。
3. 两点 `drag` 只出一个点 = 起点落在画布外，往内收 20~40 px —— 与 `drag` 的“定位→按下→定位→抬起”实现一致，是合理排障。

三步**自洽**，没有会误伤用户的动作（只移动/画图）。

### 5.4 清空画布建议的安全性与准确性

| 建议 | 安全性 | 准确性 |
|---|---|---|
| `Ctrl+A` → `Delete` | 安全：只清当前画布，不会自动保存/关闭 | ⚠️ 文中写“清成透明棋盘格，不是白底”；本轮**没有实际打开 Paint 验证**，按 MS Paint 常见行为更可能是清成白底。列为 **P1（建议改）**：实测后改成准确描述，或改成不依赖“透明/白底”的写法 |
| 要白底：前景白 + 填充工具 | 安全 | 可行 |
| `Ctrl+N` 别用：可能弹保存更改 | 安全（避免卡在对话框） | 合理 |
| 开工前 `Ctrl+S` 先存 | 安全（覆盖当前文件，符合“先存一版”） | 合理 |

> 关键：**没有数据丢失风险**（不自动保存、不关闭文档）；P1 只是“透明棋盘格”这一句可能误导，不影响操作安全性。

### 5.5 换电脑/缩放部分

- `C:\ProgramData\dsh\pointer-profile.json`：与 `tools/home-calibrate.py:57-58` 一致（`%ProgramData%` 回退 `C:\ProgramData`）。
- “按 分辨率 | 虚拟桌面 | DPI 模式 分 key”：与 `home-calibrate.py:299-303` 的 signature（`宽x高|virtual=宽x高|dpi=`）一致。
- `python tools/home-calibrate.py --transport tcp --apply`：`--transport {uart,tcp}`、`--apply` 都存在（`home-calibrate.py:192-193`），命令可执行。
- “`--selfcheck` 看「指针校准」软项”：实跑 `python tools/agent.py --selfcheck` 退出 0，输出里确有 `"check": "指针校准"`（软项，`connector.py:1093` `hard=False`）。
- “约 8 s”：本轮未跑真实标定，未验证；P2 标注即可。

---

## 6. 分级清单

### P0（必须改，阻塞）—— **0 项**

未发现会让用户/板子出事、或让断连修复失效的 P0。

### P1（建议改，不阻塞签发）—— 1 项

**P1-1 文档把 Paint 清空结果写成“透明棋盘格”未实测，可能不准确。**
位置：`docs/ai-controller.md:239`。
建议：用一次真实 `Ctrl+A`→`Delete` 确认；若实际是白底，改成“清成白底/清空当前画布”，并把“要白底”的下一行相应调整；保留“Ctrl+N 可能弹保存、先 Ctrl+S”的安全建议。

### P2（可后续处理）

1. `docs/ai-controller.md:211`：原点 y 按表内数据是 444.2/444.4，文中写 445；建议改成 `≈444` 或把锚点数据调成能推出 445 的值，保持自洽。
2. `docs/ai-controller.md:227-228`：“只认 points 和 button”漏了 `guard` 开关（`pc/brain.py:740`）；补一句“默认 guard=true，可显式关”。
3. `tests/hub_serial.py:222`：`2 <= transient_sent <= 6` 太松，改成恰好 5；`:14` 的“连续 6 次”改成 5。
4. `tests/hub_serial.py`：补 `SerialLink.read()` 的 WinError 映射用例（4 permanent + ABORTED/SEM_TIMEOUT/GEN_FAILURE），以及“不加 `--log` 不加 FileHandler”的断言。
5. `pc/hub.py:411`：`_reader_loop` 只捕 `SerialError`；非 `SerialError` 异常会让 reader 线程静默死掉而 `_serial` 仍非空，manager 不会重开。建议兜底捕获/在 finally 里标记或让 manager 检查线程存活（既有风险，非本轮引入）。
6. `pc/hub.py:403`：`_close_serial` 只置 `serial_open=False`，不改 `state`/不清旧 stats；重开前 `/api/board` 可能短暂显示 online+旧数据。建议关闭时置 `offline`（既有行为）。
7. `pc/hub.py:70`：`ERROR_GEN_FAILURE` 已不在文件内使用，删掉 import（或留给文档/日志用）。
8. `docs/ai-controller.md:233` 的 “10053/10054” 与 `:247` 的 “约 8 s” 标成经验值/实测值；本轮未验证，建议注明“经验”或补测。

---

## 7. 未验证（照实列原因）

| 项 | 原因 |
|---|---|
| 真 CH340 物理拔插的恢复时延 | 没有拔用户的板子；用假串口注入 `GEN_FAILURE` + 直接替换 `ReadFile` 验证分类/时延。真拔可能报 1167/31/其它码，本轮未实测。 |
| 真 hub 进程 + 真板子的断连/重连 | 没有启动真 hub（会占 8765/8766、板子锁和串口），避免打断用户正在用的板子；`--help`、测试和打桩 `main` 已跑。 |
| Paint 里 `Ctrl+A`→`Delete` 的实际视觉结果 | 没有打开 Paint 操作桌面；文档建议的安全性已评估，视觉描述列入 P1 待实测。 |
| `home-calibrate.py` 真实四角标定“约 8 s” | 未跑真实标定（会移动指针/占板子）；只核对了命令参数、profile 路径/key。 |
| `10053/10054` 是断线主因 | 未做网络抖动注入；属文档经验判断。 |
| 真板子上的 `ERROR_GEN_FAILURE` 是否就是拔线码 | 按任务给定分类实现；真机错误码分布未采集。 |

---

## 8. 结论

**通过。**

- P0：**0**。
- P1：1 项，仅文档“透明棋盘格”表述（未实测/可能不准），建议实测后改写；不影响清空操作安全性。
- 核心代码：`SerialError` 字段、四类 permanent、ABORTED/SEM_TIMEOUT→`b""`、GEN_FAILURE 瞬时、5 次有界重试（约 0.4s）、permanent 立即关、`--log` 默认只 stderr——**全部与任务要求一致**。
- 测试：27 项全过，真不碰真串口/真 hub；四条契约都覆盖，弱在“恰好 5 次”和 WinError 映射未固化。
- 文档：公式/缩放/两锚点方法/`drag_path` 的 tol=1、settle=10ms、逐点 locate、finally 抬笔均与 `pc/brain.py` 一致；y 原点差 <1px，列 P2。

复现命令：

```bash
python tests/hub_serial.py
python tests/smoke.py
python tests/links.py
python pc/hub.py --help
```
