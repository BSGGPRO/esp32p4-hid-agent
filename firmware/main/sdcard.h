/*
 * sdcard.h - TF card on SDMMC slot 0 (4-bit) + FAT mount + sd_test.
 *
 * Board facts (docs/protocol.md v1.2 §3.7, vendor schematic/P4 default IOMUX):
 *   CLK=GPIO43 CMD=GPIO44 D0=GPIO39 D1=GPIO40 D2=GPIO41 D3=GPIO42
 * These are exactly ESP-IDF's SDMMC_SLOT_CONFIG_DEFAULT() pins for esp32p4,
 * so no GPIO matrix remapping is involved.
 *
 * Probing and mounting are best effort: a missing, broken or exFAT card must
 * never crash the HID agent, it only shows up in the `stats` payload.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define SDCARD_MOUNT_POINT "/sdcard"
#define SDCARD_TEST_PATH   "/sdcard/ops_test.bin"

/* Probe ladder: every attempt is recorded so `stats.sd.tries` can explain a
 * "no card" verdict without needing a debug serial log. */
#define SDCARD_MAX_TRIES 4

typedef struct {
    char bus[24];   /* "4bit/20MHz", "1bit/400kHz", ... */
    char err[48];   /* "" when this attempt answered, else esp_err_to_name() */
} sdcard_try_t;

typedef struct {
    bool present;             /* card answered CMD0/CMD8/ACMD41 (sdmmc_card_init OK) */
    bool mounted;             /* esp_vfs_fat_sdmmc_mount() succeeded on /sdcard */
    char err[96];             /* "" when mounted, otherwise "<stage>:<esp_err_name>" */
    char fs[16];              /* "fat32" | "exfat" | "unknown" (from the raw boot sector) */
    uint32_t capacity_mb;     /* from CSD, decimal MB (1 MB = 1024*1024 B) */
    int64_t free_mb;          /* -1 when not mounted / unknown */
    char cid[160];            /* decoded CID: mfg/oem/name/serial/date */
    char type[16];            /* "SDSC" | "SDHC" | "SDXC" | "MMC" | "unknown" */
    char bus[24];             /* bus config that made the card answer, "" if none */
    bool has_speed;           /* true once sd_test measured the card */
    uint32_t speed_write_kbps;
    uint32_t speed_read_kbps;
    /* Card-detect: this board's microSD socket has no dedicated CD switch (pin 2
     * is CD/DATA3, shared with the 4-bit data line), so there is no GPIO to
     * read.  cd_known stays false and stats reports "cd": null. */
    bool cd_known;
    bool cd_present;
    int try_count;
    sdcard_try_t tries[SDCARD_MAX_TRIES];
} sdcard_status_t;

typedef struct {
    bool ok;                    /* format + remount succeeded */
    /* evidence captured BEFORE formatting */
    char before_cid[160];
    char before_type[16];
    char before_fs[16];
    uint32_t before_capacity_mb;
    /* state AFTER formatting + remount */
    bool mounted_after;
    char fs_after[16];
    char bus[24];
    uint32_t capacity_mb_after;
    int64_t free_mb_after;      /* -1 when unknown */
    char err[96];               /* "" on success, otherwise "<stage>:<esp_err_name>" */
} sdcard_format_result_t;

/**
 * @brief Probe the card and try to mount it at /sdcard.
 *
 * Never fatal: on any failure the status is filled with what was learned and
 * ESP_OK with mounted=false is returned.  Never formats the card (exFAT cards
 * are detected and left untouched, docs/protocol.md v1.2 §3.7).
 */
esp_err_t sdcard_init(void);

/** Thread-safe copy of the current card state (memset-safe defaults). */
void sdcard_get_status(sdcard_status_t *out);

bool sdcard_is_mounted(void);

/**
 * @brief Unmount (when mounted) and run the full probe/mount again.
 *
 * Used by the `sd_remount` command so a freshly inserted or freshly formatted
 * card is picked up without rebooting the board.
 */
esp_err_t sdcard_remount(void);

/**
 * @brief DESTRUCTIVE: format the card as FAT32 and mount it.
 *
 * Only reachable through the `sd_format` command with an explicit
 * `"confirm":"ERASE"`.  Records the pre-format identity (CID/type/capacity/fs)
 * in @p out before touching the card, then formats via the IDF
 * `format_if_mount_failed` path (FatFs f_mkfs), remounts and fills the "after"
 * fields.  On failure it still tries a normal remount so the card is not left
 * in an unmounted limbo.
 */
esp_err_t sdcard_format_fat32(sdcard_format_result_t *out);

/**
 * @brief Write + read + verify `bytes` on the mounted card and measure speed.
 *
 * Runs on a private task (callers must not invoke this from the UART RX task,
 * it can take seconds).  @p err receives "path:reason" text on failure.
 *
 * @return ESP_OK when the file was written, read back and verified.
 */
esp_err_t sdcard_run_test(uint32_t bytes, uint32_t *w_kbps, uint32_t *r_kbps,
                          bool *verify, char *err, size_t err_len);
