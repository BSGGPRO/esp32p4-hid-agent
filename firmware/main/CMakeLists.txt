idf_component_register(
    SRCS
        "app_main.c"
        "uart_link.c"
        "keymap.c"
        "hid_device.c"
        "cmd_protocol.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_driver_uart
        esp_timer
        json
)