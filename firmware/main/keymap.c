#include "keymap.h"

#include <string.h>

typedef struct {
    const char *name;
    uint8_t value; /* modifier bit for s_modifiers, usage id for s_keys */
} keymap_entry_t;

/* protocol.md §4 - modifier names.  win/cmd/meta/super are aliases of leftgui. */
static const keymap_entry_t s_modifiers[] = {
    {"leftctrl",   KM_MOD_LEFTCTRL},
    {"rightctrl",  KM_MOD_RIGHTCTRL},
    {"leftshift",  KM_MOD_LEFTSHIFT},
    {"rightshift", KM_MOD_RIGHTSHIFT},
    {"leftalt",    KM_MOD_LEFTALT},
    {"rightalt",   KM_MOD_RIGHTALT},
    {"leftgui",    KM_MOD_LEFTGUI},
    {"win",        KM_MOD_LEFTGUI},
    {"cmd",        KM_MOD_LEFTGUI},
    {"meta",       KM_MOD_LEFTGUI},
    {"super",      KM_MOD_LEFTGUI},
    {"rightgui",   KM_MOD_RIGHTGUI},
};

/* protocol.md §4 - regular key names (HID Usage ID, page 0x07). */
static const keymap_entry_t s_keys[] = {
    /* a..z: 0x04..0x1D */
    {"a", 0x04}, {"b", 0x05}, {"c", 0x06}, {"d", 0x07}, {"e", 0x08}, {"f", 0x09},
    {"g", 0x0A}, {"h", 0x0B}, {"i", 0x0C}, {"j", 0x0D}, {"k", 0x0E}, {"l", 0x0F},
    {"m", 0x10}, {"n", 0x11}, {"o", 0x12}, {"p", 0x13}, {"q", 0x14}, {"r", 0x15},
    {"s", 0x16}, {"t", 0x17}, {"u", 0x18}, {"v", 0x19}, {"w", 0x1A}, {"x", 0x1B},
    {"y", 0x1C}, {"z", 0x1D},
    /* 1..9: 0x1E..0x26, 0: 0x27 */
    {"1", 0x1E}, {"2", 0x1F}, {"3", 0x20}, {"4", 0x21}, {"5", 0x22}, {"6", 0x23},
    {"7", 0x24}, {"8", 0x25}, {"9", 0x26}, {"0", 0x27},
    /* editing / whitespace */
    {"enter", 0x28}, {"esc", 0x29}, {"backspace", 0x2A}, {"tab", 0x2B}, {"space", 0x2C},
    /* symbols (both spelled-out and literal names, protocol.md §4) */
    {"minus", 0x2D}, {"-", 0x2D},
    {"equal", 0x2E}, {"=", 0x2E},
    {"leftbracket", 0x2F}, {"[", 0x2F},
    {"rightbracket", 0x30}, {"]", 0x30},
    {"backslash", 0x31}, {"\\", 0x31},
    {"semicolon", 0x33}, {";", 0x33},
    {"quote", 0x34}, {"'", 0x34},
    {"grave", 0x35}, {"`", 0x35},
    {"comma", 0x36}, {",", 0x36},
    {"period", 0x37}, {".", 0x37},
    {"slash", 0x38}, {"/", 0x38},
    {"capslock", 0x39},
    /* function keys */
    {"f1", 0x3A}, {"f2", 0x3B}, {"f3", 0x3C}, {"f4", 0x3D}, {"f5", 0x3E}, {"f6", 0x3F},
    {"f7", 0x40}, {"f8", 0x41}, {"f9", 0x42}, {"f10", 0x43}, {"f11", 0x44}, {"f12", 0x45},
    {"printscreen", 0x46}, {"scrolllock", 0x47}, {"pause", 0x48},
    {"insert", 0x49}, {"home", 0x4A}, {"pageup", 0x4B},
    {"delete", 0x4C}, {"end", 0x4D}, {"pagedown", 0x4E},
    {"right", 0x4F}, {"left", 0x50}, {"down", 0x51}, {"up", 0x52},
    {"numlock", 0x53},
    /* keypad */
    {"kp_slash", 0x54}, {"kp_asterisk", 0x55}, {"kp_minus", 0x56}, {"kp_plus", 0x57},
    {"kp_enter", 0x58},
    {"kp_1", 0x59}, {"kp_2", 0x5A}, {"kp_3", 0x5B}, {"kp_4", 0x5C}, {"kp_5", 0x5D},
    {"kp_6", 0x5E}, {"kp_7", 0x5F}, {"kp_8", 0x60}, {"kp_9", 0x61},
    {"kp_0", 0x62}, {"kp_period", 0x63},
    {"application", 0x65}, {"power", 0x66}, {"kp_equal", 0x67},
    {"f13", 0x68}, {"f14", 0x69}, {"f15", 0x6A}, {"f16", 0x6B}, {"f17", 0x6C},
    {"f18", 0x6D}, {"f19", 0x6E}, {"f20", 0x6F}, {"f21", 0x70}, {"f22", 0x71},
    {"f23", 0x72}, {"f24", 0x73},
};

