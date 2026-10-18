# 审查报告：tools/agent_ui.py（tkinter 窗口版）

审查人：审查部（**只读**，未改任何 `.py`，未碰 `firmware/`，未 commit/push）
审查对象：`tools/agent_ui.py`，445 行，mtime 2026-10-18 17:21:29，
sha256 `424dee8be94ee3dec1de356f0298eb3cfd68f1c3a0f4ced4a62d8123acc3057a`
基准：`tools/agent.py`（215 行）、`tools/connector.py`（1128 行）、
`tools/build-ui.cmd`、`pc/hub.py`、`tools/photo-draw-gui.py`
已跑验证：`python tests/ui_smoke.py` → **31 项全 PASS，退出码 0**（实测，未进 mainloop）

## 结论摘要

| 级别 | 条数 | 一句话 |
|---|---|---|
| **P0** | **2** | ① 按钮不禁用 → 同进程并发跑 connector，截屏缓存/临时图/OCR 引擎全串；② Tk 回调异常没有任何兜底，`--windowed` 下 `stderr=None` 连 tkinter 默认处理器自己都会二次抛错 |
| **P1** | **5** | 启动失败兜底又用 Tk、关窗硬杀作业、`update()` 重入、空 steps+require 假成功、服务状态恒打 ✓ |
| **P2** | **14** | 崩溃日志误判、剪贴板不持久、重复实现/多余单例、selftest 放生产代码等 |

总评：**线程模型本身是对的**（worker 只碰 `queue.Queue`，`_drain` 在主线程序列化结果，
没有在线程里碰控件——和 `tools/photo-draw-gui.py:139-175` 的既有惯例一致）。
作业输入校验、另存为（UTF-8 无 BOM、建父目录、失败弹窗）、复用 connector/agent 这几块
做得干净。真正的问题是**「忙」的状态没人管**和**异常最后一道网没盖住 Tk 回调**。

---

## 一、Lead 已修项确认（不重复报，只确认）

| 声明 | 核对结果 |
|---|---|
| 空作业 `[]` / `{"steps":[]}` 返回 False +「作业里没有步骤」 | ✅ `agent_ui.py:184-186`；但放行 `require` 的那一半有坑，见 **P1-4** |
| `_pick_transport` 已挪进 try | ✅ `agent_ui.py:187-191`，异常被包成中文「执行时出错：…」 |
| 排版：提示标签第二行、两个框 `width=1` | ✅ `agent_ui.py:241-243`、`248`、`256`；`tests/ui_smoke.py` 的窗口构建项 PASS |
| windowed 下 selftest 的 print 加守卫 | ✅ `agent_ui.py:419-421` `if sys.stdout is not None`；但**只覆盖 selftest 分支**，主崩溃路径 `439` 用的是 `print(...) if sys.stderr else None`（能工作，写法怪），Tk 回调路径仍然没盖（P0-2） |
| 新增 `windowless_selftest()` + 环境变量入口 | ✅ `agent_ui.py:377-422`；价值与去留见 **P2-6** |

---

## 二、P0（会崩 / 会骗人）

### P0-1 连点按钮会真的并发：同一进程里的截屏缓存、临时图、OCR 引擎全被两个作业共享
`tools/agent_ui.py:286-297`（`_bg` 每次无条件下线程，无单飞标志）、
`tools/agent_ui.py:345-356`、`372-374`（`btn_check`/`btn_svc`/`btn_run` 从不禁用）

- 现象：自检和「用板子运行」可以同时点、同一个按钮也能连点。两个线程共享
  `_cap` 截屏缓存（`connector.py:191-193`，5 秒内直接复用旧画面）、
  `_tmp()` 的临时图名只按 pid 区分（`connector.py:202-204`，注释自己写的是
  「两个连接器同时跑时不能抢同一个文件」——那是**跨进程**设计，同一进程两个线程 pid 相同）、
  `_cleanup_tmp()` 会把对方正在读的图删掉（`connector.py:207-214`，
  调用点 `1026`、`1087`）、`_ocr_engine` 懒加载没有锁（`connector.py:81`、`267-279`）。
- 后果：轻则第二个作业因 hub 只允许一个透传客户端被拒（`pc/hub.py:679-690`），
  报一句莫名其妙的断连；重则 B 作业复用 A 的旧画面算坐标 → **点错位置**（会骗人），
  或临时图被对方清理导致 OCR 读坏图。
