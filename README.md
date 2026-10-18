# esp32p4-hid-agent

**中文** | [English](README.en.md)

把一块 **ESP32-P4 开发板**变成这台 Windows 上的**标准 USB 键鼠**，再用一个 **JSON 连接器**把它用起来：
截屏 → 判断 → 动鼠标 / 敲键盘 → 再截屏核对。硬件输入、零驱动、不碰任何软件注入。

```
   ┌──────────────┐  USB HID（键盘+鼠标）  ┌──────────────┐
   │ ESP32-P4 板子 │ ────────────────────▶ │  这台 Windows │
   └──────┬───────┘                       └──────▲───────┘
          │ UART 115200 / TCP 透传                 │ 截屏
   ┌──────▼───────────────────────────────────────┴───────┐
   │ tools/connector.py —— 读一份 JSON 作业，用板子执行     │
   └──────────────────────────────────────────────────────┘
```

---

## 从零跑通（陌生机器）

两个前置，各做一次：

| 前置 | 怎么做 | 通过的标志 |
|---|---|---|
| Python 3.12 + Pillow | `python -m pip install -r pc/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` | `python -c "import PIL; print(PIL.__version__)"` 有版本号 |
| CH340 串口驱动 | 管理员运行 `tools\install-ch340-admin.cmd`（或设备管理器里让 Windows 自动装） | 设备管理器「端口」下出现 `USB-SERIAL CH340 (COMx)` |

然后四条命令，每条都给了「该看到什么」：

```bash
git clone https://github.com/BSGGPRO/esp32p4-hid-agent.git && cd esp32p4-hid-agent

# 1) 编译 + 烧录（需要 ESP-IDF v5.5.6；COM7 换成你的口）
#    新克隆的仓库没有 build/，先 set-target 再 build；sdkconfig.defaults 和 partitions.csv 都在库里
tools\idf.cmd set-target esp32p4
tools\idf.cmd build
tools\idf.cmd -p COM7 flash
#  → 末行 "Hash of data verified."；高速口插到目标机后设备管理器出现 "P4 HID Bridge"

# 2) 自检：屏幕 / OCR / 板子 / 指针 / 视觉，五项一次看完
python tools/connector.py --selfcheck
#  → {"ok": true, ...}，退出码 0

# 3) 起服务（占串口，提供 TCP 透传 + 网页仪表盘）
python pc/hub.py
#  → http://127.0.0.1:8765 · TCP 127.0.0.1:8766

# 4) 让板子动一下：光标去 (1200,600)，再读屏幕左上角一小块的字
echo '{"steps":[{"op":"point","x":1200,"y":600},{"op":"ocr","rect":[0,0,600,140]}]}' | python tools/connector.py
```

第 1 步的细节（驱动来源、烧录参数、连不上怎么排）在
[docs/bringup-runbook.md](docs/bringup-runbook.md)；PC 侧接口在 [pc/README.md](pc/README.md)。

输出永远是 JSON：`{"ok": true, "steps": [...], "vars": {...}}`。
**任何一步核对不上就整单失败并停手**，不会带着错误继续动键鼠。

---

## 连接器：一个文件，24 个操作

`tools/connector.py` 从 stdin 读作业、打印结果、**不落任何文件**。每个操作都能带 `require`，不满足就中止。

| 类别 | 操作 | 说明 |
|---|---|---|
| 窗口 | `restore` `focus` `title` | 还原 / 置前 / 读标题（以 Win32 为准） |
| 键鼠 | `click` `move` `point` `drag` `drag_path` `hold` `scroll` `key` `type` | `point` 是闭环定位，自己纠偏到目标像素 |
| 观察 | `look` `ocr` `find` `diff` `wait_change` `read_text` | `find` 在区域里找一段文字并返回中心点 |
| 视觉 | `see` `vision` | 可选：本地视觉模型回答「这块区域是什么」，见下 |
| 系统 | `clip` `launch` `wait` | 剪贴板读写 / 启动程序 / 等待 |
| 组合 | `recipe` | `wechat-send`、`open-url` —— 带完整核对的成品动作 |

### 看一眼有多快

视觉模型（Qwen2.5-VL-3B，4bit，约 2.5 GB 显存）跑在**常驻服务**里：模型只加载一次，
之后每次提问只是发一张小图过去。冷启动 22 秒一次，稳态实测：

| 传图尺寸上限 | 单次问答 |
|---|---|
| `longest=480` | **0.72 s** |
| `longest=640` | 1.8 s |
| `longest=1280`（默认） | 3~4 s |

```bash
python tools/visiond.py --start     # 也可以不管：第一次 see 会自动拉起
python tools/visiond.py --status    # 在不在、占多少显存
```

**用 OCR 能回答的问题别问模型。** 文字、按钮名、坐标用 `ocr` / `find`，毫秒级且不会瞎猜；
`see` 只用来回答「这是什么界面」「现在卡在哪个状态」这类模糊问题，而且是裁小图问。


成品动作示例（微信发一条中英混排消息，含标题核对与发后核对）：

```json
{"steps": [
  {"op": "recipe", "name": "wechat-send", "to": "文件传输助手", "pieces": ["py:nihao"]}
]}
```

`py:<拼音>` 走输入法出候选再选字，`raw:<ascii>` 直接敲字母。发前核对输入框内容，发后核对输入框已清空
且出现新气泡；对不上就报错，不算成功。

