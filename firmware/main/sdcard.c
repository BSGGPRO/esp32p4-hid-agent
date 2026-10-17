/*
 * sdcard.c - TF card on SDMMC slot 0 with a FAT mount at /sdcard.
 *
 * docs/protocol.md v1.2 §3.7:
 *  - slot 0 with the P4 default IOMUX pins (CLK 43 / CMD 44 / D0 39 / D1 40 /
 *    D2 41 / D3 42), probed as 4-bit @ 20 MHz and falling back to 1-bit and
 *    400 kHz (a card that only answers on the narrow/slow bus is still usable
 *    and the speed report tells the truth);
 *  - probe first (sdmmc_card_init) so CID/CSD/capacity are reported even when
 *    the filesystem cannot be mounted;
 *  - detect exFAT from the raw boot sector, never format a card;
 *  - a missing/broken/exFAT card is reported, never fatal;
 *  - a card inserted after boot is picked up by the next sd_test (no reboot).
 */
#include "sdcard.h"

#include "pm_ctl.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/sdmmc_host.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sd_pwr_ctrl.h"
#include "sd_pwr_ctrl_by_on_chip_ldo.h"
#include "sdmmc_cmd.h"

static const char *TAG = "sdcard";

#define SD_SECTOR_SIZE   512
#define SD_TEST_CHUNK    (8 * 1024)
#define SD_MAX_FILES     5
#define SD_PROBE_RETRY_DELAY_MS 50

typedef enum {
    SD_FS_UNKNOWN = 0,
    SD_FS_FAT32,
    SD_FS_EXFAT,
    SD_FS_FAT12_16,
} sdcard_fs_t;

/* Bus settings are tried in this order; the first one the card answers on is
 * used for the mount as well (with an attempt to escalate to the fast bus).
 * The order follows the "safe first" rule: enumerate at 400 kHz single-bit,
 * then the board's normal 4-bit / 20 MHz, then 1-bit at full speed (a card that
 * only answers on a narrow/slow bus is a signal-integrity symptom, but it is
 * still better than reporting "no card"). */
typedef struct {
    int width;
    uint32_t freq_khz;
    const char *label;
} probe_cfg_t;

static const probe_cfg_t PROBE_CFGS[] = {
    {1, SDMMC_FREQ_PROBING, "1bit/400kHz"},
    {4, SDMMC_FREQ_DEFAULT, "4bit/20MHz"},
    {1, SDMMC_FREQ_DEFAULT, "1bit/20MHz"},
};
#define PROBE_CFG_COUNT ((int)(sizeof(PROBE_CFGS) / sizeof(PROBE_CFGS[0])))
#define SD_MOUNT_FAST_CFG (&PROBE_CFGS[1]) /* 4bit/20MHz */

static SemaphoreHandle_t s_lock;       /* guards s_status + s_mounted */
static SemaphoreHandle_t s_probe_lock; /* serializes probe/mount/remount */
static sdcard_status_t s_status;
static bool s_mounted;
static sdmmc_host_t s_host;            /* bus config that made the card answer */
static sdmmc_slot_config_t s_slot;
static bool s_have_cfg;
static char s_bus[24];                 /* for the (normally silent) log line */
static sd_pwr_ctrl_handle_t s_pwr_ctrl; /* on-chip LDO that powers the card IO */
static sdmmc_card_t *s_card;           /* card handle of the current mount */

/******************************* Small helpers ********************************/

static void status_reset(sdcard_status_t *st)
{
    memset(st, 0, sizeof(*st));
    st->free_mb = -1;
    snprintf(st->fs, sizeof(st->fs), "unknown");
    snprintf(st->type, sizeof(st->type), "unknown");
}

