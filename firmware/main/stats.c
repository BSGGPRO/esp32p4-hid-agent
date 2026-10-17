/*
 * stats.c - the v1.2 "stats" payload (docs/protocol.md §3.6).
 *
 * Everything here is live state, never a cached estimate, with one deliberate
 * exception: CPU usage needs two samples of the per-core idle task run time
 * counters (CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS + ESP timer clock), so the
 * percentages describe the window between two consecutive `stats` calls.
 */
#include "stats.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cmd_protocol.h"
#include "esp_clk_tree.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hid_device.h"
#include "pm_ctl.h"
#include "sdcard.h"
#include "thermal.h"
#include "uart_link.h"
#include "sdkconfig.h"

/* docs/protocol.md §3.6: "proto" inside stats is the version string with a v */
#define STATS_PROTO_STR "v1.2"

/*
 * The live core clock.  With CONFIG_PM_ENABLE (DFS) the CPU frequency changes
 * at runtime, so the build-time CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ would lie.
 * esp_clk_tree_src_get_freq_hz() is the public API for the current value.
 */
static double current_cpu_mhz(void)
{
    uint32_t hz = 0;
    if (esp_clk_tree_src_get_freq_hz(SOC_MOD_CLK_CPU, ESP_CLK_TREE_SRC_FREQ_PRECISION_CACHED,
                                     &hz) == ESP_OK && hz > 0) {
        return (double)hz / 1000000.0;
    }
    return (double)CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ;
}

/* The P4 has two cores; the idle tasks are named IDLE0 / IDLE1. */
#define STATS_MAX_CORES 2

/* Slack for tasks created between uxTaskGetNumberOfTasks() and the snapshot. */
#define STATS_TASK_SLACK 8

static SemaphoreHandle_t s_lock;
static bool s_have_sample;
static uint32_t s_prev_total_us;
static uint32_t s_prev_idle_us[STATS_MAX_CORES];

static const char *task_state_str(eTaskState state)
{
    switch (state) {
    case eRunning:
        return "running";
    case eReady:
        return "ready";
    case eBlocked:
        return "blocked";
    case eSuspended:
        return "suspended";
    case eDeleted:
        return "deleted";
    default:
        return "invalid";
    }
}

/* One scheduler-suspended snapshot, used for both the task table and the CPU
 * idle counters.  Returns NULL on OOM. */
static TaskStatus_t *snapshot_tasks(UBaseType_t *out_count, uint32_t *out_total_us)
{
    const UBaseType_t count = uxTaskGetNumberOfTasks();
    TaskStatus_t *tasks = malloc(sizeof(TaskStatus_t) * (count + STATS_TASK_SLACK));
    if (tasks == NULL) {
        return NULL;
    }

    configRUN_TIME_COUNTER_TYPE total = 0;
    const UBaseType_t got = uxTaskGetSystemState(tasks, count + STATS_TASK_SLACK, &total);
    *out_count = got;
    *out_total_us = (uint32_t)total;
    return tasks;
}

static void add_cpu(cJSON *stats, const TaskStatus_t *tasks, UBaseType_t count, uint32_t total_us)
{    cJSON *cpu = cJSON_AddObjectToObject(stats, "cpu");
    if (cpu == NULL) {
        return;
    }

    uint32_t idle_us[STATS_MAX_CORES] = {0};
    bool idle_seen[STATS_MAX_CORES] = {0};

    for (UBaseType_t i = 0; i < count; i++) {
        const char *name = tasks[i].pcTaskName;
        if (name == NULL || strncmp(name, "IDLE", 4) != 0) {
            continue;
        }
        /* "IDLE0" / "IDLE1"; a single-core build would name it "IDLE". */
        const int core = (name[4] >= '0' && name[4] <= '9') ? (name[4] - '0') : 0;
        if (core >= 0 && core < STATS_MAX_CORES) {
            idle_us[core] = (uint32_t)tasks[i].ulRunTimeCounter;
            idle_seen[core] = true;
        }
    }

    double pct[STATS_MAX_CORES] = {0.0};
    uint32_t window_ms = 0;

    if (s_have_sample) {
        const uint32_t elapsed_us = total_us - s_prev_total_us;
        window_ms = elapsed_us / 1000u;
        if (elapsed_us > 0) {
            for (int core = 0; core < STATS_MAX_CORES; core++) {
                if (!idle_seen[core] || !s_prev_idle_us[core]) {
                    continue;
                }
                const uint32_t idle_delta = idle_us[core] - s_prev_idle_us[core];
                double busy = 100.0 * (1.0 - (double)idle_delta / (double)elapsed_us);
                if (busy < 0.0) {
                    busy = 0.0;
                }
                if (busy > 100.0) {
                    busy = 100.0;
                }
                /* one decimal place is plenty for a dashboard */
                pct[core] = (double)((int)(busy * 10.0 + 0.5)) / 10.0;
            }
        }
    }

    s_prev_total_us = total_us;
    for (int core = 0; core < STATS_MAX_CORES; core++) {
        if (idle_seen[core]) {
            s_prev_idle_us[core] = idle_us[core];
        }
    }
    s_have_sample = true;

    cJSON_AddNumberToObject(cpu, "core0_pct", pct[0]);
    cJSON_AddNumberToObject(cpu, "core1_pct", pct[1]);
    cJSON_AddNumberToObject(cpu, "mhz", current_cpu_mhz());
    cJSON_AddNumberToObject(cpu, "window_ms", (double)window_ms);
}