- 改法：`_bg` 加单飞——`self._busy` 标志（忙时直接 return 并把状态栏翻成「上一个任务还在跑」），
  或进任务时 `state="disabled"` 三个耗时按钮、`_drain` 收到结果后恢复；耗时活一律串行。

### P0-2 异常兜底没盖住 Tk 回调：`--windowed` 下按钮里的未捕获异常无日志、无弹窗
`tools/agent_ui.py:423-440`（只 `try` 了 `App()` 和 `app.mainloop()`；全文件没有
`report_callback_exception`、没有 `sys.stdout/stderr` 兜底）

- 现象：tkinter 的回调异常不会冒到 `mainloop()` 外面——它走
  `CallWrapper → root.report_callback_exception`。标准库实现原文就是
  `print("Exception in Tkinter callback", file=sys.stderr)` + `traceback.print_exception(...)`，
  且其 docstring 明确写着「Applications ... **should** [override] when sys.stderr is None」。
  而 `--windowed` 打包后 `sys.stdout`/`sys.stderr` 就是 `None`（本报告按任务书前提），
  `print(..., file=None)` 会退回 `sys.stdout` → `None.write` → **在异常处理器里再抛一次
  AttributeError**。结果：没有崩溃日志、没有弹窗，按钮静默失效；崩溃日志函数写了也没人调用。
  这正好打脸文件头「双击打开一个窗口，不闪退」。
- 哪些回调真的可能未捕获：`on_copy_manual` 的 `update()` 之后（见 P1-3）、
  `on_read_manual` 的 `Toplevel`/`ScrolledText` 创建（`364-370` 不在 try 内）、
  `filedialog.asksaveasfilename`（`317-320` 不在 try 内）、`_drain` 里
  `_log`/`after` 抛 `TclError`（`299-307`）。`_bg` 的 worker 有 try，不在此列。
- 改法：`App` 覆盖 `report_callback_exception(self, exc, val, tb)`：
  `where = write_crash("回调", "".join(traceback.format_exception(exc, val, tb)))`
  然后 `messagebox.showerror("出错了", ...)`（messagebox 失败再退到
  `ctypes.windll.user32.MessageBoxW`）；并在 `main()` 最前面把
  `sys.stdout/stderr is None` 换成 `io.StringIO()`/日志文件，双保险。

---

## 三、P1（明显不对）

### P1-1 启动失败的兜底又去创建 Tk，第二次大概率同样失败 → 用户什么都看不到
`tools/agent_ui.py:424-433`

- 问题：`App()` 失败后 `write_crash` 已写日志，但弹窗依赖 `tk.Tk()`；如果失败原因就是
  「Tk 起不来」（缺 DLL/无交互桌面/资源耗尽/打包漏了 tkinter），第二次 `tk.Tk()` 同样失败，
  被内层 `except Exception: pass` 吞掉 → 双击后**无声无息**，用户不知道有日志、更不知道路径。
- 改法：兜底改用不依赖 Tk 的 `ctypes.windll.user32.MessageBoxW(0, msg, "启动失败", 0x10)`；
  并且按 **P2-2** 让 `write_crash` 返回「真的写成了」的路径，没写成时文案别再说「已写入」。

### P1-2 没有关窗确认/取消：作业跑到一半关窗 = daemon 线程被硬杀，板子可能停在按键中
`tools/agent_ui.py:286-297`（daemon 线程）、`435-436`（mainloop 退出即进程结束）；
全文件无 `protocol("WM_DELETE_WINDOW", ...)`

- 问题：`recipe`（微信发消息）、大段 `type`、`hold` 这类长作业没有取消入口；用户点 ×
  后进程直接退出，`run_job` 的 `finally`（`connector.py:1020-1026`）不保证执行
  （解释器退出时 daemon 线程被硬杀），板子可能停在「键按下/拖拽中」的状态。
- 改法：`self.protocol("WM_DELETE_WINDOW", self._on_close)`：忙时
  `messagebox.askokcancel("作业还在跑", "现在关窗会中断板子输入，继续？")`；
  状态栏显示「运行中…」，至少让用户知道关窗的代价。

### P1-3 `on_copy_manual` 在回调里 `self.update()`：重入事件循环，可能处理掉关窗事件
`tools/agent_ui.py:339`

- 问题：`update()` 会处理**所有**待处理事件（包括 `WM_DELETE_WINDOW`/`destroy`）；
  如果窗口在这行里被销毁，返回后的 `self._log(...)`（`343`，在 try 内）或
  `except` 分支里的 `self._log`（`340-341`）会碰已销毁控件抛 `TclError`，
  又落进 **P0-2** 的无声黑洞。另外 `update()` 并不能让剪贴板内容在关窗后存活（见 P2-3）。