static void status_set_err(sdcard_status_t *st, const char *stage, esp_err_t err)
{
    snprintf(st->err, sizeof(st->err), "%s:%s", stage, esp_err_to_name(err));
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void make_host(sdmmc_host_t *host, const probe_cfg_t *cfg)
{
    *host = (sdmmc_host_t)SDMMC_HOST_DEFAULT();
    host->slot = SDMMC_HOST_SLOT_0;
    /* Keep the other capability bits (notably DEINIT_ARG, which FatFs relies on
     * when it unwinds a failed mount) and only pick the bus width. */
    host->flags &= ~(uint32_t)(SDMMC_HOST_FLAG_8BIT | SDMMC_HOST_FLAG_4BIT);
    if (cfg->width == 4) {
        host->flags |= SDMMC_HOST_FLAG_4BIT;
    } else {
        host->flags |= SDMMC_HOST_FLAG_1BIT;
    }
    host->max_freq_khz = cfg->freq_khz;
    host->pwr_ctrl_handle = s_pwr_ctrl;
}

/*
 * The P4 reference design powers the microSD IO from the on-chip LDO channel 4
 * (the vendor BSP in 1-Demo does exactly this with sd_pwr_ctrl_new_on_chip_ldo).
 * Without it the card is unpowered and never answers CMD0 - which on this board
 * shows up as card_init:ESP_ERR_TIMEOUT.
 */
static void ensure_pwr_ctrl(void)
{
    if (s_pwr_ctrl != NULL) {
        return;
    }
    const sd_pwr_ctrl_ldo_config_t ldo_config = {
        .ldo_chan_id = 4,
    };
    if (sd_pwr_ctrl_new_on_chip_ldo(&ldo_config, &s_pwr_ctrl) != ESP_OK) {
        s_pwr_ctrl = NULL;
        ESP_LOGD(TAG, "on-chip SD IO LDO (channel 4) not available");
    }
}

static void make_slot(sdmmc_slot_config_t *slot, int width)
{
    *slot = (sdmmc_slot_config_t)SDMMC_SLOT_CONFIG_DEFAULT();
    slot->width = width;
    /* The TF socket has no external pull-ups on the board; ask for the internal
     * ones. */
    slot->flags = SDMMC_SLOT_FLAG_INTERNAL_PULLUP;
}

/****************************** Filesystem probe *****************************/

static sdcard_fs_t fs_from_boot_sector(const uint8_t *sector)
{
    if (memcmp(sector + 3, "EXFAT   ", 8) == 0) {
        return SD_FS_EXFAT;
    }
    if (memcmp(sector + 82, "FAT32   ", 8) == 0) {
        return SD_FS_FAT32;
    }
    if (memcmp(sector + 54, "FAT", 3) == 0) {
        return SD_FS_FAT12_16;
    }
    return SD_FS_UNKNOWN;
}

/* Find the first non-empty primary partition entry of an MBR. */
static bool mbr_first_partition(const uint8_t *sector, uint32_t *out_lba, uint8_t *out_type)
{
    if (sector[510] != 0x55 || sector[511] != 0xAA) {
        return false;
    }
    for (int i = 0; i < 4; i++) {
        const uint8_t *entry = sector + 446 + i * 16;
        const uint8_t type = entry[4];
        const uint32_t lba = le32(entry + 8);
        const uint32_t sectors = le32(entry + 12);
        if (type != 0 && lba != 0 && sectors != 0) {
            *out_lba = lba;
            *out_type = type;
            return true;
        }
    }
    return false;
}

/* Read the first 512 B at @p lba; the card probe guarantees 512 B sectors. */
static esp_err_t read_sector(sdmmc_card_t *card, uint32_t lba, uint8_t *buf)
{
    return sdmmc_read_sectors(card, buf, (size_t)lba, 1);
}

static sdcard_fs_t probe_fs(sdmmc_card_t *card)
{
    static uint8_t sector[SD_SECTOR_SIZE] __attribute__((aligned(4)));

    if (read_sector(card, 0, sector) != ESP_OK) {
        return SD_FS_UNKNOWN;
    }

    uint32_t part_lba = 0;
    uint8_t part_type = 0;
    if (mbr_first_partition(sector, &part_lba, &part_type)) {
        if (read_sector(card, part_lba, sector) != ESP_OK) {
            return SD_FS_UNKNOWN;
        }
        const sdcard_fs_t fs = fs_from_boot_sector(sector);
        if (fs != SD_FS_UNKNOWN) {
            return fs;
        }
        /* Partition type 0x07 is exFAT or NTFS; without the boot signature we
         * cannot tell which, so stay honest and report "unknown". */
        ESP_LOGD(TAG, "partition type 0x%02X, no known boot signature", part_type);
        return SD_FS_UNKNOWN;
    }

    /* Super-floppy layout: the boot sector is at LBA 0. */
    return fs_from_boot_sector(sector);
}

static const char *fs_name(sdcard_fs_t fs)
{
    switch (fs) {
    case SD_FS_EXFAT:
        return "exfat";
    case SD_FS_FAT32:
        return "fat32";
    default:
        /* The v1.2 field only distinguishes fat32/exfat; FAT12/16 stays unknown */
        return "unknown";
    }
}

/******************************* Card info fill ******************************/

static void fill_card_info(sdmmc_card_t *card, sdcard_status_t *st)
{
    st->present = true;

    const uint32_t sector_size = card->csd.sector_size != 0 ? (uint32_t)card->csd.sector_size
                                                           : SD_SECTOR_SIZE;
    const uint64_t bytes = (uint64_t)(uint32_t)card->csd.capacity * sector_size;
    st->capacity_mb = (uint32_t)(bytes / (1024ull * 1024ull));

    const bool high_capacity = (card->ocr & (1u << 30)) != 0; /* CCS bit */
    if (card->is_mmc) {
        snprintf(st->type, sizeof(st->type), "MMC");
    } else if (high_capacity) {
        snprintf(st->type, sizeof(st->type), "%s",
                 st->capacity_mb > 32768u ? "SDXC" : "SDHC");
    } else {
        snprintf(st->type, sizeof(st->type), "SDSC");
    }

    char name[9];
    memcpy(name, card->cid.name, 8);
    name[8] = '\0';
    for (int i = 7; i >= 0 && (name[i] == ' ' || name[i] == '\0'); i--) {
        name[i] = '\0';
    }
    snprintf(st->cid, sizeof(st->cid),
             "mfg=0x%02X oem=0x%04X name=%s serial=0x%08X date=0x%04X rev=%d",
             card->cid.mfg_id & 0xFF, card->cid.oem_id & 0xFFFF, name,
             (unsigned)card->cid.serial, (unsigned)card->cid.date, card->cid.revision);
}

/***************************** Probe / mount logic ***************************/

/* One bring-up attempt: host init + slot init + card probe.  On success the
 * caller owns @p out_card and must both free() it and sdmmc_host_deinit(). */
static esp_err_t probe_once(const probe_cfg_t *cfg, sdmmc_host_t *out_host,
                            sdmmc_slot_config_t *out_slot, sdmmc_card_t **out_card)
{
    sdmmc_host_t host;
    sdmmc_slot_config_t slot;
    make_host(&host, cfg);
    make_slot(&slot, cfg->width);

    esp_err_t err = sdmmc_host_init();
    if (err != ESP_OK) {
        return err;
    }
    err = sdmmc_host_init_slot(SDMMC_HOST_SLOT_0, &slot);
    if (err != ESP_OK) {
        sdmmc_host_deinit();
        return err;
    }

    sdmmc_card_t *card = calloc(1, sizeof(sdmmc_card_t));
    if (card == NULL) {
        sdmmc_host_deinit();
        return ESP_ERR_NO_MEM;
    }

    err = sdmmc_card_init(&host, card);
    if (err != ESP_OK) {
        free(card);
        sdmmc_host_deinit();
        return err;
    }

    *out_host = host;
    *out_slot = slot;
    *out_card = card;
    return ESP_OK;
}

static void refresh_free_mb(sdcard_status_t *st)
{
    uint64_t total_bytes = 0;
    uint64_t free_bytes = 0;
    if (esp_vfs_fat_info(SDCARD_MOUNT_POINT, &total_bytes, &free_bytes) == ESP_OK) {
        st->free_mb = (int64_t)(free_bytes / (1024ull * 1024ull));
    }
}

/* Mount with @p cfg.  The helper brings the host up itself, which is why the
 * probe has to deinit it first.  @p allow_format is only true for the
 * `sd_format` command: it flips esp_vfs_fat's format_if_mount_failed so the
 * IDF FatFs path (f_mkfs) turns the card into FAT32. */
static esp_err_t mount_card(const probe_cfg_t *cfg, sdcard_status_t *st, bool *mounted,
                            bool allow_format)
{
    make_host(&s_host, cfg);
    make_slot(&s_slot, cfg->width);
    snprintf(s_bus, sizeof(s_bus), "%s", cfg->label);

    const esp_vfs_fat_mount_config_t mount_config = {
        .format_if_mount_failed = allow_format, /* destructive: sd_format only */
        .max_files = SD_MAX_FILES,
        .allocation_unit_size = 16 * 1024,
        .disk_status_check_enable = false,
        .use_one_fat = false,
    };

    sdmmc_card_t *mounted_card = NULL;
    const esp_err_t err = esp_vfs_fat_sdmmc_mount(SDCARD_MOUNT_POINT, &s_host, &s_slot,
                                                 &mount_config, &mounted_card);
    if (err != ESP_OK) {
        status_set_err(st, "mount", err);
        return err;
    }

    s_card = mounted_card;
    *mounted = true;
    st->mounted = true;
    st->err[0] = '\0';
    snprintf(st->bus, sizeof(st->bus), "%s", cfg->label);
    refresh_free_mb(st);

    ESP_LOGI(TAG, "mounted at %s (%s): %u MB, free %lld MB, %s %s", SDCARD_MOUNT_POINT,
             s_bus, (unsigned)st->capacity_mb, (long long)st->free_mb, st->type, st->fs);
    return ESP_OK;
}

/* Unmount the current card (no-op when not mounted).  Caller holds s_probe_lock. */
static void unmount_card(void)
{
    if (s_card != NULL) {
        esp_vfs_fat_sdcard_unmount(SDCARD_MOUNT_POINT, s_card);
        s_card = NULL;
    }
    if (s_lock != NULL) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_mounted = false;
        xSemaphoreGive(s_lock);
    }
}

