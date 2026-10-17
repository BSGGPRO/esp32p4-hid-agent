# esp32p4-hid-agent — firmware

ESP-IDF v5.5.x project for **JC-ESP32P4-M3** (target `esp32p4`) implementing the
firmware side of [`../docs/protocol.md`](../docs/protocol.md) **v1.0.1**:
a UART0 JSON-lines command interface that drives a composite TinyUSB HID
keyboard + mouse.

| Item | Value |
|---|---|
| Target | `esp32p4` |
| IDF | v5.5.x (`idf.py set-target esp32p4 && idf.py build`) |
| USB class | HID, 1 interface, report ID 1 = keyboard, 2 = mouse |
| VID / PID | `0x303A` / `0x4004` (protocol.md §5 v1.0.1 ruling) |
| EP | IN `0x81`, 16 bytes, `bInterval 4` (HS ⇒ 1 ms/frame, FS ⇒ 4 ms) |
| Protocol link | UART0 GPIO37 TX / GPIO38 RX, 115200 8N1, no flow control |
| Logging | `ESP_LOG_NONE` by default; `CONFIG_HID_AGENT_RX_DEBUG=y` turns it on |

## Layout

```
firmware/
├── CMakeLists.txt              project + ESP-IDF include
├── sdkconfig.defaults          silent logs, 1 kHz tick, TinyUSB HID/VID/PID
└── main/
    ├── CMakeLists.txt
    ├── idf_component.yml       espressif/esp_tinyusb ^2.0.0
    ├── Kconfig.projbuild       HID_AGENT_RX_DEBUG
    ├── app_main.c              UART0 raw line reader (no VFS console)
    ├── uart_link.c/.h          UART driver init, framed TX, uptime ms
    ├── keymap.c/.h             protocol.md §4 key table + US-layout text map
    ├── hid_device.c/.h         descriptors, TinyUSB install, HID state machine
    └── cmd_protocol.c/.h       §3 commands, §3.4 errors, §3.5 events, §6 safety
```

## Static self-check: every protocol.md §3 command → implementing function

| §3 cmd | Request fields | Handler | Queued? | Reply emitted by |
|---|---|---|---|---|
| `ping` | `id` | `cmd_ping()` | no (inline) | `cmd_ping()` |
| `status` | `id` | `cmd_status()` | no (inline) | `cmd_status()` |
| `keepalive` | `id?` | `cmd_keepalive()` | no (inline) | `cmd_keepalive()` |
| `set` | `key`,`value` | `cmd_set()` | no (inline) | `cmd_set()` |
| `key` | `keys[]` | `cmd_key_action()` → `JOB_KEY_PRESS` | yes | `job_reply_ok()` |
| `key_down` | `keys[]` | `cmd_key_action()` → `JOB_KEY_PRESS` | yes | `job_reply_ok()` |
| `key_up` | `keys[]` | `cmd_key_action()` → `JOB_KEY_RELEASE` | yes | `job_reply_ok()` |
| `key_tap` | `keys[]`,`hold_ms?` | `cmd_key_tap()` → `JOB_KEY_TAP` | yes | `job_reply_ok()` |
| `release_all` | — | `cmd_release_all()` | no (inline, §9.3) | `cmd_release_all()` |
| `text` | `text`,`cps?` | `cmd_text()` → `JOB_TEXT` | yes | `job_reply_ok()` |
| `text_tap` | same as `text` | `cmd_text()` (same branch) | yes | `job_reply_ok()` |
| `mouse_move` | `dx`,`dy` | `cmd_mouse_move()` → `JOB_MOUSE_MOVE` | yes | `job_reply_ok()` |
| `mouse_btn` | `btn`,`action`,`click_ms?` | `cmd_mouse_btn()` → `JOB_MOUSE_BTN` | yes | `job_reply_ok()` |
| `mouse_scroll` | `dy` | `cmd_mouse_scroll()` → `JOB_MOUSE_SCROLL` | yes | `job_reply_ok()` |
| `mouse_click` | `btn?`,`count?`,`interval_ms?` | `cmd_mouse_click()` → `JOB_MOUSE_CLICK` | yes | `job_reply_ok()` |
| — (unknown) | | `dispatch()` fallback | — | `reply_err(..., "unknown_cmd")` |

Supporting pieces:

