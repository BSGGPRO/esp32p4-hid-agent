/*
 * hid_device.h - TinyUSB HID keyboard + mouse composite device (protocol.md §5).
 *
 * One HID interface, two report IDs:
 *   ID 1 (HID_ITF_PROTOCOL_KEYBOARD): 8 bytes [modifier, reserved, k0..k5]
 *   ID 2 (HID_ITF_PROTOCOL_MOUSE):    5 bytes [buttons, dx, dy, wheel, pan]
 *
 * The module owns the persistent "what is currently pressed" state, so that
 * release_all / safety timeout / unmount handling can always clear it.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* protocol.md §5 report IDs */
#define HID_DEV_REPORT_ID_KEYBOARD 1 /* == HID_ITF_PROTOCOL_KEYBOARD */
#define HID_DEV_REPORT_ID_MOUSE    2 /* == HID_ITF_PROTOCOL_MOUSE */

/* protocol.md §5 mouse buttons byte: bit0 left, bit1 right, bit2 middle */
#define HID_DEV_BTN_LEFT   0x01
#define HID_DEV_BTN_RIGHT  0x02
#define HID_DEV_BTN_MIDDLE 0x04

/* HID boot keyboard holds at most 6 simultaneous non-modifier keys */
#define HID_DEV_MAX_KEYS 6

/**
 * @brief Build descriptors, install the TinyUSB driver and publish zero reports.
 *
 * protocol.md §6.3: an all-zero keyboard/mouse report is emitted at start-up.
 */
esp_err_t hid_device_init(void);

/**
 * @brief protocol.md readiness predicate: tud_mounted().
 */
bool hid_device_is_mounted(void);

/**
 * @brief Poll tud_mounted() for attach/detach transitions and run the
 *        protocol.md §3.5/§6.2 side effects (events + state clearing).
 *
 * Must be called periodically from a task; esp_tinyusb v2 owns tud_mount_cb()
 * / tud_umount_cb() internally, so the app cannot implement those callbacks.
 */
void hid_device_service(void);

/** True when a key, modifier or mouse button is currently held. */
bool hid_device_has_pressed(void);

/** Number of held non-modifier key codes. */
size_t hid_device_kbd_key_count(void);

/** Current keyboard modifier byte. */
uint8_t hid_device_kbd_modifier(void);

/** Current mouse buttons byte (protocol.md §5 bit layout). */
uint8_t hid_device_mouse_buttons(void);

/** Clear local state without emitting any report. */
void hid_device_clear_state(void);

/**
 * @brief Clear all local state and emit all-zero keyboard + mouse reports.
 *
 * Used by release_all, by the safety timeout and when the host attaches.
 */
void hid_device_release_all(void);

/**
 * @brief Add modifier bits and key codes to the held set, then report it.
 *
 * @return ESP_ERR_INVALID_ARG when no free key slot is left (state unchanged).
 */
esp_err_t hid_device_kbd_press(uint8_t modifier, const uint8_t *usages, size_t count);

/**
 * @brief Remove modifier bits and key codes from the held set, then report it.
 */
esp_err_t hid_device_kbd_release(uint8_t modifier, const uint8_t *usages, size_t count);

/**
 * @brief Press one key (plus optional modifier) for hold_ms, then release it,
 *        restoring the previously held state.  Used to type text().
 */
esp_err_t hid_device_kbd_tap(uint8_t modifier, uint8_t usage, uint32_t hold_ms);

/**
 * @brief Relative mouse move, split into <=127 per axis frames.
 *
 * dx > 0 moves right, dy > 0 moves down (screen direction, never inverted).
 * Aborts remaining frames when a release_all / safety release happened.
 */
esp_err_t hid_device_mouse_move(int32_t dx, int32_t dy);

/**
 * @brief Wheel scroll, split into <=127 frames.  dy > 0 scrolls up (HID sense).
 */
esp_err_t hid_device_mouse_scroll(int32_t dy);

/** Set or clear one mouse button bit and report it (zero deltas). */
esp_err_t hid_device_mouse_button(uint8_t btn_mask, bool down);