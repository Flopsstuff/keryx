idf_component_register(SRCS "keryx_link.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES espressif__esp_websocket_client json esp_wifi esp_timer esp_app_format
                                     keryx_net keryx_console)
