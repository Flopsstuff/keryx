idf_component_register(SRCS "keryx_console.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES espressif__tinyusb hal esp_system esp_timer log)
