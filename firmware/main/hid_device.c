#include "hid_device.h"

#include <stdio.h>
#include <string.h>

#include "class/hid/hid_device.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"

#include "cmd_protocol.h"
#include "uart_link.h"

static const char *TAG = "hid_device";

/*********************** TinyUSB descriptors (protocol.md §5) *******************/

#define TUSB_DESC_TOTAL_LEN (TUD_CONFIG_DESC_LEN + CFG_TUD_HID * TUD_HID_DESC_LEN)

/* Single HID interface, report IDs 1 (keyboard) and 2 (mouse). */
#define REPORT_ID_KEYBOARD HID_ITF_PROTOCOL_KEYBOARD
#define REPORT_ID_MOUSE    HID_ITF_PROTOCOL_MOUSE

/*
 * protocol.md §5 (v1.0.1 ruling): EP IN 0x81, 16 bytes, bInterval 4.
 *
 * esp_tinyusb's default port on ESP32-P4 is the high-speed OTG 2.0 port, where
 * an interrupt endpoint interval of 4 means 2^(4-1) * 125us = 1 ms (1000 Hz).
 * On a full-speed port (fallback) it means 4 ms.  bInterval 10 would have been
 * 64 ms per frame on the high-speed port, which contradicts protocol.md §7.
 */
#define HID_EP_IN_ADDR   0x81
#define HID_EP_IN_SIZE   16
#define HID_EP_INTERVAL  4

/* Mouse report fields are signed 8 bit: frames are split at +-127 (protocol.md §5). */
#define MOUSE_FRAME_MAX 127

/* Bounded wait for the interrupt IN endpoint to be serviced by the host. */
#define HID_READY_TIMEOUT_MS 100u
#define HID_SEND_ATTEMPTS    3

const uint8_t hid_report_descriptor[] = {
    TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(REPORT_ID_KEYBOARD)),
    TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(REPORT_ID_MOUSE)),
};

static const uint8_t hid_configuration_descriptor[] = {
    /* Configuration number, interface count, string index, total length,
     * attribute, power in mA (protocol.md §5: 100 mA, remote wakeup). */
    TUD_CONFIG_DESCRIPTOR(1, 1, 0, TUSB_DESC_TOTAL_LEN, TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 100),
    /* Interface number, string index, boot protocol, report descriptor len,
     * EP IN address, size, polling interval (protocol.md §5). */
    TUD_HID_DESCRIPTOR(0, 4, false, sizeof(hid_report_descriptor), HID_EP_IN_ADDR, HID_EP_IN_SIZE, HID_EP_INTERVAL),
};

/* Language ID is handed to tinyusb as a raw 2 byte array (index 0 special case). */
static const char s_lang_id[2] = {0x09, 0x04};
static char s_serial_str[13]; /* 12 hex chars from the base MAC + NUL (protocol.md §5) */
static const char *s_string_descriptor[5] = {
    s_lang_id,                 /* 0: language */
    "JC-ESP32P4-M3 Agent",     /* 1: Manufacturer */
    "P4 HID Bridge",           /* 2: Product */
    s_serial_str,              /* 3: Serial (chip MAC) */
    "HID interface",           /* 4: HID interface */
};

/***************************** HID report callbacks ***************************/

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance)
{
    (void)instance;
    return hid_report_descriptor;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen)
{
    (void)instance;
    (void)report_id;
    (void)report_type;
    (void)buffer;
    (void)reqlen;
    return 0; /* STALL */
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize)
{
    (void)instance;
    (void)report_id;
    (void)report_type;
    (void)buffer;
    (void)bufsize;
}

/******************************* Held key state *******************************/

static SemaphoreHandle_t s_lock;
static uint8_t s_modifier;
static uint8_t s_keys[HID_DEV_MAX_KEYS];
static uint8_t s_mouse_buttons;
static bool s_last_mounted;

/******************************* Report helpers *******************************/