| Contract | Implementation |
|---|---|
| §3.4 `bad_json` | `proto_handle_line()` (non-`{...}` line, unparsable JSON, missing `cmd`) |
| §3.4 `unknown_cmd` | `dispatch()` fallback |
| §3.4 `bad_arg` | `json_read_int()`, `parse_key_set()`, per-command range checks |
| §3.4 `bad_key` | `parse_key_set()` + `reply_key_parse_error()` (`detail` = first illegal name) |
| §3.4 `unsupported_char` | `cmd_text()` loop (`detail` = 0-based index, §9.2) |
| §3.4 `not_mounted` | `hid_device_is_mounted()` guard in every reporting command (§9.3) |
| §3.4 `rate_limited` | `rate_window_add()` / `rate_limited()` (>200 frames/s) |
| §3.5 `hid_mounted` / `hid_unmounted` | `hid_device_service()` → `proto_event_hid_mounted/unmounted()` |
| §3.5 `safety_release` | `monitor_task()` → `event_send_safety_release()` |
| §3.5 `boot` | `proto_event_boot()` (called from `app_main()`) |
| §4 key table | `keymap.c` (`s_modifiers[]`, `s_keys[]`, `keymap_char_to_key()`) |
| §5 descriptors | `hid_device.c` (`hid_report_descriptor[]`, `hid_configuration_descriptor[]`) |
| §6.1 safety release | `monitor_task()` (25 ms poll of `uart_link_now_ms() - s_last_rx_ms`) |
| §6.2 detach clears state | `hid_device_service()` → `hid_device_clear_state()` |
| §6.3 power-on release | `hid_device_init()` and on attach in `hid_device_service()` |
| §6.5 abort on `release_all` | `s_seq_abort` + `hid_device_kbd_tap()`/`run_job()` checks |
| §9.1 640+ byte line buffer | `UART_LINK_LINE_MAX 768`, drop-whole-line in `rx_task()` |
| §9.4 id echo / set range | `json_read_id()`, `cmd_set()` (0–60000 ms) |

## Design notes

* **Threading.** The RX task parses frames and executes control commands inline
  (`ping`/`status`/`keepalive`/`set`/`release_all`), so `release_all` is never
  queued behind a long action. Action commands go through a 16-slot FIFO to the
  `hid_seq` worker, which keeps the RX task responsive while a `text` burst or a
  split `mouse_move` is in flight. A 25 ms `hid_mon` task performs mount edge
  detection and the §6.1 safety release. HID reports are serialised by one mutex,
  UART writes by another.
* **Mount/unmount detection (§9.5).** esp_tinyusb v2 owns `tud_mount_cb()` /
  `tud_umount_cb()` (strong symbols; defining them in the app is a link error),
  and `event_cb` differs across 2.0.x/2.4.x. The firmware therefore polls the
  frozen predicate `tud_mounted()` every 25 ms: false→true re-sends the all-zero
  report and emits `hid_mounted`, true→false clears local state and emits
  `hid_unmounted`.
* **Never lose a release.** Reports are sent only after `tud_hid_ready()` becomes
  true (bounded wait + retries), and `hid_device_release_all()` clears the local
  state *before* sending, so the firmware state is released even if the host
  never collects the report.
* **`kbd_keys`** in `status` and in every action reply is
  `popcount(kbd_modifier) + occupied key slots` (`kbd_keys_reported()`), the
  metric the PC-side selftest asserts on. A modifier-only hold therefore reports
  `1`, and a `ctrl+c` reports `2`.
* **`rx_ok` / `rx_bad`** (returned by `status`): `rx_ok` counts frames parsed and
  dispatched (including frames answered with an `err`), `rx_bad` counts dropped
  lines (not `{...}`, unparsable JSON, missing `cmd`, over-long line). Frames
  discarded by the rate limiter are counted in neither.
* **`mouse_move` splitting** uses `frames = max(ceil(|dx|/127), ceil(|dy|/127))`
  with both axes clamped to ±127 per report; `dy > 0` is down and `dx > 0` is
  right, never inverted (protocol.md §5).
* **`text` hold/period**: `hold = max(1, (1000/cps)/2)` ms, then the remainder of
  the character period is delayed. A character that no longer fits the 6 key
  slots replies `bad_arg`.
* **Parser hardening**: unknown request fields are ignored (forward compatible),
  `id` must be an integer (otherwise `bad_arg` is answered without an `id`), and
  `keys` arrays are limited to 6 distinct non-modifier keys.
* **`key_tap` "全部抬起" interpretation**: only the keys named by that `key_tap`
  are released after `hold_ms` (i.e. the whole combo is lifted). Keys that were
  already held by an earlier `key`/`key_down` are left untouched, so `key_tap`
  never clobbers unrelated held state; `release_all` remains the only command
  that clears everything. No response field depends on this distinction.

## High-speed port / VID-PID / flash (evidence)

* **No `TINYUSB_RHPORT_HS` symbol exists in esp_tinyusb v2.** That Kconfig
  choice was removed in 2.0.0: `esp_tinyusb/CHANGELOG.md`, section *2.0.0 →
  Breaking changes → Removed* lists `TINYUSB_RHPORT`, `TINYUSB_RHPORT_HS`,
  `TINYUSB_RHPORT_FS` and replaces them with the run-time field
  `tinyusb_config_t::port`.