static esp_err_t bring_up(sdcard_status_t *st, bool *mounted);

/* Bring-up wrapper: the adaptive PM governor must not change the CPU clock in
 * the middle of a card probe, so hold max frequency for the whole operation. */
static esp_err_t bring_up_guarded(sdcard_status_t *st, bool *mounted)
{
    pm_ctl_hold(PM_REASON_SD_IO);
    const esp_err_t err = bring_up(st, mounted);
    pm_ctl_release();
    return err;
}

/* Full bring-up, called with s_probe_lock held.  Never blocks on a lock and
 * never panics: every failure ends up in @p st / @p mounted. */
static esp_err_t bring_up(sdcard_status_t *st, bool *mounted)
{
    *mounted = false;
    s_have_cfg = false;
    status_reset(st);
    st->cd_known = false; /* this socket has no dedicated card-detect switch */

    ensure_pwr_ctrl();
    /* Let the card power rail settle after the LDO is enabled (the board is
     * passively cooled and the TF slot is only probed on demand). */
    vTaskDelay(pdMS_TO_TICKS(100));

    esp_err_t last = ESP_ERR_TIMEOUT;
    for (int i = 0; i < PROBE_CFG_COUNT; i++) {
        sdmmc_card_t *card = NULL;
        sdmmc_host_t host;
        sdmmc_slot_config_t slot;

        const esp_err_t err = probe_once(&PROBE_CFGS[i], &host, &slot, &card);
        if (st->try_count < SDCARD_MAX_TRIES) {
            sdcard_try_t *t = &st->tries[st->try_count++];
            snprintf(t->bus, sizeof(t->bus), "%s", PROBE_CFGS[i].label);
            snprintf(t->err, sizeof(t->err), "%s",
                     err == ESP_OK ? "ok" : esp_err_to_name(err));
        }
        if (err != ESP_OK) {
            ESP_LOGD(TAG, "probe %s failed: %s", PROBE_CFGS[i].label, esp_err_to_name(err));
            last = err;
            if (i + 1 < PROBE_CFG_COUNT) {
                vTaskDelay(pdMS_TO_TICKS(SD_PROBE_RETRY_DELAY_MS));
            }
            continue;
        }

        sdmmc_host_t saved_host = host;
        sdmmc_slot_config_t saved_slot = slot;
        snprintf(s_bus, sizeof(s_bus), "%s", PROBE_CFGS[i].label);

        fill_card_info(card, st);
        const sdcard_fs_t fs = probe_fs(card);
        snprintf(st->fs, sizeof(st->fs), "%s", fs_name(fs));
        snprintf(st->bus, sizeof(st->bus), "%s", PROBE_CFGS[i].label);
        free(card);
        sdmmc_host_deinit();

        s_host = saved_host;
        s_slot = saved_slot;
        s_have_cfg = true;

        if (fs == SD_FS_EXFAT) {
            /* IDF FatFs has no exFAT driver.  NEVER format the card (it is the
             * user's 64 GB card, most likely exFAT) - report and stop here. */
            snprintf(st->err, sizeof(st->err), "exfat_not_supported");
            ESP_LOGW(TAG, "card is exFAT: not mounted (no exFAT support, not formatting)");
            return ESP_ERR_NOT_SUPPORTED;
        }

        /* Prefer the fast bus for the actual mount; if that fails (it can if the
         * card only answered the slow probe), retry with the probing config. */
        esp_err_t merr = mount_card(SD_MOUNT_FAST_CFG, st, mounted, false);
        if (merr != ESP_OK && PROBE_CFGS[i].freq_khz != SD_MOUNT_FAST_CFG->freq_khz) {
            merr = mount_card(&PROBE_CFGS[i], st, mounted, false);
        }
        return merr;
    }

    status_set_err(st, "card_init", last);
    ESP_LOGD(TAG, "no card answered on any bus config: %s", esp_err_to_name(last));
    return last;
}

