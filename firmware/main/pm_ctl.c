/*
 * pm_ctl.c - PM/DFS governor with boost/hysteresis (see pm_ctl.h).
 *
 * Measured on 2026-10-17 with the ESP32-P4 (rev v1.3) board:
 *   - FIXED360: 10/10 sd_test 1 MB clean, 0 HID re-enumerations.
 *   - plain DFS (min=40): sd_test 1 MB reset the board with
 *     rst:0x7 (HP_SYS_HP_WDT_RESET) on the first try, and the HID device
 *     re-enumerated periodically.
 * So DFS is opt-in via `set dfs=1` and the adaptive governor keeps the CPU at
 * 360 MHz during every IO burst, only downclocking after a quiet window.
 */
#include "pm_ctl.h"

#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#include "thermal.h"

#if CONFIG_PM_ENABLE
#include "esp_pm.h"
#endif

static const char *TAG = "pm_ctl";

static bool s_adaptive;
static bool s_lock_held;
static uint32_t s_io_holds; /* reference count for long SD/USB operations */
static uint32_t s_last_activity_ms;
static uint32_t s_boosts;
static uint32_t s_last_gap_ms; /* quiet period before the most recent activity */
static uint32_t s_downs;
static pm_reason_t s_last_reason = PM_REASON_BOOT;

#if CONFIG_PM_ENABLE
static esp_pm_lock_handle_t s_lock;
#endif

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static const char *reason_name(pm_reason_t r)
{
    switch (r) {
    case PM_REASON_BOOT:
        return "boot";
    case PM_REASON_UART_RX:
        return "uart_rx";
    case PM_REASON_HID_TX:
        return "hid_tx";
    case PM_REASON_SD_IO:
        return "sd_io";
    case PM_REASON_IDLE:
        return "idle";
    case PM_REASON_THERMAL:
        return "thermal";
    case PM_REASON_FALLBACK:
        return "fallback";
    case PM_REASON_NONE:
    default:
        return "none";
    }
}

static esp_err_t configure(bool adaptive)
{
#if CONFIG_PM_ENABLE
    esp_pm_config_t cfg = {
        .max_freq_mhz = PM_DFS_MAX_MHZ,
        .min_freq_mhz = adaptive ? PM_DFS_MIN_MHZ : PM_DFS_MAX_MHZ,
        .light_sleep_enable = false, /* USB HID must stay enumerated */
    };
    return esp_pm_configure(&cfg);
#else
    (void)adaptive;
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

static void release_lock(void)
{
#if CONFIG_PM_ENABLE
    if (s_lock_held) {
        esp_pm_lock_release(s_lock);
        s_lock_held = false;
    }
#endif
}

static esp_err_t fallback_fixed(void)
{
    release_lock();
    s_io_holds = 0;
    s_adaptive = false;
    s_last_reason = PM_REASON_FALLBACK;
    (void)configure(false);
    return ESP_OK;
}

esp_err_t pm_ctl_init(void)
{
    s_adaptive = false;
    s_lock_held = false;
    s_io_holds = 0;
    s_boosts = 0;
    s_downs = 0;
    s_last_activity_ms = now_ms();
    s_last_reason = PM_REASON_BOOT;

#if CONFIG_PM_ENABLE
    esp_err_t err = esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "cpu_boost", &s_lock);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_pm_lock_create failed: %d", (int)err);
        return err;
    }
    err = configure(false);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_pm_configure(fixed360) failed: %d", (int)err);
        return err;
    }
    return ESP_OK;
#else
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

bool pm_ctl_available(void)
{
#if CONFIG_PM_ENABLE
    return true;
#else
    return false;
#endif
}

bool pm_ctl_is_adaptive(void)
{
    return s_adaptive;
}

esp_err_t pm_ctl_set_adaptive(bool adaptive)
{
#if !CONFIG_PM_ENABLE
    (void)adaptive;
    return ESP_ERR_NOT_SUPPORTED;
#else
    if (!adaptive) {
        release_lock();
        s_io_holds = 0;
        const esp_err_t err = configure(false);
        if (err != ESP_OK) {
            (void)fallback_fixed();
            return err;
        }
        s_adaptive = false;
        s_last_reason = PM_REASON_NONE;
        ESP_LOGI(TAG, "governor=fixed360");
        return ESP_OK;
    }

    const esp_err_t err = configure(true);
    if (err != ESP_OK) {
        (void)fallback_fixed();
        return err;
    }
    s_adaptive = true;
    /* Boost immediately: the command itself is activity, and we must not
     * start the adaptive mode at 40 MHz. */
    pm_ctl_activity(PM_REASON_UART_RX);
    ESP_LOGI(TAG, "governor=adaptive min=%d max=%d", PM_DFS_MIN_MHZ, PM_DFS_MAX_MHZ);
    return ESP_OK;
#endif
}

void pm_ctl_activity(pm_reason_t reason)
{
    if (!s_adaptive) {
        return;
    }
    const uint32_t now = now_ms();
    s_last_gap_ms = now >= s_last_activity_ms ? now - s_last_activity_ms : 0;
    s_last_activity_ms = now;
    s_last_reason = reason;
#if CONFIG_PM_ENABLE
    if (!s_lock_held) {
        esp_pm_lock_acquire(s_lock);
        s_lock_held = true;
        s_boosts++;
    }
#endif
}

void pm_ctl_hold(pm_reason_t reason)
{
    if (!s_adaptive) {
        return;
    }
    s_io_holds++;
    pm_ctl_activity(reason);
}

void pm_ctl_release(void)
{
    if (s_io_holds > 0) {
        s_io_holds--;
    }
    /* The lock itself is released by pm_ctl_tick() once the hysteresis window
     * has elapsed, so a burst of short IO operations does not cause a
     * down/up frequency flap between them. */
}

uint32_t pm_ctl_idle_ms(void)
{
    const uint32_t now = now_ms();
    return now >= s_last_activity_ms ? now - s_last_activity_ms : 0;
}

uint32_t pm_ctl_last_gap_ms(void)
{
    return s_last_gap_ms;
}

void pm_ctl_tick(void)
{
    if (!s_adaptive) {
        return;
    }
    if (s_io_holds > 0) {
        return; /* never change frequency in the middle of IO */
    }
    const uint32_t threshold = thermal_warn() ? PM_IDLE_THRESHOLD_HOT_MS
                                              : PM_IDLE_THRESHOLD_MS;
    if (pm_ctl_idle_ms() < threshold) {
        return;
    }
    if (s_lock_held) {
        release_lock();
        s_downs++;
        s_last_reason = thermal_warn() ? PM_REASON_THERMAL : PM_REASON_IDLE;
    }
}

void pm_ctl_fill_json(struct cJSON *pm)
{
    if (pm == NULL) {
        return;
    }
    cJSON_AddStringToObject(pm, "governor", s_adaptive ? "adaptive" : "fixed360");
    cJSON_AddBoolToObject(pm, "dfs", s_adaptive);
    cJSON_AddNumberToObject(pm, "min_mhz", s_adaptive ? PM_DFS_MIN_MHZ : PM_DFS_MAX_MHZ);
    cJSON_AddNumberToObject(pm, "max_mhz", PM_DFS_MAX_MHZ);
    cJSON_AddNumberToObject(pm, "idle_ms", (double)s_last_gap_ms);
    cJSON_AddNumberToObject(pm, "boosts", (double)s_boosts);
    cJSON_AddNumberToObject(pm, "downs", (double)s_downs);
    cJSON_AddStringToObject(pm, "last_reason", reason_name(s_last_reason));
}
