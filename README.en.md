# esp32p4-hid-agent

[中文](README.md) | **English**

Turn an **ESP32-P4 dev board** into a **standard USB keyboard + mouse** for a Windows machine,
then drive it with a **JSON connector**: screenshot → decide → move the mouse / type → screenshot again to verify.
Hardware-level input, no driver, no software injection.

```
   ┌──────────────┐   USB HID (keyboard+mouse)  ┌──────────────┐
   │  ESP32-P4    │ ──────────────────────────▶ │   Windows    │
   └──────┬───────┘                             └──────▲───────┘
          │ UART 115200 / TCP passthrough             │ screenshot
   ┌──────▼───────────────────────────────────────────┴───────┐
   │ tools/connector.py — reads one JSON job, plays it on the  │
   │ board, prints the result                                  │
   └───────────────────────────────────────────────────────────┘
```

---

## From zero on a fresh machine

Two prerequisites, once each:

| Prerequisite | How | Passes when |
|---|---|---|
| Python 3.12 + Pillow | `python -m pip install -r pc/requirements.txt` | `python -c "import PIL; print(PIL.__version__)"` prints a version |
| CH340 serial driver | run `tools\install-ch340-admin.cmd` as admin (or let Windows install it) | Device Manager shows `USB-SERIAL CH340 (COMx)` under Ports |

Then four commands, each with what you should see:

```bash
git clone https://github.com/BSGGPRO/esp32p4-hid-agent.git && cd esp32p4-hid-agent

# 1) Build + flash (needs ESP-IDF v5.5.6; replace COM7 with your port)
#    a fresh clone has no build/; sdkconfig.defaults and partitions.csv are tracked
tools\idf.cmd set-target esp32p4
tools\idf.cmd build
tools\idf.cmd -p COM7 flash
#  → last line "Hash of data verified."; "P4 HID Bridge" appears once the HS port is plugged in

# 2) Self-check: screen / OCR / board / pointer / vision in one shot
python tools/connector.py --selfcheck
#  → {"ok": true, ...}, exit code 0

# 3) Start the service (owns the serial port, serves TCP passthrough + dashboard)
python pc/hub.py
#  → http://127.0.0.1:8765 · TCP 127.0.0.1:8766

# 4) Make the board do something: pointer to (1200,600), then read a small strip top-left
echo '{"steps":[{"op":"point","x":1200,"y":600},{"op":"ocr","rect":[0,0,600,140]}]}' | python tools/connector.py
```

Flashing details (driver source, parameters, recovery) are in
[docs/bringup-runbook.md](docs/bringup-runbook.md) (Chinese); the PC side is in
[pc/README.md](pc/README.md).

Output is always JSON: `{"ok": true, "steps": [...], "vars": {...}}`.
**If any step fails to verify, the whole job aborts** — it never keeps pressing keys after a mismatch.

---

## The connector: one file, 24 operations

`tools/connector.py` reads a job from stdin, prints the result, and **writes no files**.
Every operation accepts `require`; when it does not match, the job stops.

| Group | Operations | Notes |
|---|---|---|
| Window | `restore` `focus` `title` | Un-minimize / bring to front / read the title (Win32 facts, not a model) |
| Input | `click` `move` `point` `drag` `drag_path` `hold` `scroll` `key` `type` | `point` is closed-loop: it corrects itself onto the target pixel |
| Observe | `look` `ocr` `find` `diff` `wait_change` `read_text` | `find` locates a line of text in a region and returns its centre |
| Vision | `see` `vision` | Optional local VLM answering "what is this area", see below |
| System | `clip` `launch` `wait` | Clipboard read/write, start a program, sleep |
| Recipes | `recipe` | `wechat-send`, `open-url` — finished actions that verify themselves |

### How fast is "take a look"

The vision model (Qwen2.5-VL-3B, 4-bit, ~2.5 GB VRAM) runs in a **resident service**:
the model loads once, and every question afterwards just ships a small crop.

Cold start costs ~25 s once (both engines load in parallel); steady state, measured on this
machine while idle:

| Action | Time |
|---|---|
| `ocr` small crop (~5 lines) | **0.12 s** (in-service) / 0.62 s including process start |
| `ocr` wide crop (~34 lines) | 1.4 s |
| `see` `speed=fast` (side 480) | 1.2 s |
| `see` default (side 640) | 1.7 s |
| `see` `speed=fine` (side 1280) | 3–4 s |
| `--selfcheck` (5 checks) | **1.1 s** |

> With the machine busy these roughly double. The relative comparison still holds.

```bash
python tools/visiond.py --start     # optional: the first `see`/`ocr` starts it automatically
python tools/visiond.py --status    # up? OCR and vision ready? how much VRAM
```

Two traps found by measuring, both documented in the code comments:

- **RapidOCR defaults to "scale the short side to 736"**: a 208×50 crop gets upscaled 15× before
  detection (625 ms measured). Switching to "cap the long side at 1280" (`det_limit_type=max`)
  makes the same crop **64 ms** with identical text.
- **onnxruntime grabs every core by default**: on this 16-core box that is 30% *slower* than 8
  threads, so the engine gets `cpu_count() // 2`.

**Do not ask the model what OCR can answer.** Text, button labels and coordinates go through
`ocr` / `find` — milliseconds, and they do not guess. `see` is for fuzzy questions like
"which screen is this" or "what is it stuck on", and it gets a small crop, not the whole desktop.

### It gives the VRAM back (so gaming stays smooth)

The resident vision model holds 2.5 GB of VRAM — a real slice of an 8 GB laptop GPU while you
game. So it **unloads itself after 3 idle minutes** (the process and the OCR engine stay; OCR is
CPU-only), and the next `see` / `check` loads it again (~25 s, once).