* **`TINYUSB_DEFAULT_CONFIG()` already selects the high-speed port on P4.**
  `esp_tinyusb/include/tinyusb_default_config.h`:
  `#if CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31` →
  `#define TINYUSB_CONFIG_NO_ARG() TINYUSB_CONFIG_HIGH_SPEED(NULL, NULL)`, and
  `TINYUSB_CONFIG_HIGH_SPEED()` sets `.port = TINYUSB_PORT_DEFAULT_HS`, which is
  `TINYUSB_PORT_HIGH_SPEED_0` when `SOC_USB_OTG_PERIPH_NUM > 1` (ESP32-P4).
  `tinyusb.c::tinyusb_driver_install()` then configures the UTMI PHY at
  `USB_PHY_SPEED_HIGH` for that port. No extra sdkconfig option is therefore
  needed (and none exists) — the default is the HS port on the dedicated
  USB_DM/USB_DP pins.
  * Re-check after the component is fetched:
    `grep -c TINYUSB_RHPORT managed_components/espressif__esp_tinyusb/Kconfig`
    must be 0 (recorded in the build log section below).
* **VID/PID** stay at the component defaults: `CONFIG_TINYUSB_DESC_USE_ESPRESSIF_VID=y`
  → VID `0x303A` (`tinyusb.h: TINYUSB_ESPRESSIF_VID`), and
  `CONFIG_TINYUSB_DESC_USE_DEFAULT_PID=y` → PID
  `0x4000 | (CFG_TUD_HID << 2)` = `0x4004` (`usb_descriptors.c: USB_TUSB_PID`).
* **Flash**: 16 MB, QIO, 80 MHz, console UART baud 115200 in `sdkconfig.defaults`
  (board facts for JC-ESP32P4-M3). Default single-app partition table is used;
  the image is a few hundred KB (see size report).

## Build

```powershell
# ESP-IDF v5.5.x environment (IDF_PATH / toolchain) must be exported first
idf.py set-target esp32p4
idf.py build
```

Build evidence (final `idf.py size` report, bin paths) is appended below after the
first successful toolchain build.

### Build result (verified)

Environment: `IDF_PATH=D:\esp\idf-full\esp-idf-v5.5.6`, `IDF_TOOLS_PATH=D:\esp\.espressif`,
`IDF_PYTHON_ENV_PATH=D:\esp\python_env`, `ESP-IDF v5.5.6-dirty`,
riscv32-esp-elf gcc 14.2.0. Managed components resolved:
`espressif/esp_tinyusb 2.4.0`, `espressif/tinyusb 0.21.0~2`.

`idf.py set-target esp32p4 && idf.py build` → **0 errors, 0 warnings**,
`Project build complete`.

```
esp32p4_hid_agent.bin binary size 0x508b0 bytes (329,904 B).
Smallest app partition is 0x100000 bytes. 0xaf750 bytes (69%) free.
```

`idf.py size` (total image 329,542 B):

| Memory type | Used [B] | Used [%] | Remain [B] | Total [B] |
|---|---|---|---|---|
| Flash | 251,354 | — | — | — |
|   .text | 193,162 | | | |
|   .rodata | 57,700 | | | |
|   .appdesc | 256 | | | |
|   .init_array | 236 | | | |
| DIRAM | 85,894 | 13.11 | 569,466 | 655,360 |
|   .text (DIRAM) | 69,162 | 10.55 | | |
|   .data | 8,724 | 1.33 | | |
|   .bss | 8,008 | 1.22 | | |
| HP core RAM | 538 | 6.57 | 7,654 | 8,192 |
| LP RAM | 24 | 0.07 | 32,744 | 32,768 |

`idf.py size-components` → this application's own code (`libmain.a`):
**14,365 B total** (12,080 B flash `.text`, 1,437 B `.rodata`, 848 B DIRAM).

Artifacts (`build/`):

| File | Offset | Size |
|---|---|---|
| `build/bootloader/bootloader.bin` | `0x2000` | — |
| `build/partition_table/partition-table.bin` | `0x8000` | — |
| `build/esp32p4_hid_agent.bin` | `0x10000` | 329,904 B |

`build/flash_args`: `--flash_mode dio --flash_freq 80m --flash_size 16MB`.
(`--flash_mode dio` while `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y` is expected: upstream
`components/esptool_py/Kconfig.projbuild` maps both QIO and QOUT to the header
string `"dio"`; the real mode comes from the flash chip/efuse.)

Resolved sdkconfig spot-check: `CONFIG_TINYUSB_HID_COUNT=1`,
`CONFIG_TINYUSB_DESC_USE_ESPRESSIF_VID=y`, `CONFIG_TINYUSB_DESC_USE_DEFAULT_PID=y`,
`CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`, `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`,
`CONFIG_ESPTOOLPY_FLASHFREQ_80M=y`, `CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200`,
`CONFIG_FREERTOS_HZ=1000`, `CONFIG_LOG_DEFAULT_LEVEL_NONE=y`,
`CONFIG_LOG_MAXIMUM_LEVEL_INFO=y`, `CONFIG_PARTITION_TABLE_SINGLE_APP=y`.

`grep -c TINYUSB_RHPORT managed_components/espressif__esp_tinyusb/Kconfig` → **0**
(confirms the v2 claim above: the HS port is selected by
`TINYUSB_DEFAULT_CONFIG()`, there is no Kconfig symbol to set).