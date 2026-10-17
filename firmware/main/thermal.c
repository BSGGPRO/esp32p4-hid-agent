/*
 * thermal.c - ESP32-P4 internal temperature sensor (driver/temperature_sensor.h)
 *             plus the overheat policy helpers.
 *
 * The P4 sensor has three accuracy windows (see
 * components/soc/esp32p4/temperature_sensor_periph.c):
 *   -10 .. 80 °C  (±1 °C),  20 .. 100 °C  (±2 °C),  50 .. 125 °C  (±3 °C)
 * We measure in the 20..100 °C window: it still covers idle temperatures and
 * goes high enough to detect an overheat event (the ±2 °C accuracy is fine for
 * a 70/80 °C policy).
 *
 * Logging: normally ESP_LOG_NONE (protocol.md §1).  Under CONFIG_HID_AGENT_RX_DEBUG
 * warnings are printed once per crossing, never per sample.
 */
#include "thermal.h"

#include <math.h>

#include "driver/temperature_sensor.h"
#include "esp_log.h"

static const char *TAG = "thermal";

static temperature_sensor_handle_t s_tsens;
static bool s_enabled;
static float s_last_c = NAN;
static bool s_have_reading;
static int s_warn_c = THERMAL_WARN_C;
static int s_throttle_c = THERMAL_THROTTLE_C;
static bool s_warn_logged;
static bool s_throttle_logged;

esp_err_t thermal_init(void)
{
    if (s_enabled) {
        return ESP_OK;
    }
    const temperature_sensor_config_t cfg =
        TEMPERATURE_SENSOR_CONFIG_DEFAULT(THERMAL_MIN_C, THERMAL_MAX_C);
    esp_err_t err = temperature_sensor_install(&cfg, &s_tsens);
    if (err != ESP_OK) {
        return err;
    }
    err = temperature_sensor_enable(s_tsens);
    if (err != ESP_OK) {
        temperature_sensor_uninstall(s_tsens);
        s_tsens = NULL;
        return err;
    }
    s_enabled = true;

    /* Warm-up sample, so the very first `stats` already carries a temperature. */
    (void)thermal_read(NULL);
    return ESP_OK;
}

bool thermal_read(float *out_celsius)
{
    if (!s_enabled || s_tsens == NULL) {
        if (out_celsius != NULL) {
            *out_celsius = NAN;
        }
        return false;
    }
    float celsius = NAN;
    if (temperature_sensor_get_celsius(s_tsens, &celsius) != ESP_OK) {
        if (out_celsius != NULL) {
            *out_celsius = NAN;
        }
        return false;
    }
    s_last_c = celsius;
    s_have_reading = true;
    if (out_celsius != NULL) {
        *out_celsius = celsius;
    }

    /* One-shot log on threshold crossings (silent unless RX_DEBUG is on). */
    if (!s_throttle_logged && celsius >= (float)s_throttle_c) {
        s_throttle_logged = true;
        ESP_LOGW(TAG, "temp %.1f C >= throttle %d C: workload throttled", celsius,
                 s_throttle_c);
    } else if (s_throttle_logged && celsius < (float)s_throttle_c - 3.0f) {
        s_throttle_logged = false;
        ESP_LOGI(TAG, "temp %.1f C: throttle released", celsius);
    }
    if (!s_warn_logged && celsius >= (float)s_warn_c) {
        s_warn_logged = true;
        ESP_LOGW(TAG, "temp %.1f C >= warn %d C", celsius, s_warn_c);
    } else if (s_warn_logged && celsius < (float)s_warn_c - 3.0f) {
        s_warn_logged = false;
    }
    return true;
}

float thermal_last_celsius(void)
{
    return s_have_reading ? s_last_c : NAN;
}

bool thermal_warn(void)
{
    return s_have_reading && s_last_c >= (float)s_warn_c;
}

bool thermal_throttled(void)
{
    return s_have_reading && s_last_c >= (float)s_throttle_c;
}

void thermal_set_thresholds(int warn_c, int throttle_c)
{
    s_warn_c = warn_c;
    s_throttle_c = throttle_c;
    /* Re-evaluate on the next sample so a lowered threshold also re-arms. */
    s_warn_logged = false;
    s_throttle_logged = false;
}

void thermal_get_thresholds(int *warn_c, int *throttle_c)
{
    if (warn_c != NULL) {
        *warn_c = s_warn_c;
    }
    if (throttle_c != NULL) {
        *throttle_c = s_throttle_c;
    }
}
