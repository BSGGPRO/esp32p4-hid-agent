/*
 * uart_link.h - raw UART0 byte link for the JSON-lines protocol.
 *
 * protocol.md §1: GPIO37 = TXD, GPIO38 = RXD (P4 side), 115200 8N1, no flow
 * control.  The link is driven through the UART driver, never through the VFS
 * console, and nothing is ever logged into it unless HID_AGENT_RX_DEBUG is on.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

/*
 * protocol.md §9.1: frames are limited to 512 bytes, but the line buffer must
 * hold at least 640 so that an over-long line is survived without overflowing.
 * A line that fills this buffer without a '\n' is dropped as a whole.
 */
#define UART_LINK_LINE_MAX 768

/** Install UART0 driver and configure pins for the protocol link. */
esp_err_t uart_link_init(void);

/**
 * UART event queue filled by the driver (UART_DATA / FIFO_OVF / BUFFER_FULL).
 *
 * protocol.md §7 latency ruling: the RX task must block on this queue instead
 * of calling uart_read_bytes(..., ticks_to_wait > 0).  ESP-IDF's
 * uart_read_bytes() loops `while (length)` and waits up to ticks_to_wait for
 * EVERY missing byte, so a 25 byte command read with a 256 byte request and a
 * 20 ms timeout stalls the parser for the full 20 ms.  Measured on hardware:
 * ping RTT 38 ms before, 6 ms after switching to this queue.
 *
 * Returns NULL before uart_link_init() succeeded.
 */
QueueHandle_t uart_link_event_queue(void);

/** Write one frame: `line` followed by '\n'.  Thread safe. */
void uart_link_write_line(const char *line);

/** protocol.md v1.2 §3.6 uart.tx_ok: frames written to UART0 since boot. */
uint32_t uart_link_tx_frames(void);

/** Milliseconds since boot (esp_timer based), same base as the "t" field. */
uint32_t uart_link_now_ms(void);