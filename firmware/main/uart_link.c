#include "uart_link.h"

#include <string.h>

#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "uart_link";

/* protocol.md §1 */
#define LINK_UART_NUM      UART_NUM_0
#define LINK_TX_GPIO       37
#define LINK_RX_GPIO       38
#define LINK_BAUD_RATE     115200
#define LINK_RX_BUF_SIZE   4096
#define LINK_TX_BUF_SIZE   2048

/* Deep enough that a burst of frames never blocks the driver ISR. */
#define LINK_EVENT_QUEUE_DEPTH 32

static SemaphoreHandle_t s_tx_mutex;
static QueueHandle_t s_event_queue;
static volatile uint32_t s_tx_frames;

QueueHandle_t uart_link_event_queue(void)
{
    return s_event_queue;
}

esp_err_t uart_link_init(void)
{
    const uart_config_t cfg = {
        .baud_rate = LINK_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    s_event_queue = xQueueCreate(LINK_EVENT_QUEUE_DEPTH, sizeof(uart_event_t));
    if (s_event_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* UART0 has no VFS console reader in this project, so installing the
     * driver directly is safe (the IDF console VFS does not own the driver).
     * The event queue is what keeps RX latency at zero: the RX task sleeps on
     * it and wakes as soon as the driver has bytes (protocol.md §7). */
    ESP_RETURN_ON_ERROR(uart_driver_install(LINK_UART_NUM, LINK_RX_BUF_SIZE, LINK_TX_BUF_SIZE,
                                            LINK_EVENT_QUEUE_DEPTH, &s_event_queue, 0),
                        TAG, "uart_driver_install failed");
    ESP_RETURN_ON_ERROR(uart_param_config(LINK_UART_NUM, &cfg), TAG, "uart_param_config failed");
    ESP_RETURN_ON_ERROR(uart_set_pin(LINK_UART_NUM, LINK_TX_GPIO, LINK_RX_GPIO,
                                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE),
                        TAG, "uart_set_pin failed");

    s_tx_mutex = xSemaphoreCreateMutex();
    if (s_tx_mutex == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* Drop anything the ROM/bootloader already pushed to the RX FIFO. */
    uart_flush_input(LINK_UART_NUM);

    ESP_LOGI(TAG, "uart0 ready: tx=%d rx=%d %d 8N1", LINK_TX_GPIO, LINK_RX_GPIO, LINK_BAUD_RATE);
    return ESP_OK;
}

void uart_link_write_line(const char *line)
{
    if (line == NULL) {
        return;
    }

    const size_t len = strlen(line);
    const uint8_t nl = (uint8_t)'\n';

    if (s_tx_mutex != NULL) {
        if (xSemaphoreTake(s_tx_mutex, portMAX_DELAY) != pdTRUE) {
            return;
        }
    }
    if (len > 0) {
        uart_write_bytes(LINK_UART_NUM, line, len);
    }
    uart_write_bytes(LINK_UART_NUM, &nl, 1);
    if (s_tx_mutex != NULL) {
        xSemaphoreGive(s_tx_mutex);
    }
    s_tx_frames++;
}

uint32_t uart_link_tx_frames(void)
{
    return s_tx_frames;
}

uint32_t uart_link_now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}