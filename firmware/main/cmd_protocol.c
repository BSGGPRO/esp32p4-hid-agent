/*
 * cmd_protocol.c - implementation of docs/protocol.md v1.0.1 (all §3 commands,
 * §3.4 error codes, §3.5 events, §6 safety semantics, §9 rulings).
 */
#include "cmd_protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "hid_device.h"
#include "keymap.h"
#include "uart_link.h"

static const char *TAG = "cmd_proto";

#define FW_VERSION_STR    "1.0.0"
#define PROTO_VERSION_STR "1.0"

/* protocol.md §3.1: caps is fixed */
#define CAPS_KEYBOARD "hid_keyboard"
#define CAPS_MOUSE    "hid_mouse"
#define CAPS_UART     "uart"

/* protocol.md §3.4 error codes */
#define ERR_BAD_JSON         "bad_json"
#define ERR_UNKNOWN_CMD      "unknown_cmd"
#define ERR_BAD_ARG          "bad_arg"
#define ERR_BAD_KEY          "bad_key"
#define ERR_UNSUPPORTED_CHAR "unsupported_char"
#define ERR_NOT_MOUNTED      "not_mounted"
#define ERR_RATE_LIMITED     "rate_limited"

/* protocol.md §3 command defaults and limits */
#define DEFAULT_SAFETY_TIMEOUT_MS 1500
#define SAFETY_TIMEOUT_MAX_MS     60000
#define DEFAULT_TEXT_CPS          40
#define TEXT_CPS_MAX              1000
#define DEFAULT_TAP_HOLD_MS       30
#define DEFAULT_CLICK_MS          40
#define DEFAULT_CLICK_INTERVAL_MS 80
#define DEFAULT_MOUSE_CLICK_COUNT 1
#define MOUSE_CLICK_COUNT_MAX     1000
#define MOUSE_DELTA_LIMIT         32767
#define TIME_VALUE_MAX_MS         60000

/* protocol.md §3.4: more than 200 frames per second are rejected */
#define RATE_LIMIT_PER_SEC 200

/* Queued action sequences */
#define SEQ_QUEUE_LEN      16
#define SEQ_QUEUE_WAIT_MS  200

/* protocol.md §6.1 monitoring cadence (also drives §9.5 mount detection) */
#define MONITOR_PERIOD_MS 25

/********************************** Job model *********************************/

typedef enum {
    JOB_KEY_PRESS = 0,
    JOB_KEY_RELEASE,
    JOB_KEY_TAP,
    JOB_TEXT,
    JOB_MOUSE_BTN,
    JOB_MOUSE_CLICK,
    JOB_MOUSE_MOVE,
    JOB_MOUSE_SCROLL,
} seq_job_type_t;

typedef enum {
    BTN_ACTION_DOWN = 0,
    BTN_ACTION_UP,
    BTN_ACTION_CLICK,
} btn_action_t;

typedef struct {
    seq_job_type_t type;
    bool has_id;
    int id;

    /* key jobs */
    uint8_t modifier;
    uint8_t keys[HID_DEV_MAX_KEYS];
    size_t key_count;
    uint32_t hold_ms;

    /* text jobs */
    char *text;
    uint32_t cps;

    /* mouse button / click jobs */
    uint8_t btn_mask;
    uint8_t btn_action;
    int count;
    uint32_t click_ms;
    uint32_t interval_ms;

    /* mouse move / scroll jobs */
    int32_t dx;
    int32_t dy;
} seq_job_t;

typedef struct {
    uint8_t modifier;
    uint8_t usages[HID_DEV_MAX_KEYS];
    size_t count;
} key_set_t;

/********************************** State *************************************/

static volatile uint32_t s_rx_ok;
static volatile uint32_t s_rx_bad;
static volatile uint32_t s_last_rx_ms;
static volatile uint32_t s_safety_timeout_ms = DEFAULT_SAFETY_TIMEOUT_MS;
static volatile bool s_seq_abort;

static QueueHandle_t s_queue;

/* >200 frames/s window */
static uint32_t s_window_start_ms;
static uint32_t s_window_count;

/******************************* Response helpers *****************************/

/*
 * protocol.md §3.1 "kbd_keys" (agreed with the PC bridge selftest):
 * popcount of the modifier byte + number of occupied key slots.
 */
