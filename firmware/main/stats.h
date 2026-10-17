/*
 * stats.h - "stats" command payload (docs/protocol.md v1.2 §3.6).
 *
 * The object returned by stats_build() is the frozen v1.2 schema:
 *   uptime_ms, cpu{core0_pct,core1_pct,mhz,window_ms},
 *   mem{heap_free,heap_min_free,heap_total,psram_free,psram_total},
 *   tasks[{name,prio,state,stack_hwm}],
 *   sd{present,mounted,err,fs,capacity_mb,free_mb,cid,type,
 *      speed_write_kbps,speed_read_kbps},
 *   net{up,dhcp,ip,netmask,gw,mac,link_mbps}, hid{mounted},
 *   uart{rx_ok,rx_bad,tx_ok}, proto.
 */
#pragma once

#include "cJSON.h"

/**
 * @brief Take the first CPU idle sample and create the internal lock.
 *
 * Called once from app_main() after the rest of the system is up, so that the
 * first `stats` command already has a meaningful measurement window.
 */
void stats_init(void);

/**
 * @brief Build the v1.2 "stats" object (docs/protocol.md §3.6).
 *
 * Live values only: heap/PSRAM from the IDF allocator, task table from
 * uxTaskGetSystemState(), SD state from sdcard.c, HID/UART counters from their
 * owners.  The CPU percentages are derived from the per-core idle task run
 * time counters (CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS) delta between two
 * calls; the very first call returns 0 with window_ms=0.
 *
 * @return cJSON object owned by the caller (cJSON_Delete), or NULL on OOM.
 */
cJSON *stats_build(void);
