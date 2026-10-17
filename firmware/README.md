# esp32p4-hid-agent — firmware

ESP-IDF v5.5.x project for **JC-ESP32P4-M3** (target `esp32p4`) implementing the
firmware side of [`../docs/protocol.md`](../docs/protocol.md) **v1.2**:
a UART0 JSON-lines command interface that drives a composite TinyUSB HID
keyboard + mouse, plus live `stats` telemetry (`stats`, §3.6), TF-card
verification (`sd_test`, §3.7) and an on-die temperature sensor (§10.4).

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
├── partitions.csv              16 MB: nvs 24K, phy_init 4K, factory app 3 MB
├── sdkconfig.defaults          silent logs, 1 kHz tick, TinyUSB HID/VID/PID,
│                               16 MB flash, PSRAM, PM/DFS, custom partitions
└── main/
    ├── CMakeLists.txt
    ├── idf_component.yml       espressif/esp_tinyusb ^2.0.0
    ├── Kconfig.projbuild       HID_AGENT_RX_DEBUG
    ├── app_main.c              UART0 raw line reader (no VFS console), PM/DFS
    ├── uart_link.c/.h          UART driver init, framed TX, uptime ms, tx counter
    ├── keymap.c/.h             protocol.md §4 key table + US-layout text map
    ├── hid_device.c/.h         descriptors, TinyUSB install, HID state machine
    ├── cmd_protocol.c/.h       §3 commands (+ stats/sd_test), errors, events, safety
    ├── stats.c/.h              §3.6 stats payload (cpu/mem/temp/tasks/sd/net/uart)
    ├── sdcard.c/.h             §3.7 TF card: LDO power, probe ladder, FAT mount,
    │                           sd_test read/write/verify
    └── thermal.c/.h            §10.4 on-die temperature sensor + thresholds
```

## Static self-check: every protocol.md §3 command → implementing function

| §3 cmd | Request fields | Handler | Queued? | Reply emitted by |
|---|---|---|---|---|
| `ping` | `id` | `cmd_ping()` | no (inline) | `cmd_ping()` |
| `status` | `id` | `cmd_status()` | no (inline) | `cmd_status()` |
| `keepalive` | `id?` | `cmd_keepalive()` | no (inline) | `cmd_keepalive()` |
| `set` | `key`,`value` | `cmd_set()` | no (inline) | `cmd_set()` |
| `stats` | `id` | `cmd_stats()` → `stats_build()` | no (inline) | `cmd_stats()` |
| `sd_test` | `id`,`bytes?` | `cmd_sd_test()` → `sd_test` task → `sdcard_run_test()` | own task | `reply_sd_test()` |
| `sd_remount` | `id` | 卸载并重新探测/挂载 TF 卡，返回完整 `sd` 状态 |
| `sd_format` | `id`, `confirm:"ERASE"`, `fs`?`"fat32"` | **破坏性** FAT32 格式化 + 重挂载 + 立即 1MB `sd_test` |
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
esp32p4_hid_agent.bin binary size 0x6d170 bytes (446,832 B).
Smallest app partition is 0x300000 bytes. 0x292e90 bytes (86%) free.
```

`idf.py size` (real output for the v0.2 build, full log in
`docs/evidence/fw-build-size.txt`):

| Memory type | Used [B] | Used [%] | Remain [B] | Total [B] |
|---|---|---|---|---|
| Flash | 353,806 | — | — | — |
|   .text | 277,254 | | | |
|   .rodata | 76,028 | | | |
|   .init_array | 268 | | | |
|   .appdesc | 256 | | | |
| DIRAM | 102,646 | 17.81 | 473,818 | 576,464 |
|   .text (DIRAM) | 80,710 | 14.00 | | |
|   .data | 12,072 | 2.09 | | |
|   .bss | 9,864 | 1.71 | | |
| HP core RAM | 122 | 1.49 | 8,070 | 8,192 |
|   .text | 62 | 0.76 | | |
|   .data | 60 | 0.73 | | |
| LP RAM | 24 | 0.07 | 32,744 | 32,768 |
|   .rtc_reserved | 24 | 0.07 | | |

