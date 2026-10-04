idf_component_register(SRCS "keryx_net.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_app_format esp_timer heap lwip keryx_console)