/* Publish the current status under s_lock (short critical section). */
static void publish(const sdcard_status_t *st, bool mounted)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_status = *st;
    s_mounted = mounted;
    xSemaphoreGive(s_lock);
}

/* Re-probe on demand (card inserted after boot).  Called from the sd_test task,
 * so a few hundred ms of probing cannot stall the UART command path. */
static bool try_remount(void)
{
    xSemaphoreTake(s_probe_lock, portMAX_DELAY);
    if (!s_mounted) {
        sdcard_status_t st;
        bool mounted = false;
        bring_up_guarded(&st, &mounted);
        publish(&st, mounted);
    }
    const bool mounted = s_mounted;
    xSemaphoreGive(s_probe_lock);
    return mounted;
}

/********************************* Public API ********************************/

esp_err_t sdcard_init(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
        s_probe_lock = xSemaphoreCreateMutex();
        if (s_lock == NULL || s_probe_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
        status_reset(&s_status);
    }

    xSemaphoreTake(s_probe_lock, portMAX_DELAY);
    sdcard_status_t st;
    bool mounted = false;
    bring_up_guarded(&st, &mounted);
    publish(&st, mounted);
    xSemaphoreGive(s_probe_lock);

    /* A missing/broken/unsupported card is a normal state, not a boot failure:
     * the reason is in sdcard_get_status()->err, and the agent must keep
     * working (HID does not depend on the card). */
    return ESP_OK;
}

