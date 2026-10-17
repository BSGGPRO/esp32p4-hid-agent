/*
 * keymap.h - protocol.md §4 key name table (single authoritative copy on the
 * firmware side).
 *
 * Modifier names fold into the HID modifier byte, regular names map to HID
 * Usage IDs (Keyboard/Keypad page 0x07).  Anything not in the table is rejected
 * with err:"bad_key".
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Modifier bits (protocol.md §4) */
#define KM_MOD_LEFTCTRL   0x01
#define KM_MOD_LEFTSHIFT  0x02
#define KM_MOD_LEFTALT    0x04
#define KM_MOD_LEFTGUI    0x08
#define KM_MOD_RIGHTCTRL  0x10
#define KM_MOD_RIGHTSHIFT 0x20
#define KM_MOD_RIGHTALT   0x40
#define KM_MOD_RIGHTGUI   0x80

/* HID Usage IDs used by the text() shift mapping */
#define KM_KEY_SPACE 0x2C

/**
 * @brief Look up a modifier key name (protocol.md §4, incl. win/cmd/meta/super).
 *
 * @param[in]  name  NUL terminated key name.
 * @param[out] bit   Modifier bit for that name.
 * @return true when the name is a modifier name.
 */
bool keymap_lookup_modifier(const char *name, uint8_t *bit);

/**
 * @brief Look up a regular (non-modifier) key name.
 *
 * @param[in]  name   NUL terminated key name (e.g. "a", "f12", "kp_enter", "-").
 * @param[out] usage  HID Usage ID.
 * @return true when the name is a regular key name.
 */
bool keymap_lookup_key(const char *name, uint8_t *usage);

/**
 * @brief Map a printable US-layout ASCII character to (usage, shift) for text().
 *
 * protocol.md §4: "A-Z -> shift + lower case key", and
 * !@#$%^&*()_+{}|:"<>?~ -> shift + base key.
 *
 * @param[in]  c      Character to map (expected in 0x20..0x7E).
 * @param[out] usage  HID Usage ID.
 * @param[out] shift  true when the left shift modifier is required.
 * @return false when the character has no US-layout mapping.
 */
bool keymap_char_to_key(char c, uint8_t *usage, bool *shift);