static int kbd_keys_reported(void)
{
    return (int)__builtin_popcount((unsigned)hid_device_kbd_modifier()) +
           (int)hid_device_kbd_key_count();
}

static cJSON *resp_new(bool ok, bool with_id, int id)
{
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) {
        return NULL;
    }
    cJSON_AddBoolToObject(obj, "ok", ok);
    if (with_id) {
        cJSON_AddNumberToObject(obj, "id", (double)id);
    }
    cJSON_AddNumberToObject(obj, "t", (double)uart_link_now_ms());
    return obj;
}

static void resp_send(cJSON *obj)
{
    if (obj == NULL) {
        return;
    }
    char *json = cJSON_PrintUnformatted(obj);
    if (json != NULL) {
        uart_link_write_line(json);
        cJSON_free(json);
    }
    cJSON_Delete(obj);
}

static void reply_ok(bool has_id, int id)
{
    resp_send(resp_new(true, has_id, id));
}

static void reply_err(bool has_id, int id, const char *err)
{
    cJSON *obj = resp_new(false, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "err", err);
    resp_send(obj);
}

static void reply_err_detail_str(bool has_id, int id, const char *err, const char *detail)
{
    cJSON *obj = resp_new(false, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "err", err);
    cJSON_AddStringToObject(obj, "detail", detail != NULL ? detail : "");
    resp_send(obj);
}

/* protocol.md §9.2: unsupported_char carries a 0-based integer index */
static void reply_err_detail_int(bool has_id, int id, const char *err, int detail)
{
    cJSON *obj = resp_new(false, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "err", err);
    cJSON_AddNumberToObject(obj, "detail", (double)detail);
    resp_send(obj);
}

/******************************* JSON helpers *********************************/

/* Reads an optional integer field.  Returns false on a type error. */
static bool json_read_int(const cJSON *obj, const char *name, bool *present, int *out)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (item == NULL) {
        *present = false;
        return true;
    }
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    const double value = item->valuedouble;
    if (value < -2147483648.0 || value > 2147483647.0) {
        return false;
    }
    if (value != (double)(int)value) {
        return false;
    }
    *present = true;
    *out = (int)value;
    return true;
}

/* protocol.md §9.4: the response id must be numerically equal to the request id. */
static bool json_read_id(const cJSON *root, bool *has_id, int *id)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, "id");
    *has_id = false;
    *id = 0;
    if (item == NULL) {
        return true;
    }
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    const double value = item->valuedouble;
    if (value < -2147483648.0 || value > 2147483647.0 || value != (double)(int)value) {
        return false;
    }
    *has_id = true;
    *id = (int)value;
    return true;
}

/******************************** Rate limiting *******************************/

static void rate_window_add(void)
{
    const uint32_t now = uart_link_now_ms();
    if ((uint32_t)(now - s_window_start_ms) >= 1000u) {
        s_window_start_ms = now;
        s_window_count = 0;
    }
    s_window_count++;
}

static bool rate_limited(void)
{
    return s_window_count > RATE_LIMIT_PER_SEC;
}

/********************************** Events ************************************/

static void event_send_mounted(void)
{
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "evt", "hid_mounted");
    cJSON_AddNumberToObject(obj, "t", (double)uart_link_now_ms());
    resp_send(obj);
}

static void event_send_unmounted(void)
{
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "evt", "hid_unmounted");
    cJSON_AddNumberToObject(obj, "t", (double)uart_link_now_ms());
    resp_send(obj);
}

/* protocol.md §3.5: safety_release carries a detail string */
static void event_send_safety_release(const char *detail)
{
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "evt", "safety_release");
    cJSON_AddStringToObject(obj, "detail", detail != NULL ? detail : "");
    cJSON_AddNumberToObject(obj, "t", (double)uart_link_now_ms());
    resp_send(obj);
}

void proto_event_boot(void)
{
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "evt", "boot");
    cJSON_AddStringToObject(obj, "fw", FW_VERSION_STR);
    cJSON_AddStringToObject(obj, "proto", PROTO_VERSION_STR);
    cJSON_AddNumberToObject(obj, "uptime_ms", (double)uart_link_now_ms());
    cJSON_AddNumberToObject(obj, "t", (double)uart_link_now_ms());
    resp_send(obj);
}

void proto_event_hid_mounted(void)
{
    event_send_mounted();
}