bool keymap_lookup_modifier(const char *name, uint8_t *bit)
{
    if (name == NULL || bit == NULL) {
        return false;
    }
    for (size_t i = 0; i < sizeof(s_modifiers) / sizeof(s_modifiers[0]); i++) {
        if (strcmp(name, s_modifiers[i].name) == 0) {
            *bit = s_modifiers[i].value;
            return true;
        }
    }
    return false;
}

bool keymap_lookup_key(const char *name, uint8_t *usage)
{
    if (name == NULL || usage == NULL) {
        return false;
    }
    for (size_t i = 0; i < sizeof(s_keys) / sizeof(s_keys[0]); i++) {
        if (strcmp(name, s_keys[i].name) == 0) {
            *usage = s_keys[i].value;
            return true;
        }
    }
    return false;
}

bool keymap_char_to_key(char c, uint8_t *usage, bool *shift)
{
    if (usage == NULL || shift == NULL) {
        return false;
    }
    *shift = false;

    if (c >= 'a' && c <= 'z') {
        *usage = (uint8_t)(0x04 + (c - 'a'));
        return true;
    }
    if (c >= 'A' && c <= 'Z') {
        *usage = (uint8_t)(0x04 + (c - 'A'));
        *shift = true;
        return true;
    }
    if (c >= '1' && c <= '9') {
        *usage = (uint8_t)(0x1E + (c - '1'));
        return true;
    }

    switch (c) {
    case '0':   *usage = 0x27; return true;
    case ' ':   *usage = KM_KEY_SPACE; return true;
    /* unshifted symbols */
    case '-':   *usage = 0x2D; return true;
    case '=':   *usage = 0x2E; return true;
    case '[':   *usage = 0x2F; return true;
    case ']':   *usage = 0x30; return true;
    case '\\':  *usage = 0x31; return true;
    case ';':   *usage = 0x33; return true;
    case '\'':  *usage = 0x34; return true;
    case '`':   *usage = 0x35; return true;
    case ',':   *usage = 0x36; return true;
    case '.':   *usage = 0x37; return true;
    case '/':   *usage = 0x38; return true;
    /* shifted symbols: protocol.md §4 "!@#$%^&*()_+{}|:\"<>?~" */
    case '!':   *usage = 0x1E; *shift = true; return true;
    case '@':   *usage = 0x1F; *shift = true; return true;
    case '#':   *usage = 0x20; *shift = true; return true;
    case '$':   *usage = 0x21; *shift = true; return true;
    case '%':   *usage = 0x22; *shift = true; return true;
    case '^':   *usage = 0x23; *shift = true; return true;
    case '&':   *usage = 0x24; *shift = true; return true;
    case '*':   *usage = 0x25; *shift = true; return true;
    case '(':   *usage = 0x26; *shift = true; return true;
    case ')':   *usage = 0x27; *shift = true; return true;
    case '_':   *usage = 0x2D; *shift = true; return true;
    case '+':   *usage = 0x2E; *shift = true; return true;
    case '{':   *usage = 0x2F; *shift = true; return true;
    case '}':   *usage = 0x30; *shift = true; return true;
    case '|':   *usage = 0x31; *shift = true; return true;
    case ':':   *usage = 0x33; *shift = true; return true;
    case '"':   *usage = 0x34; *shift = true; return true;
    case '~':   *usage = 0x35; *shift = true; return true;
    case '<':   *usage = 0x36; *shift = true; return true;
    case '>':   *usage = 0x37; *shift = true; return true;
    case '?':   *usage = 0x38; *shift = true; return true;
    default:    return false;
    }
}