Total image size: **446,442 B** (`.bin` padded to 446,832 B).

Artifacts (`build/`):

| File | Offset | Size |
|---|---|---|
| `build/bootloader/bootloader.bin` | `0x2000` | 16,064 B |
| `build/partition_table/partition-table.bin` | `0x8000` | 3,072 B |
| `build/esp32p4_hid_agent.bin` | `0x10000` | 446,832 B |

`build/flash_args`: `--flash_mode dio --flash_freq 80m --flash_size 16MB`.

Resolved sdkconfig spot-check: `CONFIG_TINYUSB_HID_COUNT=1`,
`CONFIG_TINYUSB_DESC_USE_ESPRESSIF_VID=y`, `CONFIG_TINYUSB_DESC_USE_DEFAULT_PID=y`,
`CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`, `CONFIG_ESPTOOLPY_FLASHMODE_QIO=y`,
`CONFIG_ESPTOOLPY_FLASHFREQ_80M=y`, `CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200`,
`CONFIG_FREERTOS_HZ=1000`, `CONFIG_LOG_DEFAULT_LEVEL_NONE=y`,
`CONFIG_LOG_MAXIMUM_LEVEL_INFO=y`, `CONFIG_PARTITION_TABLE_CUSTOM=y` +
`CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"`,
`CONFIG_SPIRAM=y` / `CONFIG_SPIRAM_MODE_HEX=y` / `CONFIG_SPIRAM_SPEED_200M=y`,
`CONFIG_FREERTOS_USE_TRACE_FACILITY=y`, `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y`,
`CONFIG_PM_ENABLE=y`, `CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`.

`grep -c TINYUSB_RHPORT managed_components/espressif__esp_tinyusb/Kconfig` → **0**
(confirms the v2 claim above: the HS port is selected by
`TINYUSB_DEFAULT_CONFIG()`, there is no Kconfig symbol to set).

## v0.2 additions: `stats`, TF card, temperature / passive cooling

### `stats` (§3.6)

`stats_build()` lives in `stats.c`; every field is read live at call time.
Two details worth knowing:

* **CPU %** is window based: `stats_init()` seeds the idle-task run-time counters
  at boot, so the first call already reports a meaningful window; later calls
  report the window since the previous call (`window_ms` says which).
* `heap_*` are all **internal RAM** (`MALLOC_CAP_INTERNAL`). The global
  `esp_get_minimum_free_heap_size()` would include the 32 MB PSRAM heap and
  report ~32 MB as a "low water mark", which is useless — hence the
  `heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL)` fix.
* `cpu.mhz` is the **instantaneous** clock (`esp_clk_tree_src_get_freq_hz`).
  With DFS enabled it reads 360 while a command is being served (the PM
  framework ramps up for any running task) and drops to 40 while idle.

### TF card (§3.7) — `sdcard.c`

Board wiring (verified against the vendor BSP in `1-Demo/`, which matches the
P4 slot-0 default IOMUX, so no GPIO-matrix remap is needed):

| Signal | GPIO | | Signal | GPIO |
|---|---|---|---|---|
| CLK | 43 | | D1 | 40 |
| CMD | 44 | | D2 | 41 |
| D0 | 39 | | D3 | 42 |

Bring-up sequence (`bring_up()`):

1. `sd_pwr_ctrl_new_on_chip_ldo({.ldo_chan_id = 4})` — **the card's IO rail is
   powered by the P4 on-chip LDO channel 4**. Without this the card never answers
   CMD0 and the probe reports `card_init:ESP_ERR_TIMEOUT` (that was the original
   bug: identical symptom, wrong root cause).
2. 100 ms power-on delay.
3. Probe ladder, first success wins and is recorded in `stats.sd.tries`:
   `1bit/400kHz` → `4bit/20MHz` → `1bit/20MHz` (each `sdmmc_card_init` with a
   fresh host; the host is deinit'ed before the mount because
   `esp_vfs_fat_sdmmc_mount()` brings it up itself).
4. Raw boot-sector inspection decides `sd.fs`; exFAT is **reported and never
   formatted** (`err:"exfat_not_supported"`) because IDF FatFs has no exFAT
   driver. A FAT card is mounted at `/sdcard` with
   `format_if_mount_failed=false`, preferring the 4-bit/20 MHz bus and falling
   back to the probing bus.