void proto_event_hid_unmounted(void)
{
    event_send_unmounted();
}

bool proto_seq_should_abort(void)
{
    return s_seq_abort;
}

/****************************** Key parsing ***********************************/

/* 0 = ok, 1 = unknown key name (bad_name set), 2 = bad argument */
static int parse_key_set(const cJSON *jkeys, key_set_t *out, const char **bad_name)
{
    memset(out, 0, sizeof(*out));
    *bad_name = NULL;

    if (!cJSON_IsArray(jkeys)) {
        return 2;
    }

    const cJSON *item = NULL;
    cJSON_ArrayForEach(item, jkeys) {
        if (!cJSON_IsString(item) || item->valuestring == NULL) {
            return 2;
        }

        uint8_t bit = 0;
        if (keymap_lookup_modifier(item->valuestring, &bit)) {
            out->modifier |= bit;
            continue;
        }

        uint8_t usage = 0;
        if (keymap_lookup_key(item->valuestring, &usage)) {
            bool duplicate = false;
            for (size_t i = 0; i < out->count; i++) {
                if (out->usages[i] == usage) {
                    duplicate = true;
                    break;
                }
            }
            if (duplicate) {
                continue;
            }
            if (out->count >= HID_DEV_MAX_KEYS) {
                /* protocol.md §3.2: at most 6 non-modifier keys per frame */
                return 2;
            }
            out->usages[out->count++] = usage;
            continue;
        }

        /* protocol.md §3.2: detail is the first illegal key name */
        *bad_name = item->valuestring;
        return 1;
    }
    return 0;
}

static void reply_key_parse_error(bool has_id, int id, int rc, const char *bad_name)
{
    if (rc == 1) {
        reply_err_detail_str(has_id, id, ERR_BAD_KEY, bad_name);
    } else {
        reply_err(has_id, id, ERR_BAD_ARG);
    }
}

/******************************* Job submission *******************************/

static bool job_submit(seq_job_t *job)
{
    if (s_queue == NULL) {
        return false;
    }
    return xQueueSend(s_queue, &job, pdMS_TO_TICKS(SEQ_QUEUE_WAIT_MS)) == pdTRUE;
}

static void reply_queue_full(bool has_id, int id)
{
    reply_err_detail_str(has_id, id, ERR_RATE_LIMITED, "queue full");
}

static void queue_key_job(seq_job_type_t type, const key_set_t *ks, bool has_id, int id, uint32_t hold_ms)
{
    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = type;
    job->has_id = has_id;
    job->id = id;
    job->modifier = ks->modifier;
    memcpy(job->keys, ks->usages, sizeof(job->keys));
    job->key_count = ks->count;
    job->hold_ms = hold_ms;

    if (!job_submit(job)) {
        free(job);
        reply_queue_full(has_id, id);
    }
}

/****************************** Command handlers ******************************/

