#include "keryx_console.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/usb_serial_jtag_ll.h"
#include "soc/rtc_cntl_reg.h"
#include "tusb.h"

static const char *TAG = "console";

#define POLL_MS 20
#define LOG_LINE_MAX 256

static SemaphoreHandle_t write_lock;
static const char *banner_text;

static void write_bytes(const char *data, size_t len)
{
    if (!tud_cdc_connected() || xSemaphoreTake(write_lock, pdMS_TO_TICKS(10)) != pdTRUE) {
        return;
    }
    // what does not fit into the TX buffer is dropped: the host is not keeping up
    tud_cdc_write(data, len);
    tud_cdc_write_flush();
    xSemaphoreGive(write_lock);
}

static void write_vprintf(const char *fmt, va_list args)
{
    char line[LOG_LINE_MAX];
    int n = vsnprintf(line, sizeof(line), fmt, args);
    if (n > 0) {
        write_bytes(line, n < (int)sizeof(line) ? (size_t)n : sizeof(line) - 1);
    }
}

void keryx_console_printf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    write_vprintf(fmt, args);
    va_end(args);
}

static int log_vprintf(const char *fmt, va_list args)
{
    write_vprintf(fmt, args);
    return 0;
}

static void restart_into_bootloader(void)
{
    ESP_LOGI(TAG, "restarting into the ROM bootloader");
    vTaskDelay(pdMS_TO_TICKS(100));  // let the line out
    tud_disconnect();
    vTaskDelay(pdMS_TO_TICKS(50));
    // The ROM's download mode talks over the USB serial/JTAG controller: hand it back the PHY that TinyUSB took.
    // Both registers live in the RTC domain and survive the restart.
    usb_serial_jtag_ll_phy_enable_external(false);
    REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    esp_restart();
}

static void run_command(const char *cmd)
{
    if (strcmp(cmd, "bootloader") == 0) {
        restart_into_bootloader();
    } else if (strcmp(cmd, "reboot") == 0) {
        ESP_LOGI(TAG, "rebooting");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    } else if (cmd[0] != '\0') {
        keryx_console_printf("unknown command: %s (try: bootloader, reboot)\n", cmd);
    }
}

static void console_task(void *arg)
{
    char cmd[64];
    size_t len = 0;
    bool was_connected = false;
    for (;;) {
        bool connected = tud_cdc_connected();
        if (connected && !was_connected) {
            keryx_console_printf("%s, up %lld s\n", banner_text, esp_timer_get_time() / 1000000);
        }
        was_connected = connected;

        while (tud_cdc_available()) {
            char c;
            if (tud_cdc_read(&c, 1) != 1) {
                break;
            }
            if (c == '\r' || c == '\n') {
                cmd[len] = '\0';
                run_command(cmd);
                len = 0;
            } else if (len < sizeof(cmd) - 1) {
                cmd[len++] = c;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

esp_err_t keryx_console_start(const char *banner)
{
    banner_text = banner;
    write_lock = xSemaphoreCreateMutex();
    if (write_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(console_task, "console", 3072, NULL, 2, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    esp_log_set_vprintf(log_vprintf);
    return ESP_OK;
}
