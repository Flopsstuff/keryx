idf_component_register(SRCS "main.c"
                       PRIV_REQUIRES esp_driver_i2s esp_driver_gpio esp_timer xvf_control keryx_wakeword
                                     keryx_console keryx_net)