static void cmd_ping(bool has_id, int id)
{
    cJSON *obj = resp_new(true, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddBoolToObject(obj, "pong", true);
    cJSON_AddStringToObject(obj, "fw", FW_VERSION_STR);
    cJSON_AddStringToObject(obj, "proto", PROTO_VERSION_STR);
    cJSON_AddNumberToObject(obj, "uptime_ms", (double)uart_link_now_ms());
    cJSON_AddBoolToObject(obj, "host_mounted", hid_device_is_mounted());
    cJSON *caps = cJSON_AddArrayToObject(obj, "caps");
    if (caps != NULL) {
        cJSON_AddItemToArray(caps, cJSON_CreateString(CAPS_KEYBOARD));
        cJSON_AddItemToArray(caps, cJSON_CreateString(CAPS_MOUSE));
        cJSON_AddItemToArray(caps, cJSON_CreateString(CAPS_UART));
    }
    resp_send(obj);
}

static void cmd_status(bool has_id, int id)
{
    cJSON *obj = resp_new(true, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddBoolToObject(obj, "host_mounted", hid_device_is_mounted());
    cJSON_AddNumberToObject(obj, "kbd_keys", (double)kbd_keys_reported());
    cJSON_AddNumberToObject(obj, "mouse_btn", (double)hid_device_mouse_buttons());
    cJSON_AddNumberToObject(obj, "rx_ok", (double)s_rx_ok);
    cJSON_AddNumberToObject(obj, "rx_bad", (double)s_rx_bad);
    cJSON_AddNumberToObject(obj, "safety_timeout_ms", (double)s_safety_timeout_ms);
    resp_send(obj);
}

static void cmd_keepalive(bool has_id, int id)
{
    /* The keep-alive timestamp is refreshed for every received frame already. */
    reply_ok(has_id, id);
}

static void cmd_set(const cJSON *root, bool has_id, int id)
{
    const cJSON *jkey = cJSON_GetObjectItemCaseSensitive(root, "key");
    const cJSON *jvalue = cJSON_GetObjectItemCaseSensitive(root, "value");

    if (!cJSON_IsString(jkey) || jkey->valuestring == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (!cJSON_IsNumber(jvalue)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    const double raw = jvalue->valuedouble;
    if (raw != (double)(int)raw) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    const int value = (int)raw;

    if (strcmp(jkey->valuestring, "safety_timeout_ms") != 0) {
        reply_err_detail_str(has_id, id, ERR_BAD_ARG, "unsupported key");
        return;
    }
    if (value < 0 || value > SAFETY_TIMEOUT_MAX_MS) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    s_safety_timeout_ms = (uint32_t)value;

    cJSON *obj = resp_new(true, has_id, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddStringToObject(obj, "key", "safety_timeout_ms");
    cJSON_AddNumberToObject(obj, "value", (double)value);
    resp_send(obj);
}

static void cmd_key_action(const cJSON *root, bool has_id, int id, bool release)
{
    const cJSON *jkeys = cJSON_GetObjectItemCaseSensitive(root, "keys");
    key_set_t ks;
    const char *bad_name = NULL;

    const int rc = parse_key_set(jkeys, &ks, &bad_name);
    if (rc != 0) {
        reply_key_parse_error(has_id, id, rc, bad_name);
        return;
    }
    /* protocol.md §9.3: refuse without touching local key state */
    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }
    queue_key_job(release ? JOB_KEY_RELEASE : JOB_KEY_PRESS, &ks, has_id, id, 0);
}

static void cmd_key_tap(const cJSON *root, bool has_id, int id)
{
    const cJSON *jkeys = cJSON_GetObjectItemCaseSensitive(root, "keys");
    key_set_t ks;
    const char *bad_name = NULL;

    const int rc = parse_key_set(jkeys, &ks, &bad_name);
    if (rc != 0) {
        reply_key_parse_error(has_id, id, rc, bad_name);
        return;
    }

    bool has_hold = false;
    int hold_ms = DEFAULT_TAP_HOLD_MS;
    if (!json_read_int(root, "hold_ms", &has_hold, &hold_ms)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (has_hold && (hold_ms < 0 || hold_ms > TIME_VALUE_MAX_MS)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }
    queue_key_job(JOB_KEY_TAP, &ks, has_id, id, (uint32_t)(has_hold ? hold_ms : DEFAULT_TAP_HOLD_MS));
}

static void cmd_release_all(bool has_id, int id)
{
    /* protocol.md §9.3: the safety primitive never fails and always clears. */
    hid_device_release_all();
    /* protocol.md §6.5: abort any in-flight text()/key_tap()/click() sequence. */
    s_seq_abort = true;

    if (!has_id) {
        return;
    }
    cJSON *obj = resp_new(true, true, id);
    if (obj == NULL) {
        return;
    }
    cJSON_AddNumberToObject(obj, "kbd_keys", 0);
    cJSON_AddNumberToObject(obj, "mouse_btn", 0);
    resp_send(obj);
}

static void cmd_text(const cJSON *root, bool has_id, int id)
{
    const cJSON *jtext = cJSON_GetObjectItemCaseSensitive(root, "text");
    if (!cJSON_IsString(jtext) || jtext->valuestring == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    const char *text = jtext->valuestring;
    /* protocol.md §9.2: reject non printable ASCII with the 0-based index. */
    for (size_t i = 0; text[i] != '\0'; i++) {
        const unsigned char c = (unsigned char)text[i];
        if (c < 0x20 || c > 0x7E) {
            reply_err_detail_int(has_id, id, ERR_UNSUPPORTED_CHAR, (int)i);
            return;
        }
    }

    bool has_cps = false;
    int cps = DEFAULT_TEXT_CPS;
    if (!json_read_int(root, "cps", &has_cps, &cps)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (has_cps && (cps < 1 || cps > TEXT_CPS_MAX)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }

    if (text[0] == '\0') {
        if (has_id) {
            cJSON *obj = resp_new(true, true, id);
            if (obj != NULL) {
                cJSON_AddNumberToObject(obj, "sent", 0);
                resp_send(obj);
            }
        }
        return;
    }

    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->text = strdup(text);
    if (job->text == NULL) {
        free(job);
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = JOB_TEXT;
    job->has_id = has_id;
    job->id = id;
    job->cps = (uint32_t)(has_cps ? cps : DEFAULT_TEXT_CPS);

    if (!job_submit(job)) {
        free(job->text);
        free(job);
        reply_queue_full(has_id, id);
    }
}

static void cmd_mouse_move(const cJSON *root, bool has_id, int id)
{
    bool has_dx = false;
    bool has_dy = false;
    int dx = 0;
    int dy = 0;

    if (!json_read_int(root, "dx", &has_dx, &dx) || !json_read_int(root, "dy", &has_dy, &dy)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (!has_dx || !has_dy) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (dx < -MOUSE_DELTA_LIMIT || dx > MOUSE_DELTA_LIMIT ||
        dy < -MOUSE_DELTA_LIMIT || dy > MOUSE_DELTA_LIMIT) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }

    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = JOB_MOUSE_MOVE;
    job->has_id = has_id;
    job->id = id;
    job->dx = dx;
    job->dy = dy;
    if (!job_submit(job)) {
        free(job);
        reply_queue_full(has_id, id);
    }
}

static void cmd_mouse_scroll(const cJSON *root, bool has_id, int id)
{
    bool has_dy = false;
    int dy = 0;

    if (!json_read_int(root, "dy", &has_dy, &dy) || !has_dy) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (dy < -MOUSE_DELTA_LIMIT || dy > MOUSE_DELTA_LIMIT) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }

    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = JOB_MOUSE_SCROLL;
    job->has_id = has_id;
    job->id = id;
    job->dy = dy;
    if (!job_submit(job)) {
        free(job);
        reply_queue_full(has_id, id);
    }
}

static bool parse_btn_mask(const char *name, uint8_t *mask)
{
    if (strcmp(name, "left") == 0) {
        *mask = HID_DEV_BTN_LEFT;
        return true;
    }
    if (strcmp(name, "right") == 0) {
        *mask = HID_DEV_BTN_RIGHT;
        return true;
    }
    if (strcmp(name, "middle") == 0) {
        *mask = HID_DEV_BTN_MIDDLE;
        return true;
    }
    return false;
}

static void cmd_mouse_btn(const cJSON *root, bool has_id, int id)
{
    const cJSON *jbtn = cJSON_GetObjectItemCaseSensitive(root, "btn");
    const cJSON *jaction = cJSON_GetObjectItemCaseSensitive(root, "action");

    uint8_t mask = 0;
    if (!cJSON_IsString(jbtn) || jbtn->valuestring == NULL || !parse_btn_mask(jbtn->valuestring, &mask)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (!cJSON_IsString(jaction) || jaction->valuestring == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    uint8_t action;
    if (strcmp(jaction->valuestring, "down") == 0) {
        action = BTN_ACTION_DOWN;
    } else if (strcmp(jaction->valuestring, "up") == 0) {
        action = BTN_ACTION_UP;
    } else if (strcmp(jaction->valuestring, "click") == 0) {
        action = BTN_ACTION_CLICK;
    } else {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    bool has_click_ms = false;
    int click_ms = DEFAULT_CLICK_MS;
    if (!json_read_int(root, "click_ms", &has_click_ms, &click_ms)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (has_click_ms && (click_ms < 0 || click_ms > TIME_VALUE_MAX_MS)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }

    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = JOB_MOUSE_BTN;
    job->has_id = has_id;
    job->id = id;
    job->btn_mask = mask;
    job->btn_action = action;
    job->click_ms = (uint32_t)(has_click_ms ? click_ms : DEFAULT_CLICK_MS);
    if (!job_submit(job)) {
        free(job);
        reply_queue_full(has_id, id);
    }
}

static void cmd_mouse_click(const cJSON *root, bool has_id, int id)
{
    /* protocol.md §3.3: btn (default left), count (default 1), interval_ms (default 80) */
    uint8_t mask = HID_DEV_BTN_LEFT;
    const cJSON *jbtn = cJSON_GetObjectItemCaseSensitive(root, "btn");
    if (jbtn != NULL) {
        if (!cJSON_IsString(jbtn) || jbtn->valuestring == NULL || !parse_btn_mask(jbtn->valuestring, &mask)) {
            reply_err(has_id, id, ERR_BAD_ARG);
            return;
        }
    }

    bool has_count = false;
    int count = DEFAULT_MOUSE_CLICK_COUNT;
    if (!json_read_int(root, "count", &has_count, &count)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (has_count && (count < 1 || count > MOUSE_CLICK_COUNT_MAX)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    bool has_interval = false;
    int interval_ms = DEFAULT_CLICK_INTERVAL_MS;
    if (!json_read_int(root, "interval_ms", &has_interval, &interval_ms)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    if (has_interval && (interval_ms < 0 || interval_ms > TIME_VALUE_MAX_MS)) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }

    if (!hid_device_is_mounted()) {
        reply_err(has_id, id, ERR_NOT_MOUNTED);
        return;
    }

    seq_job_t *job = calloc(1, sizeof(*job));
    if (job == NULL) {
        reply_err(has_id, id, ERR_BAD_ARG);
        return;
    }
    job->type = JOB_MOUSE_CLICK;
    job->has_id = has_id;
    job->id = id;
    job->btn_mask = mask;
    job->count = has_count ? count : DEFAULT_MOUSE_CLICK_COUNT;
    job->click_ms = DEFAULT_CLICK_MS;
    job->interval_ms = (uint32_t)(has_interval ? interval_ms : DEFAULT_CLICK_INTERVAL_MS);
    if (!job_submit(job)) {
        free(job);
        reply_queue_full(has_id, id);
    }
}

/******************************* Sequence worker ******************************/

static void job_reply_ok(const seq_job_t *job, bool aborted, int sent)
{
    if (!job->has_id) {
        return;
    }
    cJSON *obj = resp_new(true, true, job->id);
    if (obj == NULL) {
        return;
    }
    if (aborted) {
        cJSON_AddBoolToObject(obj, "aborted", true);
    }
    if (sent >= 0) {
        cJSON_AddNumberToObject(obj, "sent", (double)sent);
    }
    cJSON_AddNumberToObject(obj, "kbd_keys", (double)kbd_keys_reported());
    cJSON_AddNumberToObject(obj, "mouse_btn", (double)hid_device_mouse_buttons());
    resp_send(obj);
}

static void run_job(seq_job_t *job)
{
    bool aborted = false;
    int sent = -1;
    esp_err_t err = ESP_OK;

    switch (job->type) {
    case JOB_KEY_PRESS:
        err = hid_device_kbd_press(job->modifier, job->keys, job->key_count);
        break;

    case JOB_KEY_RELEASE:
        err = hid_device_kbd_release(job->modifier, job->keys, job->key_count);
        break;

    case JOB_KEY_TAP:
        err = hid_device_kbd_press(job->modifier, job->keys, job->key_count);
        if (err == ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(job->hold_ms == 0 ? 1 : job->hold_ms));
            hid_device_kbd_release(job->modifier, job->keys, job->key_count);
        }
        break;

    case JOB_TEXT: {
        const uint32_t period = job->cps > 0 ? (1000u / job->cps) : 1u;
        uint32_t hold = period / 2u;
        if (hold == 0) {
            hold = 1;
        }
        const size_t total = strlen(job->text);
        sent = 0;
        for (size_t i = 0; i < total; i++) {
            if (s_seq_abort) {
                aborted = true;
                break;
            }
            uint8_t usage = 0;
            bool shift = false;
            if (!keymap_char_to_key(job->text[i], &usage, &shift)) {
                continue; /* unreachable: characters were validated on receipt */
            }
            const esp_err_t tap_err = hid_device_kbd_tap(shift ? KM_MOD_LEFTSHIFT : 0, usage, hold);
            if (tap_err != ESP_OK) {
                err = tap_err;
                break;
            }
            sent = (int)(i + 1);
            const uint32_t rest = period > hold ? (period - hold) : 1u;
            vTaskDelay(pdMS_TO_TICKS(rest));
        }
        break;
    }

    case JOB_MOUSE_BTN:
        if (job->btn_action == BTN_ACTION_DOWN) {
            err = hid_device_mouse_button(job->btn_mask, true);
        } else if (job->btn_action == BTN_ACTION_UP) {
            err = hid_device_mouse_button(job->btn_mask, false);
        } else {
            err = hid_device_mouse_button(job->btn_mask, true);
            vTaskDelay(pdMS_TO_TICKS(job->click_ms == 0 ? 1 : job->click_ms));
            if (!s_seq_abort) {
                hid_device_mouse_button(job->btn_mask, false);
            }
        }
        break;

    case JOB_MOUSE_CLICK:
        for (int i = 0; i < job->count; i++) {
            if (s_seq_abort) {
                aborted = true;
                break;
            }
            hid_device_mouse_button(job->btn_mask, true);
            vTaskDelay(pdMS_TO_TICKS(job->click_ms == 0 ? 1 : job->click_ms));
            hid_device_mouse_button(job->btn_mask, false);
            if (i + 1 < job->count) {
                vTaskDelay(pdMS_TO_TICKS(job->interval_ms));
            }
        }
        break;

    case JOB_MOUSE_MOVE:
        err = hid_device_mouse_move(job->dx, job->dy);
        break;

    case JOB_MOUSE_SCROLL:
        err = hid_device_mouse_scroll(job->dy);
        break;
    }

    if (!aborted && s_seq_abort) {
        aborted = true;
    }
    if (aborted) {
        /* Make sure an interrupted sequence cannot leave anything pressed. */
        hid_device_release_all();
    }

    if (err != ESP_OK) {
        reply_err(job->has_id, job->id, ERR_BAD_ARG);
    } else {
        job_reply_ok(job, aborted, sent);
    }
}

static void seq_worker_task(void *arg)
{
    (void)arg;
    seq_job_t *job = NULL;

    for (;;) {
        if (xQueueReceive(s_queue, &job, portMAX_DELAY) != pdTRUE || job == NULL) {
            continue;
        }
        /* A stale abort from a previous sequence must not kill this job. */
        s_seq_abort = false;
        run_job(job);
        if (job->text != NULL) {
            free(job->text);
        }
        free(job);
        job = NULL;
    }
}

/***************************** Mount + safety monitor *************************/

static void monitor_task(void *arg)
{
    (void)arg;

    for (;;) {
        /* protocol.md §9.5: attach/detach edge detection through tud_mounted() */
        hid_device_service();

        /* protocol.md §6.1: safety release while something is held */
        const uint32_t timeout = s_safety_timeout_ms;
        if (timeout > 0 && hid_device_has_pressed()) {
            const uint32_t now = uart_link_now_ms();
            if ((uint32_t)(now - s_last_rx_ms) >= timeout) {
                const uint8_t modifier = hid_device_kbd_modifier();
                const size_t keys = hid_device_kbd_key_count();
                const uint8_t buttons = hid_device_mouse_buttons();
                char detail[96];

                snprintf(detail, sizeof(detail),
                         "timeout_ms=%u kbd_modifier=0x%02X kbd_keys=%u mouse_btn=0x%02X",
                         (unsigned)timeout, (unsigned)modifier, (unsigned)keys, (unsigned)buttons);

                hid_device_release_all();
                s_seq_abort = true;
                event_send_safety_release(detail);
                s_last_rx_ms = uart_link_now_ms();
            }
        }

        vTaskDelay(pdMS_TO_TICKS(MONITOR_PERIOD_MS));
    }
}

/******************************** Entry points ********************************/

void proto_init(void)
{
    s_last_rx_ms = uart_link_now_ms();
    s_safety_timeout_ms = DEFAULT_SAFETY_TIMEOUT_MS;
    s_seq_abort = false;

    s_queue = xQueueCreate(SEQ_QUEUE_LEN, sizeof(seq_job_t *));
    if (s_queue != NULL) {
        xTaskCreate(seq_worker_task, "hid_seq", 4096, NULL, 6, NULL);
    }
    xTaskCreate(monitor_task, "hid_mon", 3584, NULL, 7, NULL);

    ESP_LOGI(TAG, "protocol %s ready (queue=%d, safety=%u ms)",
             PROTO_VERSION_STR, SEQ_QUEUE_LEN, (unsigned)DEFAULT_SAFETY_TIMEOUT_MS);
}

void proto_note_line_dropped(void)
{
    rate_window_add();
    s_last_rx_ms = uart_link_now_ms();
    s_rx_bad++;
}

static bool looks_like_frame(const char *line, size_t len)
{
    size_t start = 0;
    size_t end = len;

    while (start < end && (line[start] == ' ' || line[start] == '\t')) {
        start++;
    }
    while (end > start && (line[end - 1] == ' ' || line[end - 1] == '\t' ||
                           line[end - 1] == '\r' || line[end - 1] == '\n')) {
        end--;
    }
    if (end <= start) {
        return false;
    }
    return line[start] == '{' && line[end - 1] == '}';
}

static void dispatch(const char *cmd, const cJSON *root, bool has_id, int id)
{
    if (strcmp(cmd, "ping") == 0) {
        cmd_ping(has_id, id);
    } else if (strcmp(cmd, "status") == 0) {
        cmd_status(has_id, id);
    } else if (strcmp(cmd, "keepalive") == 0) {
        cmd_keepalive(has_id, id);
    } else if (strcmp(cmd, "set") == 0) {
        cmd_set(root, has_id, id);
    } else if (strcmp(cmd, "key") == 0 || strcmp(cmd, "key_down") == 0) {
        cmd_key_action(root, has_id, id, false);
    } else if (strcmp(cmd, "key_up") == 0) {
        cmd_key_action(root, has_id, id, true);
    } else if (strcmp(cmd, "key_tap") == 0) {
        cmd_key_tap(root, has_id, id);
    } else if (strcmp(cmd, "release_all") == 0) {
        cmd_release_all(has_id, id);
    } else if (strcmp(cmd, "text") == 0 || strcmp(cmd, "text_tap") == 0) {
        cmd_text(root, has_id, id);
    } else if (strcmp(cmd, "mouse_move") == 0) {
        cmd_mouse_move(root, has_id, id);
    } else if (strcmp(cmd, "mouse_btn") == 0) {
        cmd_mouse_btn(root, has_id, id);
    } else if (strcmp(cmd, "mouse_scroll") == 0) {
        cmd_mouse_scroll(root, has_id, id);
    } else if (strcmp(cmd, "mouse_click") == 0) {
        cmd_mouse_click(root, has_id, id);
    } else {
        reply_err(has_id, id, ERR_UNKNOWN_CMD);
    }
}

void proto_handle_line(const char *line, size_t len)
{
    /* protocol.md §6.1: any PC frame refreshes the safety timer. */
    s_last_rx_ms = uart_link_now_ms();
    rate_window_add();

    if (line == NULL || len == 0 || !looks_like_frame(line, len)) {
        s_rx_bad++;
        return;
    }

    cJSON *root = cJSON_Parse(line);
    if (root == NULL || !cJSON_IsObject(root)) {
        if (root != NULL) {
            cJSON_Delete(root);
        }
        s_rx_bad++;
        return;
    }

    bool has_id = false;
    int id = 0;
    if (!json_read_id(root, &has_id, &id)) {
        reply_err(false, 0, ERR_BAD_ARG);
        cJSON_Delete(root);
        s_rx_bad++;
        return;
    }

    const cJSON *jcmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(jcmd) || jcmd->valuestring == NULL) {
        /* protocol.md §3.4: a JSON object without a "cmd" field is bad_json */
        if (has_id) {
            reply_err(true, id, ERR_BAD_JSON);
        }
        cJSON_Delete(root);
        s_rx_bad++;
        return;
    }

    /*
     * protocol.md §6 / §9.6: release_all is THE safety command and must never be
     * rate limited.  Measured on hardware: 200 back-to-back mouse_move frames
     * followed by release_all got "rate_limited" - i.e. the one command that
     * un-sticks the target machine could be refused, leaving keys held down.
     */
    if (rate_limited() && strcmp(jcmd->valuestring, "release_all") != 0) {
        if (has_id) {
            reply_err(true, id, ERR_RATE_LIMITED);
        }
        cJSON_Delete(root);
        return;
    }

    dispatch(jcmd->valuestring, root, has_id, id);
    cJSON_Delete(root);
    s_rx_ok++;
}