void sdcard_get_status(sdcard_status_t *out)
{
    if (out == NULL) {
        return;
    }
    if (s_lock == NULL) {
        memset(out, 0, sizeof(*out));
        out->free_mb = -1;
        snprintf(out->fs, sizeof(out->fs), "unknown");
        snprintf(out->type, sizeof(out->type), "unknown");
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    *out = s_status;
    xSemaphoreGive(s_lock);
}

bool sdcard_is_mounted(void)
{
    return s_mounted;
}

esp_err_t sdcard_remount(void)
{
    if (s_probe_lock == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_probe_lock, portMAX_DELAY);
    unmount_card();
    sdcard_status_t st;
    bool mounted = false;
    const esp_err_t err = bring_up_guarded(&st, &mounted);
    publish(&st, mounted);
    xSemaphoreGive(s_probe_lock);
    return err;
}

esp_err_t sdcard_format_fat32(sdcard_format_result_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof(*out));
    out->free_mb_after = -1;
    if (s_probe_lock == NULL) {
        snprintf(out->err, sizeof(out->err), "not_initialized");
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_probe_lock, portMAX_DELAY);

    /* Erasing + writing the FATs must not race a DFS clock change. */
    pm_ctl_hold(PM_REASON_SD_IO);

    /* ---- 1. pre-format evidence (CID / type / capacity / current fs) ---- */
    sdcard_status_t st;
    bool mounted = false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    st = s_status;
    xSemaphoreGive(s_lock);
    if (!st.present) {
        unmount_card();
        bring_up_guarded(&st, &mounted);
        publish(&st, mounted);
    }
    snprintf(out->before_cid, sizeof(out->before_cid), "%s", st.cid);
    snprintf(out->before_type, sizeof(out->before_type), "%s", st.type);
    snprintf(out->before_fs, sizeof(out->before_fs), "%s", st.fs);
    out->before_capacity_mb = st.capacity_mb;

    if (!st.present) {
        snprintf(out->err, sizeof(out->err), "card_init:no_card");
        ESP_LOGW(TAG, "sd_format refused: no card answered");
        pm_ctl_release();
        xSemaphoreGive(s_probe_lock);
        return ESP_ERR_INVALID_STATE;
    }

    /* ---- 2. format (unmount first; the helper refuses to format a mounted fs) */
    unmount_card();
    make_host(&s_host, SD_MOUNT_FAST_CFG);
    make_slot(&s_slot, SD_MOUNT_FAST_CFG->width);

    const esp_vfs_fat_mount_config_t mount_config = {
        .format_if_mount_failed = true, /* DESTRUCTIVE: sd_format only */
        .max_files = SD_MAX_FILES,
        .allocation_unit_size = 16 * 1024,
        .disk_status_check_enable = false,
        .use_one_fat = false,
    };
    sdmmc_card_t *card = NULL;
    esp_err_t err = esp_vfs_fat_sdmmc_mount(SDCARD_MOUNT_POINT, &s_host, &s_slot,
                                            &mount_config, &card);

    if (err == ESP_OK) {
        s_card = card;
        sdcard_status_t after;
        status_reset(&after);
        after.cd_known = false;
        after.present = true;
        after.mounted = true;
        fill_card_info(card, &after);
        const sdcard_fs_t fs = probe_fs(card);
        snprintf(after.fs, sizeof(after.fs), "%s", fs_name(fs));
        snprintf(after.bus, sizeof(after.bus), "%s", SD_MOUNT_FAST_CFG->label);
        after.try_count = 1;
        snprintf(after.tries[0].bus, sizeof(after.tries[0].bus), "%s",
                 SD_MOUNT_FAST_CFG->label);
        snprintf(after.tries[0].err, sizeof(after.tries[0].err), "%s", "ok");
        refresh_free_mb(&after);
        publish(&after, true);

        out->ok = true;
        out->mounted_after = true;
        out->capacity_mb_after = after.capacity_mb;
        out->free_mb_after = after.free_mb;
        snprintf(out->fs_after, sizeof(out->fs_after), "%s", after.fs);
        snprintf(out->bus, sizeof(out->bus), "%s", after.bus);
        ESP_LOGI(TAG, "formatted as %s: %u MB, free %lld MB", after.fs,
                 (unsigned)after.capacity_mb, (long long)after.free_mb);
    } else {
        snprintf(out->err, sizeof(out->err), "format:%s", esp_err_to_name(err));
        ESP_LOGW(TAG, "format failed: %s", esp_err_to_name(err));
        /* Never leave the card in limbo: try a normal probe/mount again. */
        sdcard_status_t st2;
        bool m2 = false;
        bring_up(&st2, &m2);
        publish(&st2, m2);
        out->mounted_after = m2;
        snprintf(out->fs_after, sizeof(out->fs_after), "%s", st2.fs);
        out->capacity_mb_after = st2.capacity_mb;
        out->free_mb_after = st2.free_mb;
        snprintf(out->bus, sizeof(out->bus), "%s", st2.bus);
    }

    pm_ctl_release();
    xSemaphoreGive(s_probe_lock);
    return err;
}