---

## 图片 → 画出来

把任意图片转成笔画，用板子在画图里一笔笔画出来（图片转笔画是确定性算法，不依赖模型视觉）：

```powershell
python tools\photo-to-draw.py 照片.jpg --quality good --draw   # 或双击 photo-draw.bat
python tools\draw-image.py "inbox\cat.png" --mode line --preview-width 4 --draw
```

实测：猫线稿 26 笔 / 193 顶点 / 约 4 秒，`pearson 0.92`；照片速写 42 笔 / 492 顶点。
细节见 [docs/image-to-draw.md](docs/image-to-draw.md)。1 px 铅笔做不到无损还原，能给钢笔/版画级可辨认画。

---

## 网页仪表盘（中英双语）

`python pc/hub.py` 起在 <http://127.0.0.1:8765>，每秒钟读一次板子统计（运行时长、双核占用、
内存、TF 卡、调频、网络、任务栈）。

右上角语言下拉可切 **中文 / English**，选择记在浏览器里；也可以直接用链接指定：

```
http://127.0.0.1:8765/?lang=en        # 英文
http://127.0.0.1:8765/?lang=zh        # 中文
```

翻译是渲染后的词表替换（`web/i18n.js`），不改面板逻辑；没收录的句子原样显示，不会把界面搞坏。

---

## 已验证状态（真机）

| 部分 | 状态 | 证据 |
|---|---|---|
| 固件 | ✅ 编译 + 烧录 + 真机运行 | `esp32p4_hid_agent.bin` 453,056 B |
| 可复现构建 | ✅ 从零克隆能编译 | `git clone` 到空目录 → `tools\idf.cmd build` 退出码 0，产物同为 453,056 B；生成的 `sdkconfig` 与本地 2749 行逐行一致 |
| USB 枚举 | ✅ 系统认成标准键鼠 | `HID Keyboard Device` + `HID-compliant mouse`，序列号 = 芯片 MAC |
| PC 桥 | ✅ 自检 39/39 | 截屏 2560×1440，仅依赖 Pillow |
| 协议 | ✅ v1.2 | 15 命令 / 7 错误码 / 4 事件 |
| 端到端 | ✅ 通过 | 截屏 → 移鼠标 → 再截屏差分，`E2E OK` |
| 性能 | ✅ 达标 | ping 18.4 ms、mouse_move 12.5 ms、连发 332 条/秒 |
| 指针精度 | ✅ 1 px | 四角标定，最差角误差 1 px |
| 自检 | ✅ 5/5 | `--selfcheck`：屏幕 2560×1440 / OCR / 板子 / 指针 / 视觉 |
| 视觉延迟 | ✅ 0.72 s | 常驻服务，`longest=480`；冷启动 22 s 只付一次 |
| 仪表盘双语 | ✅ 实测 | ⌄ 切 English 后刷新仍英文，`?lang=zh` 切回 |

---

## 接线：两个口都要插

| 口 | 接到 | 作用 |
|---|---|---|
| UART（CH340） | 本机 | 烧录 + 指令通道；换 USB 口会改 COM 号 |
| 高速 Type-C | 目标机 | USB HID 键鼠输出，`VID_303A&PID_4004` |

只插 UART：能收发协议但没有手。只插高速口：是键鼠但没有指令通道。**两个都要。**

---

## 限制与红线

- 一次只能一个透传客户端；独占失败报 `board_busy`。
- 板子是硬件键鼠，**会真的操作这台电脑**：不对着真人或不可逆动作盲点。
- 不移动、不缩放、不最大化用户窗口；只为目标窗口做「还原 + 置前」。
- 独占全屏程序（游戏等）会吞掉按键，此时不投键鼠。
- 中文输入依赖系统输入法；`raw:` 只在输入法组字时按回车提交。

---

## 目录

| 路径 | 内容 |
|---|---|
| `firmware/` | ESP-IDF 固件（纯 ESP-IDF，不用 Arduino） |
| `pc/` | PC 侧桥：`hub.py` 服务、`brain.py` 动作接口、协议实现 |
| `tools/` | `connector.py` 连接器 + 指针标定 + 图片转笔画 |
| `web/` | 板子状态网页仪表盘 |
| `docs/` | 硬件事实、协议、标定、排障、操作手册 |
| `inbox/` | 本地素材与产物（不参与版本库） |

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [docs/field-guide.md](docs/field-guide.md) | 单文件全量手册：架构 / 自检 / 接口 / 排障 |
| [docs/operations.md](docs/operations.md) | 日常操作约定与安全红线 |
| [docs/protocol.md](docs/protocol.md) | 串口/TCP 协议（v1.2，冻结） |
| [docs/hardware-facts.md](docs/hardware-facts.md) | 板子实测：芯片、时序、供电、TF 卡 |
| [docs/pointer-calibration.md](docs/pointer-calibration.md) | 指针标定：1 px 精度的来由 |
| [docs/bringup-runbook.md](docs/bringup-runbook.md) | 从零到跑通的步骤 |
| [docs/brain-usage.md](docs/brain-usage.md) | `brain.py` 接口用法 |
| [docs/image-to-draw.md](docs/image-to-draw.md) | 图片转笔画与绘制 |

---

## License

MIT，见 [LICENSE](LICENSE)。