5. `sd.cd` is always `null`: this socket has no independent card-detect switch
   (microSD pin 2 is CD/DATA3, shared with the 4-bit data line).

`sd_test` runs on a private 6 KB task (`s_sd_test_busy` guard, one at a time),
writes the pattern `(i*31+7) & 0xFF`, reads it back, verifies, and caches the
speeds into `stats.sd.speed_*`. `bytes` is clamped to 1 KB … 16 MB.

**Real card on the bench: 64 GB SanDisk-class exFAT (`SN64G`, `SDXC`,
`capacity_mb` 60 906) → detected and reported, not mounted** — the electrical
path is fixed, an exFAT-capable FS is the only remaining gap (see
`docs/evidence/` and the hand-off note).

### Temperature / passive cooling (§10.4) — `thermal.c`

* Internal P4 temperature sensor, 20–100 °C window (±2 °C), read once per
  `stats` call → `temp_c` (1 decimal) and `temp_warn`.
* **No fixed 240 MHz on this silicon**: ESP32-P4 rev v1.3 (`ESP32P4_SELECTS_REV_LESS_V3`)
  only offers 40 or 360 MHz fixed (`components/esp_system/port/soc/esp32p4/Kconfig.cpu`).
* Heat reduction therefore uses **esp_pm DFS**: `esp_pm_configure(max=360,
  min=40, light_sleep=false)`; idle → 40 MHz, command → 360 MHz.
  UART0 is clocked from `UART_SCLK_DEFAULT = PLL_F80M` (fixed 80 MHz PLL,
  independent of the CPU clock), so 115200 baud cannot be disturbed; the SDMMC
  driver takes an `ESP_PM_APB_FREQ_MAX` lock around its transactions.
  Light sleep stays off because USB HID must remain enumerated.
* Overheat policy: `temp_c ≥ 70` sets `temp_warn`; `≥ 80` makes the RX task
  insert a 10 ms delay between frames (a command storm can no longer hold the
  CPU at 360 MHz), released below 77 °C. Thresholds are runtime-configurable via
  `{"cmd":"set","key":"temp_warn_c","value":70}` / `"temp_throttle_c"`.
* Evidence: `docs/evidence/fw-serial-verify.txt` (temp fields + `sd.tries`),
  `docs/evidence/fw-soak-*.txt` (10 min mixed workload: temp, heap, stack HWM,
  ping/mouse latency).
## v0.2 additions: PM/DFS runtime governor (protocol.md §10.4)

- **Boot default: fixed 360 MHz.** `pm_ctl_init()` configures
  `esp_pm_configure(max=360, min=360, light_sleep=false)`.
- **Runtime switch:** `{"cmd":"set","key":"dfs","value":1}` enables the
  `adaptive` governor, `value:0` returns to `fixed360`. Not persisted (a reset
  returns to the safe default on purpose).
- **adaptive governor:** boost via `esp_pm_lock_acquire(ESP_PM_CPU_FREQ_MAX)` on
  any UART frame / HID report / SD transfer, release after 500 ms of quiet
  (100 ms above 70 °C), and hold the lock for the whole SD operation so the
  clock never changes mid-transfer.
- **Why:** plain DFS (min=40) caused `rst:0x7 (HP_SYS_HP_WDT_RESET)` on long
  SDMMC writes and periodic HID re-enumeration on this early P4 silicon
  (rev v1.3). Adaptive passes 15/15 `sd_test`, identical ping/mouse latency;
  fixed360 passes 10/10. Evidence: `docs/evidence/fw-governor.txt`,
  `fw-bisect-nopm.txt`, `fw-pm-toggle.txt`.
- **stats.pm:** `{governor, dfs, min_mhz, max_mhz, cur_mhz, idle_ms, boosts,
  downs, last_reason}` — additive field, safe for older clients.

Latest build: `esp32p4_hid_agent.bin` = 452,640 B (0x6e820), app partition 86% free; `idf.py build` 0 errors / 0 warnings.
