/*
 * thermal.h - ESP32-P4 internal temperature sensor + overheat policy.
 *
 * docs/protocol.md v1.2 §10.4: the board is passively cooled (a small heatsink
 * shell only), so the firmware reports its die temperature in `stats` and
 * applies a workload throttle when it gets too hot.
 *
 * Thresholds are runtime-configurable through the existing `set` command:
 *   {"cmd":"set","key":"temp_warn_c","value":70}
 *   {"cmd":"set","key":"temp_throttle_c","value":80}
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

/* Defaults; change with the `set` command (integers, degrees Celsius). */
#define THERMAL_WARN_C      70
#define THERMAL_THROTTLE_C  80
#define THERMAL_MIN_C       20
#define THERMAL_MAX_C       100

/** Install + enable the internal temperature sensor.  Never fatal. */
esp_err_t thermal_init(void);

/**
 * @brief Read the die temperature.
 * @param[out] out_celsius  value in °C (may be NULL)
 * @return true when the sensor answered; false if unavailable.
 */
bool thermal_read(float *out_celsius);

/** Last known temperature in °C (NAN if never read successfully). */
float thermal_last_celsius(void);

/** true when the last reading is >= the warning threshold. */
bool thermal_warn(void);

/** true when the last reading is >= the throttle threshold. */
bool thermal_throttled(void);

/** Update thresholds (used by the `set` command). */
void thermal_set_thresholds(int warn_c, int throttle_c);

/** Current thresholds (for logging/tests). */
void thermal_get_thresholds(int *warn_c, int *throttle_c);
