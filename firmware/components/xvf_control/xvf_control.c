#include "xvf_control.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "xvf";

#define STATUS_OK 0
#define STATUS_RETRY 64
#define MAX_PAYLOAD 64
#define RETRY_MS 500  // between attempts of xvf_set_float_when_ready

static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t xvf, codec;

esp_err_t xvf_control_init(gpio_num_t sda, gpio_num_t scl)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = false, // the board has 4.7 kΩ pull-ups to VDDIO
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &bus), TAG, "I2C bus");

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDR,
        .scl_speed_hz = 100000,
        .scl_wait_us = 20000, // the XVF3800 stretches the clock while it prepares a response
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus, &dev_cfg, &xvf), TAG, "XVF3800");
    dev_cfg.device_address = CODEC_I2C_ADDR;
    dev_cfg.scl_wait_us = 0;
    return i2c_master_bus_add_device(bus, &dev_cfg, &codec);
}

i2c_master_bus_handle_t xvf_i2c_bus(void)
{
    return bus;
}

esp_err_t xvf_read(uint8_t resid, uint8_t cmd, void *out, size_t len)
{
    uint8_t request[3] = {resid, (uint8_t)(cmd | 0x80), (uint8_t)(len + 1)};
    uint8_t response[MAX_PAYLOAD + 1];
    ESP_RETURN_ON_FALSE(len <= MAX_PAYLOAD, ESP_ERR_INVALID_SIZE, TAG, "read too long");
    for (int attempt = 0; attempt < 50; attempt++) {
        ESP_RETURN_ON_ERROR(i2c_master_transmit(xvf, request, sizeof(request), 100), TAG, "request %u:%u", resid, cmd);
        ESP_RETURN_ON_ERROR(i2c_master_receive(xvf, response, len + 1, 100), TAG, "response %u:%u", resid, cmd);
        if (response[0] == STATUS_OK) {
            memcpy(out, response + 1, len);
            return ESP_OK;
        }
        if (response[0] != STATUS_RETRY) {
            ESP_LOGW(TAG, "resid %u cmd %u: status %u", resid, cmd, response[0]);
            return ESP_FAIL;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t xvf_write(uint8_t resid, uint8_t cmd, const void *data, size_t len)
{
    uint8_t request[3 + MAX_PAYLOAD] = {resid, cmd, (uint8_t)len};
    ESP_RETURN_ON_FALSE(len <= MAX_PAYLOAD, ESP_ERR_INVALID_SIZE, TAG, "write too long");
    memcpy(request + 3, data, len);
    return i2c_master_transmit(xvf, request, 3 + len, 100);
}

esp_err_t codec_read(uint8_t reg, uint8_t *value)
{
    // one transaction with a repeated start: after a stop the codec reads from the next register
    return i2c_master_transmit_receive(codec, &reg, 1, value, 1, 50);
}

esp_err_t codec_write(uint8_t reg, uint8_t value)
{
    const uint8_t request[2] = {reg, value};
    return i2c_master_transmit(codec, request, sizeof(request), 50);
}

esp_err_t xvf_set_float(uint8_t resid, uint8_t cmd, float value)
{
    float check;
    ESP_RETURN_ON_ERROR(xvf_write(resid, cmd, &value, sizeof(value)), TAG, "write %u:%u", resid, cmd);
    ESP_RETURN_ON_ERROR(xvf_read(resid, cmd, &check, sizeof(check)), TAG, "read back %u:%u", resid, cmd);
    if (fabsf(check - value) > 1e-4f * fmaxf(1.0f, fabsf(value))) {
        ESP_LOGW(TAG, "resid %u cmd %u: wrote %g, reads %g", resid, cmd, value, check);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

typedef struct {
    uint8_t resid, cmd;
    float value;
    const char *name;
} pending_t;

static void set_when_ready_task(void *arg)
{
    pending_t *p = arg;
    int attempts = 1;
    while (xvf_set_float(p->resid, p->cmd, p->value) != ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(RETRY_MS));
        attempts++;
    }
    ESP_LOGI(TAG, "%s set to %g (attempt %d)", p->name, p->value, attempts);
    free(p);
    vTaskDelete(NULL);
}

void xvf_set_float_when_ready(uint8_t resid, uint8_t cmd, float value, const char *name)
{
    pending_t *p = malloc(sizeof(*p));
    if (p == NULL) {
        ESP_LOGE(TAG, "no memory to set %s", name);
        return;
    }
    *p = (pending_t){resid, cmd, value, name};
    xTaskCreate(set_when_ready_task, "xvf_set", 3072, p, 2, NULL);
}
