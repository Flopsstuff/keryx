idf_component_register(SRCS "main.c"
                       PRIV_REQUIRES esp_driver_i2s esp_driver_gpio esp_timer esp_netif nvs_flash xvf_control keryx_wakeword
                                     keryx_console keryx_net keryx_link keryx_panel)
