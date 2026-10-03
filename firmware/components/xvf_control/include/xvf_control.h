/*
 * XVF3800 control over I2C: the same resource/command parameters as the USB control protocol (tools/xvf), see
 * docs/respeaker-flex-xvf3800.md for their meaning.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "esp_err.h"

#define XVF_I2C_ADDR 0x2C

// AEC_ASROUTGAIN: fixed gain on the ASR output (float, linear, 0..1000, 1.0 after reset)
#define XVF_AEC_RESID 33
#define XVF_AEC_ASROUTGAIN 36

esp_err_t xvf_control_init(gpio_num_t sda, gpio_num_t scl);

// Write {resid, cmd | 0x80, length + 1}, then read back a status byte followed by the payload.
esp_err_t xvf_read(uint8_t resid, uint8_t cmd, void *out, size_t len);

// Write {resid, cmd, length, payload}. The chip does not acknowledge writes; read the value back to check.
esp_err_t xvf_write(uint8_t resid, uint8_t cmd, const void *data, size_t len);

// Writes a float parameter and reads it back; ESP_ERR_INVALID_RESPONSE if the chip reports something else.
esp_err_t xvf_set_float(uint8_t resid, uint8_t cmd, float value);

// Sets a float parameter from a background task, retrying until the chip reads it back: after power-up the
// XVF3800 boots more slowly than the ESP32 and does not answer at first. `name` is only for the log.
void xvf_set_float_when_ready(uint8_t resid, uint8_t cmd, float value, const char *name);