- 改法：改成 `self.update_idletasks()`（只刷界面、不处理关窗），或直接删掉这行；
  在提示里写「粘完再关窗口」。

### P1-4 空 steps 带顶层 `require` 被放行，但 connector 不执行顶层 require → 显示 ✓ 完成
`tools/agent_ui.py:184-186`

- 问题：`{"steps": [], "require": "xxx"}` 通过校验，`connector.run_job` 只读
  `job.get("steps")`（`connector.py:828-831`），顶层 `require` 没有任何人执行 →
  返回 `{"ok": true, "steps": []}` → 界面打「运行作业 → 完成 ✓」，实际什么都没做。
- 改法：删掉 `and job.get("require")` 这个例外，空 steps 一律返回 False。
  `require` 本来就是**每步**字段（`connector.py:1007-1010`），没有顶层语义。

### P1-5 服务状态恒返回成功：三处全挂时标题仍是「查服务状态 → 完成 ✓」
`tools/agent_ui.py:355-356`（`lambda: (True, services_text())`）

- 问题：正文里会用 ✗ 标出 hub/板子/视觉不在，但 `_drain` 的标题行只认 `ok`
  （`302-303`），会先打一行带 ✓ 的「完成」。用户扫一眼标题就以为服务都在。
- 改法：`ok = 三处任一 up`，或把标题行改成「查询完成（服务不一定在）」并不打 ✓。

---

## 四、P2（可以更好）

1. **`_drain` 重排不在 `finally` 里** `agent_ui.py:299-307`：`_log` 抛一次 `TclError`
   （关窗瞬间）轮询链就断了，之后所有后台结果永远不显示。改法：
   `try: ... finally: if self.winfo_exists(): self.after(120, self._drain)`。
   （注：**正常关窗**不会有「after 碰已销毁控件」——Tk 随解释器销毁会丢弃未触发的 after；
   真正的生命周期风险是 P1-3 的 `update()` 重入。）
2. **崩溃日志路径会误判 + 弹窗谎报** `agent_ui.py:107`、`121-123`、`430`：
   Windows 上 `os.access(d, os.W_OK)` 只近似看只读属性，**不查 ACL**；Program Files、
   UAC 虚拟化目录、网络盘都可能返回 True 但实际写不进去。`write_crash` 吞掉 `OSError`
   仍返回路径，弹窗说「详情已写入：<path>」，用户拿到的可能是个不存在的文件。
   改法：不预判，直接按 `base → %TEMP%` 顺序尝试 `open(..., "a")`，返回**实际写成**的路径；
   都没写成返回空串，弹窗文案相应改口。
3. **剪贴板关窗后大概率丢** `agent_ui.py:334-343`：Tk 在 Windows 用 delayed rendering，
   内容绑定进程/解释器生命周期，**进程退出后通常变空**；`update()` 只保证本次会话内
   Tk 接管剪贴板，不解决持久化（339 行注释的说法不准确）。当前流程（复制→粘→再关窗）可用，
   建议在提示里写明「粘完再关窗口」；若要跨退出持久化，用 `ctypes` 直接
   `OpenClipboard/SetClipboardData`（约 20 行，不必加依赖）。
4. **`_app` 单例 + `_lock` 是多余的** `agent_ui.py:47-48`、`131-137`：`import connector`
   本身幂等且线程安全，模块缓存就是单例；锁只保护 import，起不到别的串行作用。
   保留「懒加载」（避免启动就加载 draw-rage/Pillow）即可，函数体三行变一行。
5. **`_base_dir`/`manual_path` 重复实现 `agent._manual_path()`** `agent_ui.py:53-59`
   vs `agent.py:75-78`：打包路径应该只有一个定义，否则将来改一处忘另一处。改法：
   `return _agent()._manual_path()`。
6. **`windowless_selftest` 放在生产代码里** `agent_ui.py:377-422`：30 行测试脚手架 +
   环境变量入口；`tests/ui_smoke.py` 已经覆盖同样的纯函数和窗口构建。它唯一不可替代的
   价值是验证 **frozen 包**（内嵌说明书、无控制台），但入口风格与 `agent.py` 的
   `--manual/--caps/...` 不一致（`main(argv)` 完全没用 argv）。建议：改成 `--selftest`
   参数（环境变量兼容保留），测试产物 `%TEMP%\agent-ui-selftest.md` 用完删掉。