static void add_mem(cJSON *stats)
{
    cJSON *mem = cJSON_AddObjectToObject(stats, "mem");
    if (mem == NULL) {
        return;
    }
    /* heap_* is the internal RAM heap, psram_* the external one (when enabled) */
    cJSON_AddNumberToObject(mem, "heap_free",
                            (double)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    /* Internal RAM only, so the three heap_* fields are comparable: the global
     * esp_get_minimum_free_heap_size() also counts the 32 MB PSRAM heap and
     * would report ~32 MB here, which is meaningless as a low-water mark. */
    cJSON_AddNumberToObject(mem, "heap_min_free",
                            (double)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(mem, "heap_total",
                            (double)heap_caps_get_total_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(mem, "psram_free",
                            (double)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    cJSON_AddNumberToObject(mem, "psram_total",
                            (double)heap_caps_get_total_size(MALLOC_CAP_SPIRAM));
}

static void add_tasks(cJSON *stats, const TaskStatus_t *tasks, UBaseType_t count)
{
    cJSON *arr = cJSON_AddArrayToObject(stats, "tasks");
    if (arr == NULL) {
        return;
    }

    for (UBaseType_t i = 0; i < count; i++) {
        cJSON *item = cJSON_CreateObject();
        if (item == NULL) {
            return;
        }
        cJSON_AddStringToObject(item, "name",
                                tasks[i].pcTaskName != NULL ? tasks[i].pcTaskName : "");
        cJSON_AddNumberToObject(item, "prio", (double)tasks[i].uxCurrentPriority);
        cJSON_AddStringToObject(item, "state", task_state_str(tasks[i].eCurrentState));
        /* StackType_t is uint8_t on this port, so the high water mark is bytes */
        cJSON_AddNumberToObject(item, "stack_hwm", (double)tasks[i].usStackHighWaterMark);
        cJSON_AddItemToArray(arr, item);
    }
}

static void add_sd(cJSON *stats)
{
    sdcard_status_t sd;
    sdcard_get_status(&sd);

    cJSON *obj = cJSON_AddObjectToObject(stats, "sd");
    if (obj == NULL) {
        return;
    }
    cJSON_AddBoolToObject(obj, "present", sd.present);
    cJSON_AddBoolToObject(obj, "mounted", sd.mounted);
    if (sd.err[0] != '\0') {
        cJSON_AddStringToObject(obj, "err", sd.err);
    } else {
        cJSON_AddItemToObject(obj, "err", cJSON_CreateNull());
    }
    cJSON_AddStringToObject(obj, "fs", sd.fs);
    cJSON_AddNumberToObject(obj, "capacity_mb", (double)sd.capacity_mb);
    if (sd.free_mb >= 0) {
        cJSON_AddNumberToObject(obj, "free_mb", (double)sd.free_mb);
    } else {
        cJSON_AddItemToObject(obj, "free_mb", cJSON_CreateNull());
    }
    cJSON_AddStringToObject(obj, "cid", sd.cid);
    cJSON_AddStringToObject(obj, "type", sd.type);
    if (sd.has_speed) {
        cJSON_AddNumberToObject(obj, "speed_write_kbps", (double)sd.speed_write_kbps);
        cJSON_AddNumberToObject(obj, "speed_read_kbps", (double)sd.speed_read_kbps);
    } else {
        cJSON_AddItemToObject(obj, "speed_write_kbps", cJSON_CreateNull());
        cJSON_AddItemToObject(obj, "speed_read_kbps", cJSON_CreateNull());
    }

    /* v1.2 additions (optional fields, §10.4): the bus that answered, the
     * card-detect state (null: this socket has no CD switch) and the per-attempt
     * probe log so a "no card" verdict is explainable from `stats` alone. */
    if (sd.bus[0] != '\0') {
        cJSON_AddStringToObject(obj, "bus", sd.bus);
    } else {
        cJSON_AddItemToObject(obj, "bus", cJSON_CreateNull());
    }
    if (sd.cd_known) {
        cJSON_AddBoolToObject(obj, "cd", sd.cd_present);
    } else {
        cJSON_AddItemToObject(obj, "cd", cJSON_CreateNull());
    }
    cJSON *tries = cJSON_AddArrayToObject(obj, "tries");
    if (tries != NULL) {
        for (int i = 0; i < sd.try_count && i < SDCARD_MAX_TRIES; i++) {
            cJSON *item = cJSON_CreateObject();
            if (item == NULL) {
                break;
            }
            cJSON_AddStringToObject(item, "bus", sd.tries[i].bus);
            if (sd.tries[i].err[0] != '\0') {
                cJSON_AddStringToObject(item, "err", sd.tries[i].err);
            } else {
                cJSON_AddItemToObject(item, "err", cJSON_CreateNull());
            }
            cJSON_AddItemToArray(tries, item);
        }
    }
}

static void add_pm(cJSON *stats)
{
    cJSON *pm = cJSON_AddObjectToObject(stats, "pm");
    if (pm == NULL) {
        return;
    }
    pm_ctl_fill_json(pm);
    cJSON_AddNumberToObject(pm, "cur_mhz", current_cpu_mhz());
}

static void add_thermal(cJSON *stats)
{
    float celsius = 0.0f;
    if (thermal_read(&celsius)) {
        /* one decimal place, same rounding style as cpu pct */
        cJSON_AddNumberToObject(stats, "temp_c",
                                (double)((int)(celsius * 10.0f + 0.5f)) / 10.0);
    } else {
        cJSON_AddItemToObject(stats, "temp_c", cJSON_CreateNull());
    }
    cJSON_AddBoolToObject(stats, "temp_warn", thermal_warn());
}

static void add_net(cJSON *stats)
{
    /* v0.2 has no Ethernet/Wi-Fi uplink (the user does not plug a cable in), but
     * the v1.2 field set is frozen, so report the honest "no uplink" state. */
    cJSON *net = cJSON_AddObjectToObject(stats, "net");
    if (net == NULL) {
        return;
    }

    uint8_t mac[6] = {0};
    char mac_str[18] = "";
    if (esp_efuse_mac_get_default(mac) == ESP_OK) {
        snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }

    cJSON_AddBoolToObject(net, "up", false);
    cJSON_AddBoolToObject(net, "dhcp", false);
    cJSON_AddItemToObject(net, "ip", cJSON_CreateNull());
    cJSON_AddItemToObject(net, "netmask", cJSON_CreateNull());
    cJSON_AddItemToObject(net, "gw", cJSON_CreateNull());
    cJSON_AddStringToObject(net, "mac", mac_str);
    cJSON_AddNumberToObject(net, "link_mbps", 0);
}

static void add_uart(cJSON *stats)
{
    cJSON *uart = cJSON_AddObjectToObject(stats, "uart");
    if (uart == NULL) {
        return;
    }
    cJSON_AddNumberToObject(uart, "rx_ok", (double)proto_rx_ok());
    cJSON_AddNumberToObject(uart, "rx_bad", (double)proto_rx_bad());
    cJSON_AddNumberToObject(uart, "tx_ok", (double)uart_link_tx_frames());
}

void stats_init(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
    }

    UBaseType_t count = 0;
    uint32_t total_us = 0;
    TaskStatus_t *tasks = snapshot_tasks(&count, &total_us);
    if (tasks == NULL) {
        return;
    }

    for (UBaseType_t i = 0; i < count; i++) {
        const char *name = tasks[i].pcTaskName;
        if (name == NULL || strncmp(name, "IDLE", 4) != 0) {
            continue;
        }
        const int core = (name[4] >= '0' && name[4] <= '9') ? (name[4] - '0') : 0;
        if (core >= 0 && core < STATS_MAX_CORES) {
            s_prev_idle_us[core] = (uint32_t)tasks[i].ulRunTimeCounter;
        }
    }
    s_prev_total_us = total_us;
    s_have_sample = true;
    free(tasks);
}

cJSON *stats_build(void)
{
    if (s_lock != NULL) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }

    cJSON *stats = cJSON_CreateObject();
    if (stats == NULL) {
        goto done;
    }

    cJSON_AddNumberToObject(stats, "uptime_ms", (double)uart_link_now_ms());

    UBaseType_t count = 0;
    uint32_t total_us = 0;
    TaskStatus_t *tasks = snapshot_tasks(&count, &total_us);

    if (tasks != NULL) {
        add_cpu(stats, tasks, count, total_us);
        add_tasks(stats, tasks, count);
        free(tasks);
    } else {
        cJSON *cpu = cJSON_AddObjectToObject(stats, "cpu");
        if (cpu != NULL) {
            cJSON_AddNumberToObject(cpu, "core0_pct", 0);
            cJSON_AddNumberToObject(cpu, "core1_pct", 0);
            cJSON_AddNumberToObject(cpu, "mhz", current_cpu_mhz());
            cJSON_AddNumberToObject(cpu, "window_ms", 0);
        }
        cJSON_AddArrayToObject(stats, "tasks");
    }

    add_mem(stats);
    add_pm(stats);
    add_thermal(stats);
    add_sd(stats);
    add_net(stats);

    cJSON *hid = cJSON_AddObjectToObject(stats, "hid");
    if (hid != NULL) {
        cJSON_AddBoolToObject(hid, "mounted", hid_device_is_mounted());
    }

    add_uart(stats);
    cJSON_AddStringToObject(stats, "proto", STATS_PROTO_STR);

done:
    if (s_lock != NULL) {
        xSemaphoreGive(s_lock);
    }
    return stats;
}