```bash
python tools/visiond.py --unload     # free VRAM now (run this before a game)
python tools/visiond.py --stop       # stop the process too (OCR dies with it)
python tools/visiond.py --status     # is the model currently resident?
```

Measured back to back: `nvidia-smi` whole-card usage **3780 MiB → 1171 MiB** (2609 MB freed).
On compute: idle it runs nothing, so it does not cost frames; frames only drop if a job asks
the model something *while you are playing* — which now only happens when you ask for it.

---

## Image → drawing

Turn any image into strokes and draw it in Paint with the board, one line at a time
(the image-to-strokes pass is a deterministic algorithm, no model vision involved):

```powershell
python tools\photo-to-draw.py photo.jpg --quality good --draw   # or double-click photo-draw.bat
python tools\draw-image.py "inbox\cat.png" --mode line --preview-width 4 --draw
```

Measured: a cat line drawing is 26 strokes / 193 vertices / about 4 s, `pearson 0.92`;
a photo sketch is 42 strokes / 492 vertices.
Details in [docs/image-to-draw.md](docs/image-to-draw.md) (Chinese).
A 1 px pencil cannot reproduce a photo losslessly — what you get is a recognisable pen/engraving look.

---

## Web dashboard (Chinese / English)

`python pc/hub.py` serves <http://127.0.0.1:8765>, polling board stats once per second
(uptime, dual-core load, heap, TF card, DFS, network, task stacks).

The language dropdown in the top-right switches **中文 / English** and remembers the choice;
you can also pin it by URL:

```
http://127.0.0.1:8765/?lang=en
http://127.0.0.1:8765/?lang=zh
```

Translation is a post-render dictionary swap (`web/i18n.js`) and does not touch the panel logic;
anything not in the table is left as-is, so a missing entry never breaks the page.

---

## Verified on real hardware

| Part | Status | Evidence |
|---|---|---|
| Firmware | ✅ builds, flashes, runs | `esp32p4_hid_agent.bin` 453,056 B |
| Reproducible build | ✅ builds from a fresh clone | clone into an empty dir → `tools\idf.cmd build` exits 0, same 453,056 B; the generated `sdkconfig` matches the local one line for line (2749 lines) |
| USB enumeration | ✅ shows up as standard HID | `HID Keyboard Device` + `HID-compliant mouse`, serial = chip MAC |
| PC bridge | ✅ self-test 39/39 | 2560×1440 capture, Pillow only |
| Protocol | ✅ v1.2 | 15 commands / 7 error codes / 4 events |
| End to end | ✅ passes | screenshot → move → screenshot → diff, `E2E OK` |
| Performance | ✅ | ping 18.4 ms, mouse_move 12.5 ms, 332 messages/s burst |
| Pointer accuracy | ✅ 1 px | four-corner calibration, worst error 1 px |
| Self-check | ✅ 1.1 s, 5/5 | `--selfcheck`: screen 2560×1440 / OCR / board / pointer / vision |
| Vision latency | ✅ 1.2 s / 1.7 s | `speed=fast` / default; the ~25 s cold start is paid once |
| OCR latency | ✅ 0.12 s | small crop, OCR engine resident (was 2.7 s per job) |
| Dashboard i18n | ✅ tested | switched to English, still English after reload; `?lang=zh` switches back |

---

## Wiring: both ports are required

| Port | Goes to | Purpose |
|---|---|---|
| UART (CH340) | this PC | flashing + command channel; changing USB port changes the COM number |
| High-speed Type-C | target machine | USB HID output, `VID_303A&PID_4004` |

UART only: protocol works but there are no hands. High-speed only: it is a keyboard but unreachable.
**Both.**

---

## Limits and red lines

- One passthrough client at a time; a second one fails with `board_busy`.
- The board is a real keyboard and mouse: **it will actually operate the machine**. Never blind-click
  into irreversible actions or at a human.
- Never moves, resizes or maximises the user's windows — only "restore + bring to front".
- Exclusive-fullscreen programs (games) swallow injected keys; no input is sent while they are foreground.
- Chinese input goes through the system IME; `raw:` only presses Enter when a candidate bar is present.

---

## Layout

| Path | Contents |
|---|---|
| `firmware/` | ESP-IDF firmware (plain ESP-IDF, no Arduino) |
| `pc/` | PC bridge: `hub.py` service, `brain.py` action API, protocol implementation |
| `tools/` | `connector.py` plus pointer calibration and image→strokes tools |
| `web/` | Board status dashboard (bilingual) |
| `docs/` | Hardware facts, protocol, calibration, troubleshooting, operations |
| `inbox/` | Local assets and output (not tracked) |

---

## Docs

| Document | What it covers |
|---|---|
| [docs/field-guide.md](docs/field-guide.md) | Single-file manual: architecture / self-check / interfaces / troubleshooting (Chinese) |
| [docs/operations.md](docs/operations.md) | Day-to-day conventions and safety red lines (Chinese) |
| [docs/protocol.md](docs/protocol.md) | Serial/TCP protocol (v1.2, frozen) |
| [docs/hardware-facts.md](docs/hardware-facts.md) | Measured board facts: chip, timing, power, TF card |
| [docs/pointer-calibration.md](docs/pointer-calibration.md) | Where the 1 px accuracy comes from |
| [docs/bringup-runbook.md](docs/bringup-runbook.md) | From zero to a working board |
| [docs/brain-usage.md](docs/brain-usage.md) | `brain.py` API usage |
| [docs/image-to-draw.md](docs/image-to-draw.md) | Image → strokes → drawing |

---

## License

MIT, see [LICENSE](LICENSE).