7. **`main(argv)` 收参数不用** `agent_ui.py:409`、`445`：也没有 `--version`/`--help`，
   与命令行版约定不一致。要么删掉 argv，要么补上 `--version`（顺手解决 P2-6 的入口）。
8. **自检结果先 dumps 再 loads 取 ok** `agent_ui.py:345-352`：绕一圈只为读 `"ok"`。
   改法：让 `selfcheck_text()` 返回 `(ok, text)`，或抽一个 `selfcheck_ok()`。
9. **保存说明书前读两遍** `agent_ui.py:312` + `save_manual` 内部 `81`：预读只用于
   「没说明书也先别弹保存框」，可以保留 UX，但把 `text` 传给 `save_manual`，别读两次。
10. **按钮文案「保存 AI 说明书…（下载）」** `agent_ui.py:231`：这是本地另存，没有任何
    下载动作，容易让人以为要从网上下。改「保存 AI 说明书…」。
11. **中文报错有漏网** `agent_ui.py:293-294`、`72`：`_bg` 兜底直接拼
    `"%s: %s" % (type(e).__name__, e)`，`manual_text` 的 `UnicodeDecodeError` 也没包中文，
    用户可能看到纯英文。至少包一层「出错了：…」，或把常见异常换成中文说明。
12. **顶层 `import tkinter` 无兜底** `agent_ui.py:35`：用错打包脚本时（`build-agent.cmd:22`
    明确 `--exclude-module tkinter`）打出来的 EXE 双击会在 import 处直接死，连日志都没有。
    `build-ui.cmd` 是对的；建议 `try/except` 后用 `ctypes` 弹一句「这个包没带界面组件」。
13. **transport 没有逃生口** `agent_ui.py:163-165`、`188`：UI 固定 `_pick_transport("auto")`，
    不认 `CONNECTOR_TRANSPORT`，也没有 `agent.py --transport tcp|serial` 的对应入口。
    **语义本身一致**（8766 通→tcp，否则 serial，`agent.py:131-135`），只是没法强制。
    需要时加一个下拉或读环境变量。
14. **`status` 永远是同一句静态提示** `agent_ui.py:277-279`：加了 P0-1 的单飞之后，
    正好用它显示「自检中…/运行作业中…」，比一句固定提示有用。

---

## 五、Lead 点名的五个判断（直接回答）

1. **`_bg` + `queue.Queue` + `after(120)` 有没有竞态？**
   **主路径没有。** worker 只 `q.put`（`295`），所有控件操作都在主线程
   （`_log` 由 `__init__`/按钮回调/`_drain` 调用），`_drain` 也只在主线程重排。
   窗口 destroy 后：未触发的 after 随解释器销毁丢弃，worker 往 Queue 里 put 不碰控件，
   进程退出时 daemon 线程被回收——不会「碰已销毁控件」。
   **两个真实缺口**：① `on_copy_manual` 的 `update()` 重入（P1-3）；
   ② `_drain` 自己抛错导致轮询链断（P2-1）。**连点按钮会并发**（P0-1，现在没禁用）。

2. **`_dpi_aware()` 在 `tk.Tk()` 前调用有没有顺序问题？**
   **顺序正确**：`agent_ui.py:213` 先 `_dpi_aware()`，`214` 才 `super().__init__()`，
   DPI 感知必须在任何窗口创建前设置。`SetProcessDpiAwareness(2)`（per-monitor v2）
   是正确选择，失败回落 `SetProcessDPIAware()` 也合理。`geometry` 是**物理像素**，
   150% 下 `960x660` 约等于 640x440 逻辑像素，偏小但可用，不会「失真」。
   唯一要接受的限制：Tk 不处理 `WM_DPICHANGED`，窗口拖到另一块不同缩放的屏上不会重算尺寸
   （这是 Tk 的坑，不是这段代码的错误，P2 级、不建议为此加代码）。

3. **`clipboard_append` + `update()`，关窗后剪贴板会不会丢？**
   **大概率丢**：Tk 在 Windows 上 delayed rendering，内容跟着进程走，退出后通常变空；
   `update()` 只保证 Tk 完成接管，**不能跨进程退出保活**（行内注释不准确）。
   当前用法（复制完立刻粘贴）没问题；提示里写「粘完再关窗口」即可，要真持久化就
   用 `ctypes` 写系统剪贴板，不建议为此引入 `pyperclip`。

