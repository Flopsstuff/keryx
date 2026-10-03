idf_component_register(SRCS "xvf_control.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_driver_gpio
                       PRIV_REQUIRES esp_driver_i2c)