static bool hid_wait_ready(uint32_t timeout_ms)
{
    const uint32_t start = uart_link_now_ms();

    while (uart_link_now_ms() - start < timeout_ms) {
        if (!tud_mounted()) {
            return false;
        }
        if (tud_hid_ready()) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    return tud_mounted() && tud_hid_ready();
}

/* Never silently drop a report: wait for the IN endpoint, then retry. */
static bool hid_send_keyboard(uint8_t modifier, const uint8_t keys[HID_DEV_MAX_KEYS])
{
    uint8_t snapshot[HID_DEV_MAX_KEYS];

    memcpy(snapshot, keys, sizeof(snapshot));
    for (int attempt = 0; attempt < HID_SEND_ATTEMPTS; attempt++) {
        if (!hid_wait_ready(HID_READY_TIMEOUT_MS)) {
            return false;
        }
        if (tud_hid_keyboard_report(REPORT_ID_KEYBOARD, modifier, snapshot)) {
            return true;
        }
    }
    return false;
}

static bool hid_send_mouse(uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel, int8_t pan)
{
    for (int attempt = 0; attempt < HID_SEND_ATTEMPTS; attempt++) {
        if (!hid_wait_ready(HID_READY_TIMEOUT_MS)) {
            return false;
        }
        if (tud_hid_mouse_report(REPORT_ID_MOUSE, buttons, dx, dy, wheel, pan)) {
            return true;
        }
    }
    return false;
}

/**************************** State helpers (locked) **************************/

static int key_index_locked(uint8_t usage)
{
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (s_keys[i] == usage) {
            return i;
        }
    }
    return -1;
}

static int first_free_key_locked(void)
{
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (s_keys[i] == 0) {
            return i;
        }
    }
    return -1;
}

static int first_free_in(const uint8_t keys[HID_DEV_MAX_KEYS])
{
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (keys[i] == 0) {
            return i;
        }
    }
    return -1;
}

static int32_t clamp_frame(int32_t value)
{
    if (value > MOUSE_FRAME_MAX) {
        return MOUSE_FRAME_MAX;
    }
    if (value < -MOUSE_FRAME_MAX) {
        return -MOUSE_FRAME_MAX;
    }
    return value;
}

/******************************** Public API **********************************/

esp_err_t hid_device_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    s_modifier = 0;
    s_mouse_buttons = 0;
    memset(s_keys, 0, sizeof(s_keys));
    s_last_mounted = false;

    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_BASE) != ESP_OK) {
        memset(mac, 0, sizeof(mac));
    }
    snprintf(s_serial_str, sizeof(s_serial_str), "%02X%02X%02X%02X%02X%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();

    /* VID/PID come from Kconfig (protocol.md §5: 0x303A / 0x4004 defaults). */
    tusb_cfg.descriptor.device = NULL;
    tusb_cfg.descriptor.full_speed_config = hid_configuration_descriptor;
    /* No-op on full-speed-only targets; required on the P4 high-speed port. */
    tusb_cfg.descriptor.high_speed_config = hid_configuration_descriptor;
    tusb_cfg.descriptor.string = s_string_descriptor;
    tusb_cfg.descriptor.string_count = (int)(sizeof(s_string_descriptor) / sizeof(s_string_descriptor[0]));

    esp_err_t err = tinyusb_driver_install(&tusb_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_driver_install failed: %d", (int)err);
        return err;
    }
    ESP_LOGI(TAG, "tinyusb ready: vid=0x303A pid=0x4004 serial=%s", s_serial_str);

    /* protocol.md §6.3: power-on release.  Before enumeration this is a no-op
     * inside tinyusb; the same call is issued again on attach. */
    hid_device_release_all();
    return ESP_OK;
}

bool hid_device_is_mounted(void)
{
    return tud_mounted();
}