esp_err_t sdcard_run_test(uint32_t bytes, uint32_t *w_kbps, uint32_t *r_kbps,
                          bool *verify, char *err, size_t err_len)
{
    if (w_kbps != NULL) {
        *w_kbps = 0;
    }
    if (r_kbps != NULL) {
        *r_kbps = 0;
    }
    if (verify != NULL) {
        *verify = false;
    }
    if (err != NULL && err_len > 0) {
        err[0] = '\0';
    }

    if (!s_mounted && !try_remount()) {
        snprintf(err, err_len, "%s", "not_mounted");
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_have_cfg) {
        snprintf(err, err_len, "%s", "not_mounted");
        return ESP_ERR_INVALID_STATE;
    }

    /* Internal DMA-capable RAM on purpose: the SDMMC driver DMAs directly from
     * these buffers, and a PSRAM buffer would need cache maintenance on every
     * chunk (on this early P4 that path is what hammered the interrupt
     * watchdog). */
    uint8_t *wbuf = heap_caps_malloc(SD_TEST_CHUNK, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    uint8_t *rbuf = heap_caps_malloc(SD_TEST_CHUNK, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
    if (wbuf == NULL || rbuf == NULL) {
        free(wbuf);
        free(rbuf);
        snprintf(err, err_len, "%s", "no_mem");
        return ESP_ERR_NO_MEM;
    }

    esp_err_t result = ESP_OK;

    for (size_t i = 0; i < SD_TEST_CHUNK; i++) {
        wbuf[i] = (uint8_t)((i * 31u) + 7u);
    }

    /* ---- write ---- */
    const uint64_t t0 = (uint64_t)esp_timer_get_time();
    /* No clock change while the card is being written or read. */
    pm_ctl_hold(PM_REASON_SD_IO);

    FILE *f = fopen(SDCARD_TEST_PATH, "wb");
    if (f == NULL) {
        snprintf(err, err_len, "%s", "fopen_write_failed");
        result = ESP_FAIL;
        goto out;
    }
    uint32_t written = 0;
    while (written < bytes) {
        const size_t chunk = (bytes - written) < SD_TEST_CHUNK ? (bytes - written)
                                                               : SD_TEST_CHUNK;
        if (fwrite(wbuf, 1, chunk, f) != chunk) {
            snprintf(err, err_len, "%s", "fwrite_failed");
            result = ESP_FAIL;
            fclose(f);
            goto out;
        }
        written += (uint32_t)chunk;
        /* Yield so the IDLE tasks (and the task watchdog) stay fed while a
         * multi-second write is in progress. */
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    if (fclose(f) != 0) {
        snprintf(err, err_len, "%s", "fclose_write_failed");
        result = ESP_FAIL;
        goto out;
    }
    const uint64_t t1 = (uint64_t)esp_timer_get_time();

    /* ---- read back + verify ---- */
    const uint64_t t2 = (uint64_t)esp_timer_get_time();
    f = fopen(SDCARD_TEST_PATH, "rb");
    if (f == NULL) {
        snprintf(err, err_len, "%s", "fopen_read_failed");
        result = ESP_FAIL;
        goto out;
    }
    uint32_t read_total = 0;
    bool ok = true;
    while (read_total < bytes) {
        const size_t chunk = (bytes - read_total) < SD_TEST_CHUNK ? (bytes - read_total)
                                                                   : SD_TEST_CHUNK;
        const size_t got = fread(rbuf, 1, chunk, f);
        if (got != chunk) {
            snprintf(err, err_len, "%s", "fread_failed");
            result = ESP_FAIL;
            fclose(f);
            goto out;
        }
        for (size_t i = 0; i < chunk; i++) {
            if (rbuf[i] != wbuf[i]) {
                ok = false;
                break;
            }
        }
        if (!ok) {
            break;
        }
        read_total += (uint32_t)chunk;
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    fclose(f);
    const uint64_t t3 = (uint64_t)esp_timer_get_time();

    if (verify != NULL) {
        *verify = ok;
    }
    if (!ok) {
        snprintf(err, err_len, "%s", "verify_mismatch");
        result = ESP_FAIL;
        goto out;
    }

    const uint64_t write_us = (t1 > t0) ? (t1 - t0) : 1;
    const uint64_t read_us = (t3 > t2) ? (t3 - t2) : 1;
    const uint32_t wr = (uint32_t)(((uint64_t)bytes * 1000000ull) / (1024ull * write_us));
    const uint32_t rd = (uint32_t)(((uint64_t)bytes * 1000000ull) / (1024ull * read_us));

    if (w_kbps != NULL) {
        *w_kbps = wr;
    }
    if (r_kbps != NULL) {
        *r_kbps = rd;
    }

    /* Cache the speed and refresh the free-space figure for the next stats. */
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_status.has_speed = true;
    s_status.speed_write_kbps = wr;
    s_status.speed_read_kbps = rd;
    refresh_free_mb(&s_status);
    xSemaphoreGive(s_lock);

    ESP_LOGI(TAG, "sd_test: %u B, write %u kB/s, read %u kB/s, verify=%d",
             (unsigned)bytes, (unsigned)wr, (unsigned)rd, (int)ok);

out:
    pm_ctl_release();
    free(wbuf);
    free(rbuf);
    return result;
}
