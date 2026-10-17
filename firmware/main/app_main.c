/*
 * app_main.c - UART0 raw line reader for the JSON-lines protocol.
 *
 * protocol.md §1: bytes are read from UART0 through the UART driver (never the
 * VFS console) and assembled into '{...}' lines by hand.  Over-long lines are
 * dropped as a whole (protocol.md §9.1).  Nothing is ever logged on UART0
 * unless HID_AGENT_RX_DEBUG is enabled.
 */
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "cmd_protocol.h"
#include "hid_device.h"
#include "uart_link.h"

static const char *TAG = "hid_agent";

#define RX_TASK_NAME  "hid_rx"
#define RX_TASK_STACK 5120
#define RX_TASK_PRIO  5
#define RX_CHUNK_SIZE 256

static void rx_task(void *arg)
{
    (void)arg;

    /* >= 640 bytes as demanded by protocol.md §9.1 */
    static char line[UART_LINK_LINE_MAX + 1];
    size_t len = 0;
    bool overflow = false;
    uint8_t chunk[RX_CHUNK_SIZE];

    QueueHandle_t events = uart_link_event_queue();
    if (events == NULL) {
        ESP_LOGE(TAG, "uart event queue missing");
        vTaskDelete(NULL);
        return;
    }

    for (;;) {
        uart_event_t evt;
        if (xQueueReceive(events, &evt, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        if (evt.type == UART_FIFO_OVF || evt.type == UART_BUFFER_FULL) {
            /* Bytes were lost, so the partial line can never be trusted. */
            uart_flush_input(UART_NUM_0);
            xQueueReset(events);
            len = 0;
            overflow = false;
            proto_note_line_dropped();
            continue;
        }
        if (evt.type != UART_DATA) {
            continue;
        }

        /*
         * protocol.md §7: take exactly what the driver has buffered and parse it
         * immediately.  ticks_to_wait MUST stay 0 - ESP-IDF's uart_read_bytes()
         * loops `while (length)` and waits up to ticks_to_wait for every missing
         * byte, so a short frame read with a larger request would stall the
         * parser for the whole timeout (measured: 20 ms per command).
         */
        size_t remaining = evt.size;
        while (remaining > 0) {
            const size_t want = remaining < sizeof(chunk) ? remaining : sizeof(chunk);
            const int read = uart_read_bytes(UART_NUM_0, chunk, want, 0);
            if (read <= 0) {
                break;
            }
            remaining -= (size_t)read;

            for (int i = 0; i < read; i++) {
                const char c = (char)chunk[i];

                if (c == '\n') {
                    if (overflow) {
                        /* protocol.md §9.1: drop the whole line, no response at all */
                        proto_note_line_dropped();
                    } else if (len > 0) {
                        line[len] = '\0';
                        proto_handle_line(line, len);
                    }
                    len = 0;
                    overflow = false;
                } else if (c == '\r') {
                    /* tolerate CRLF, never part of the frame */
                } else if (len < UART_LINK_LINE_MAX) {
                    line[len++] = c;
                } else {
                    overflow = true;
                }
            }
        }
    }
}

void app_main(void)
{
    /* protocol.md §1: silence every log first, before anything can print. */
    esp_log_level_set("*", ESP_LOG_NONE);
#if CONFIG_HID_AGENT_RX_DEBUG
    esp_log_level_set("hid_agent", ESP_LOG_INFO);
    esp_log_level_set("hid_device", ESP_LOG_INFO);
    esp_log_level_set("cmd_proto", ESP_LOG_INFO);
    esp_log_level_set("uart_link", ESP_LOG_INFO);
#endif

    esp_err_t err = uart_link_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_link_init failed: %d", (int)err);
    }

    err = hid_device_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "hid_device_init failed: %d", (int)err);
    }

    proto_init();
    proto_event_boot();

    if (xTaskCreate(rx_task, RX_TASK_NAME, RX_TASK_STACK, NULL, RX_TASK_PRIO, NULL) != pdPASS) {
        ESP_LOGE(TAG, "failed to start %s", RX_TASK_NAME);
    }

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}