void hid_device_service(void)
{
    const bool mounted = tud_mounted();

    if (mounted == s_last_mounted) {
        return;
    }
    s_last_mounted = mounted;

    if (mounted) {
        /* Fresh enumeration: make sure no stale key survives (protocol.md §6.3). */
        hid_device_release_all();
        proto_event_hid_mounted();
    } else {
        /* protocol.md §6.2: drop all local state on detach, no report needed. */
        hid_device_clear_state();
        proto_event_hid_unmounted();
    }
}

bool hid_device_has_pressed(void)
{
    bool pressed = false;

    if (s_lock == NULL) {
        return false;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    pressed = (s_modifier != 0) || (s_mouse_buttons != 0);
    for (int i = 0; i < HID_DEV_MAX_KEYS && !pressed; i++) {
        pressed = (s_keys[i] != 0);
    }
    xSemaphoreGive(s_lock);
    return pressed;
}

size_t hid_device_kbd_key_count(void)
{
    size_t count = 0;

    if (s_lock == NULL) {
        return 0;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (s_keys[i] != 0) {
            count++;
        }
    }
    xSemaphoreGive(s_lock);
    return count;
}

uint8_t hid_device_kbd_modifier(void)
{
    uint8_t modifier;

    if (s_lock == NULL) {
        return 0;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    modifier = s_modifier;
    xSemaphoreGive(s_lock);
    return modifier;
}

uint8_t hid_device_mouse_buttons(void)
{
    uint8_t buttons;

    if (s_lock == NULL) {
        return 0;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    buttons = s_mouse_buttons;
    xSemaphoreGive(s_lock);
    return buttons;
}

void hid_device_clear_state(void)
{
    if (s_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_modifier = 0;
    s_mouse_buttons = 0;
    memset(s_keys, 0, sizeof(s_keys));
    xSemaphoreGive(s_lock);
}

void hid_device_release_all(void)
{
    if (s_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    /* Clear first: even if the host never takes the report, the firmware state
     * (and therefore the safety logic) is already released. */
    s_modifier = 0;
    s_mouse_buttons = 0;
    memset(s_keys, 0, sizeof(s_keys));
    hid_send_keyboard(0, s_keys);
    hid_send_mouse(0, 0, 0, 0, 0);
    xSemaphoreGive(s_lock);
}

esp_err_t hid_device_kbd_press(uint8_t modifier, const uint8_t *usages, size_t count)
{
    if (usages == NULL || count > HID_DEV_MAX_KEYS) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    size_t needed = 0;
    for (size_t i = 0; i < count; i++) {
        if (usages[i] != 0 && key_index_locked(usages[i]) < 0) {
            needed++;
        }
    }
    size_t free_slots = 0;
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (s_keys[i] == 0) {
            free_slots++;
        }
    }
    if (needed > free_slots) {
        xSemaphoreGive(s_lock);
        return ESP_ERR_INVALID_ARG;
    }

    s_modifier |= modifier;
    for (size_t i = 0; i < count; i++) {
        if (usages[i] == 0 || key_index_locked(usages[i]) >= 0) {
            continue;
        }
        const int slot = first_free_key_locked();
        if (slot < 0) {
            break; /* unreachable: slots were reserved above */
        }
        s_keys[slot] = usages[i];
    }
    hid_send_keyboard(s_modifier, s_keys);
    xSemaphoreGive(s_lock);
    return ESP_OK;
}

esp_err_t hid_device_kbd_release(uint8_t modifier, const uint8_t *usages, size_t count)
{
    if (usages == NULL || count > HID_DEV_MAX_KEYS) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    s_modifier &= (uint8_t)~modifier;
    for (size_t i = 0; i < count; i++) {
        const int idx = key_index_locked(usages[i]);
        if (idx >= 0) {
            s_keys[idx] = 0;
        }
    }
    /* Compact the slots so that ordering stays stable and dumping is simple. */
    uint8_t compact[HID_DEV_MAX_KEYS] = {0};
    size_t n = 0;
    for (int i = 0; i < HID_DEV_MAX_KEYS; i++) {
        if (s_keys[i] != 0) {
            compact[n++] = s_keys[i];
        }
    }
    memcpy(s_keys, compact, sizeof(s_keys));

    hid_send_keyboard(s_modifier, s_keys);
    xSemaphoreGive(s_lock);
    return ESP_OK;
}

esp_err_t hid_device_kbd_tap(uint8_t modifier, uint8_t usage, uint32_t hold_ms)
{
    if (usage == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    uint8_t report_keys[HID_DEV_MAX_KEYS];
    memcpy(report_keys, s_keys, sizeof(report_keys));
    if (key_index_locked(usage) < 0) {
        const int slot = first_free_in(report_keys);
        if (slot < 0) {
            xSemaphoreGive(s_lock);
            return ESP_ERR_INVALID_ARG;
        }
        report_keys[slot] = usage;
    }

    /* Press and release are kept inside one critical section so that a
     * concurrent release_all can never be undone by this character. */
    hid_send_keyboard((uint8_t)(s_modifier | modifier), report_keys);
    vTaskDelay(pdMS_TO_TICKS(hold_ms == 0 ? 1 : hold_ms));
    hid_send_keyboard(s_modifier, s_keys);

    xSemaphoreGive(s_lock);
    return ESP_OK;
}

esp_err_t hid_device_mouse_move(int32_t dx, int32_t dy)
{
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const int32_t abs_x = dx < 0 ? -dx : dx;
    const int32_t abs_y = dy < 0 ? -dy : dy;
    int32_t frames = ((abs_x + MOUSE_FRAME_MAX - 1) / MOUSE_FRAME_MAX);
    const int32_t frames_y = ((abs_y + MOUSE_FRAME_MAX - 1) / MOUSE_FRAME_MAX);
    if (frames_y > frames) {
        frames = frames_y;
    }
    if (frames < 1) {
        frames = 1;
    }

    int32_t rest_x = dx;
    int32_t rest_y = dy;
    for (int32_t i = 0; i < frames; i++) {
        if (proto_seq_should_abort()) {
            break;
        }
        const int8_t step_x = (int8_t)clamp_frame(rest_x);
        const int8_t step_y = (int8_t)clamp_frame(rest_y);
        rest_x -= step_x;
        rest_y -= step_y;

        /* One report per lock acquisition: a release_all / safety release must
         * never wait for a long split to finish. */
        xSemaphoreTake(s_lock, portMAX_DELAY);
        hid_send_mouse(s_mouse_buttons, step_x, step_y, 0, 0);
        xSemaphoreGive(s_lock);
    }
    return ESP_OK;
}

esp_err_t hid_device_mouse_scroll(int32_t dy)
{
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const int32_t abs_y = dy < 0 ? -dy : dy;
    int32_t frames = ((abs_y + MOUSE_FRAME_MAX - 1) / MOUSE_FRAME_MAX);
    if (frames < 1) {
        frames = 1;
    }

    int32_t rest = dy;
    for (int32_t i = 0; i < frames; i++) {
        if (proto_seq_should_abort()) {
            break;
        }
        const int8_t step = (int8_t)clamp_frame(rest);
        rest -= step;

        xSemaphoreTake(s_lock, portMAX_DELAY);
        hid_send_mouse(s_mouse_buttons, 0, 0, step, 0);
        xSemaphoreGive(s_lock);
    }
    return ESP_OK;
}

esp_err_t hid_device_mouse_button(uint8_t btn_mask, bool down)
{
    if (s_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    if (down) {
        s_mouse_buttons |= btn_mask;
    } else {
        s_mouse_buttons &= (uint8_t)~btn_mask;
    }
    hid_send_mouse(s_mouse_buttons, 0, 0, 0, 0);

    xSemaphoreGive(s_lock);
    return ESP_OK;
}