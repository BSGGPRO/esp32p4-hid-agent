/*
 * cmd_protocol.h - JSON-lines command protocol (docs/protocol.md v1.0.1).
 *
 * Threading model:
 *   - proto_handle_line() runs in the UART RX task only.
 *   - Control commands (ping/status/keepalive/set/release_all) execute inline in
 *     the RX task so that release_all is always immediate (protocol.md §6.5).
 *   - Action commands (key, key_down, key_up, key_tap, text, text_tap, mouse_*)
 *     are queued and executed by the
 *     sequence worker task, so the RX task keeps parsing while a long text() or
 *     a split mouse_move() is in flight.
 *   - The monitor task polls tud_mounted() (protocol.md §9.5) and implements the
 *     safety release of protocol.md §6.1.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/** Create the command queue and start the worker/monitor tasks. */
void proto_init(void);

/** Handle one complete protocol line (without the trailing '\n'). */
void proto_handle_line(const char *line, size_t len);

/**
 * @brief Account for a line that was dropped as a whole (protocol.md §9.1).
 *
 * Refreshes the keep-alive timestamp, counts it as bad and never answers.
 */
void proto_note_line_dropped(void);

/** protocol.md §3.5: {"evt":"boot",...} */
void proto_event_boot(void);

/** protocol.md §3.5: {"evt":"hid_mounted",...} */
void proto_event_hid_mounted(void);

/** protocol.md §3.5: {"evt":"hid_unmounted",...} */
void proto_event_hid_unmounted(void);

/**
 * @brief True while a queued action sequence must stop early (release_all or
 *        safety release happened).  Checked by long-running HID loops.
 */
bool proto_seq_should_abort(void);