4. **`log_path()` 用 `os.access(W_OK)` 在 UAC/网络盘会不会误判？**
   **会。** Windows 上 `os.access(W_OK)` 不查 ACL、只看近似属性，Program Files、
   被 UAC 虚拟化的目录、部分网络盘都可能报「可写」然后 `open` 失败；而
   `write_crash` 吞掉 `OSError` 还返回路径，弹窗继续说「详情已写入」——见 P2-2，
   这条建议直接改掉，别只修判断。

5. **有没有过度设计该删的？**
   有，都不大：`_app`/`_lock` 单例（P2-4）、`_base_dir`/`manual_path` 重复
   `agent._manual_path`（P2-5）、`main(argv)` 空收参数（P2-7）、自检结果 dumps→loads
   （P2-8）、保存前重复读说明书（P2-9）。`windowless_selftest` **不建议直接删**
   （它是唯一能验证 frozen 包内嵌说明书的入口），但应该从「环境变量 + 生产类里的测试代码」
   改成 `--selftest` 参数（P2-6），与命令行版约定对齐。

---

## 六、一致性与红线检查（已逐项核对）

| 检查项 | 结论 |
|---|---|
| 不碰 `firmware/` | ✅ `grep firmware tools/agent_ui.py` 无命中；没有编译/烧录调用 |
| 不最大化用户窗口 | ✅ 无 `state("zoomed")`/`attributes("-zoomed")`/`attributes(`；固定 `960x660`、`minsize(780,540)`，看说明书 Toplevel 固定 `900x700` |
| 错误信息中文 | ⚠️ 主流程中文；异常兜底/解码错误会漏英文（P2-11） |
| 退出码语义（0/1/2） | ✅ UI 不伪造作业退出码；崩溃返回 1、selftest 0/1，和 `agent.py:13` 不冲突 |
| transport 语义 | ✅ auto 判定与 `agent.py:131-135` 完全一致；⚠️ 无 `tcp`/`serial` 强制入口（P2-13） |
| 执行逻辑不重写 | ✅ 作业走 `connector.run_job`、自检走 `connector.selfcheck`、服务/能力/说明书路径复用 `agent.py`；没有自己解析/分发步骤 |
| 另存为 | ✅ UTF-8 无 BOM（`82`、`91-92`，实测通过）、父目录自动建（`85-89`）、失败弹中文（`93-94`/`326-328`）、取消不报错（`321-322`）、默认文件名 `AI-控制器说明书.md`（`38`、`318`） |
| 运行作业真实性 | ✅ 空输入/非法 JSON/非对象/步骤缺 op 都是 `(False, 中文)`；`ok:false` 显示「失败」并带完整 JSON/err（`192`、`302-304`）。**唯一例外**是 P1-4 的空 steps+require |
| 打包 | ✅ `tools/build-ui.cmd` 存在：`--windowed`、**没有** `--exclude-module tkinter`、`--add-data docs\ai-controller.md;docs`、hidden-import connector/brain/eyes/visiond；与 `build-agent.cmd` 的 `--exclude-module tkinter` 有明确区分 |
| 现有 UI 惯例 | ✅ 配色/字体跟 `photo-draw-gui.py:45-70` 一套；线程 + `root.after` 回主线程同一模式 |

---

## 七、结论

- **可以交付给人用，但 P0-1 建议在打包前修**：并发点击会共享 connector 的进程级全局状态
  （截屏缓存/临时图/OCR 引擎），最坏情况是「画面是旧的、坐标算错、鼠标点歪」——这比报错更难查。
  改动很小：`_bg` 一个 busy 标志 + 三个按钮禁用/恢复。
- **P0-2 是「双击不闪退」承诺的最后一块缺口**：顶层 try 只包住了启动和 mainloop，
  Tk 回调异常在 `--windowed` 下会被 tkinter 默认处理器（`stderr is None`）二次搞坏。
  覆盖 `report_callback_exception` 约 10 行，值得做。
- P1 五条都是真实行为问题，其中 P1-4（假成功）和 P1-2（关窗硬杀）建议一起修；
  P1-1/P1-3/P1-5 各自几行。
- P2 十四条里真正值得动手的是 2（日志路径）、3（剪贴板文案）、4/5（删重复）、6（selftest 入口）；
  其余可以放着，不阻塞交付。

> 红线遵守情况：本次审查**没有修改任何 `.py`**，没有 `git commit/push`，没有读写 `firmware/`；
> 唯一写入是本报告 `docs/reviews/agent-ui-review.md`。
