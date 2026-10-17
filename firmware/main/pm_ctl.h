/*
 * pm_ctl.h - PM/DFS governor (protocol.md v1.2 §10.4).
 *
 * Two governors:
 *   - FIXED360 (boot default): no DFS, CPU stays at 360 MHz.  This is the
 *     configuration that ran 10/10 clean in the 2026-10-17 PM bisect.
 *   - ADAPTIVE (`set dfs=1`): CPU is boosted to 360 MHz immediately on any
 *     activity (UART frame, HID report, SD transfer) via an
 *     ESP_PM_CPU_FREQ_MAX lock, and the lock is released only after a
 *     hysteresis window with no activity (500 ms, 100 ms when the die is hot).
 *     Long SD/USB operations hold the lock for their whole duration so the
 *     clock never changes in the middle of a transfer.
 *
 * Failure safety: if esp_pm_configure() fails the governor falls back to fixed
 * 360 MHz and reports the error; the board is never left half-configured.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

struct cJSON; /* used by pm_ctl_fill_json() */

#define PM_DFS_MIN_MHZ 40
#define PM_DFS_MAX_MHZ 360
#define PM_IDLE_THRESHOLD_MS 500  /* normal hysteresis before downclocking */
#define PM_IDLE_THRESHOLD_HOT_MS 100 /* die is hot -> downclock more eagerly */

typedef enum {
    PM_REASON_NONE = 0,
    PM_REASON_BOOT,     /* boot/first configuration */
    PM_REASON_UART_RX,  /* a UART frame arrived */
    PM_REASON_HID_TX,   /* a HID report is being sent */
    PM_REASON_SD_IO,    /* SD transfer in progress */
    PM_REASON_IDLE,     /* downclocked because nothing happened */
    PM_REASON_THERMAL,  /* downclocked early because the die is hot */
    PM_REASON_FALLBACK, /* esp_pm_configure failed -> fixed 360 */
} pm_reason_t;

/** Configure the boot default (fixed 360 MHz) and create the boost lock. */
esp_err_t pm_ctl_init(void);

/** True when the PM driver is compiled in (CONFIG_PM_ENABLE). */
bool pm_ctl_available(void);

/**
 * @brief Select the governor.
 * @param adaptive true = ADAPTIVE (DFS 40..360 with hysteresis), false = FIXED360.
 * @return ESP_OK, or the esp_pm_configure() error (after falling back to fixed).
 */
esp_err_t pm_ctl_set_adaptive(bool adaptive);

/** @brief Current governor is ADAPTIVE. */
bool pm_ctl_is_adaptive(void);

/** @brief Boost to max CPU freq for an activity (UART frame / HID report). */
void pm_ctl_activity(pm_reason_t reason);

/**
 * @brief Hold the CPU at max frequency for the duration of a long IO operation.
 * Reference counted: every pm_ctl_hold() must be paired with pm_ctl_release().
 */
void pm_ctl_hold(pm_reason_t reason);
void pm_ctl_release(void);

/** @brief Periodic governor tick (called from the 25 ms monitor task). */
void pm_ctl_tick(void);

/** @brief Milliseconds since the last activity that grabbed the boost lock. */
uint32_t pm_ctl_idle_ms(void);

/**
 * @brief Quiet period (ms) that preceded the most recent activity.
 *
 * This is what `stats.pm.idle_ms` reports: reading `stats` is itself a boost
 * source, so "time since the last activity" would always be ~0 for the very
 * command that asks.  The gap before it shows whether the governor really let
 * the CPU drop between commands.
 */
uint32_t pm_ctl_last_gap_ms(void);

/**
 * @brief Fill a JSON object with the current governor state (stats.pm).
 *
 * Beyond the governor bookkeeping this also exposes physical frequency
 * evidence:
 *   - `freq_now_mhz`: PM framework's current CPU frequency;
 *   - `measured_mhz`: mcycle/timer delta sampled in the most recent tick that
 *     ran WITHOUT holding the boost lock (null before the first such tick).
 */
void pm_ctl_fill_json(struct cJSON *pm);
