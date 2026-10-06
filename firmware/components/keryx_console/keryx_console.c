#include "keryx_console.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/usb_serial_jtag_ll.h"
#include "soc/rtc_cntl_reg.h"
#include "tusb.h"

static const char *TAG = "console";

#define POLL_MS 20
#define LOG_LINE_MAX 256
#define REMOTE_LINE_MAX 128
#define REMOTE_OUTPUT_MAX 3072
#define REMOTE_QUEUE 2

typedef struct {
    int id;
    char line[REMOTE_LINE_MAX];
    keryx_console_done_fn done;
} remote_t;

static SemaphoreHandle_t write_lock;
static const char *banner_text;
static keryx_console_command_fn app_command;
static TaskHandle_t console_task_handle;
static QueueHandle_t remote_queue;
// while a submitted line runs: what the console task prints is also collected here
static char *capture;
static size_t capture_len;
static volatile bool capturing;

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
        n = n < (int)sizeof(line) ? n : (int)sizeof(line) - 1;
        write_bytes(line, n);
        if (capturing && xTaskGetCurrentTaskHandle() == console_task_handle) {
            size_t room = REMOTE_OUTPUT_MAX - 1 - capture_len;
            size_t take = (size_t)n < room ? (size_t)n : room;
            memcpy(capture + capture_len, line, take);
            capture_len += take;
            capture[capture_len] = '\0';
        }
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

// "log <tag|*> <none|error|warn|info|debug|verbose>"
static void set_log_level(const char *args)
{
    static const char *const LEVELS[] = {"none", "error", "warn", "info", "debug", "verbose"};
    char tag[32], level[16];
    if (sscanf(args, "%31s %15s", tag, level) == 2) {
        for (int i = 0; i < 6; i++) {
            if (strcmp(level, LEVELS[i]) == 0) {
                esp_log_level_set(tag, (esp_log_level_t)i);
                keryx_console_printf("ok log %s %s\n", tag, level);
                return;
            }
        }
    }
    keryx_console_printf("error usage: log <tag|*> <none|error|warn|info|debug|verbose>\n");
}

// "top [seconds]": the share of each core every task took over the interval, busiest first.
static void show_top(int seconds)
{
    UBaseType_t cap = uxTaskGetNumberOfTasks() + 8;
    TaskStatus_t *before = calloc(cap, sizeof(TaskStatus_t)), *after = calloc(cap, sizeof(TaskStatus_t));
    if (before == NULL || after == NULL) {
        free(before);
        free(after);
        keryx_console_printf("error out of memory\n");
        return;
    }
    configRUN_TIME_COUNTER_TYPE t0, t1;
    UBaseType_t n0 = uxTaskGetSystemState(before, cap, &t0);
    vTaskDelay(pdMS_TO_TICKS(seconds * 1000));
    UBaseType_t n1 = uxTaskGetSystemState(after, cap, &t1);
    float span = (float)(t1 - t0);
    float busy[2] = {100.0f, 100.0f};
    uint32_t delta[cap];
    for (UBaseType_t i = 0; i < n1; i++) {
        delta[i] = 0;
        for (UBaseType_t j = 0; j < n0; j++) {
            if (before[j].xHandle == after[i].xHandle) {
                delta[i] = after[i].ulRunTimeCounter - before[j].ulRunTimeCounter;
            }
        }
        if (strncmp(after[i].pcTaskName, "IDLE", 4) == 0) {
            int core = after[i].pcTaskName[4] - '0';
            if (core == 0 || core == 1) {
                busy[core] -= 100.0f * delta[i] / span;
            }
        }
    }
    keryx_console_printf("cpu over %d s: core 0 %.1f %%, core 1 %.1f %%, at %d MHz\n", seconds, busy[0], busy[1],
                         CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ);
    for (;;) {  // busiest first, skipping what took under 0.1 %
        UBaseType_t best = n1;
        for (UBaseType_t i = 0; i < n1; i++) {
            if (strncmp(after[i].pcTaskName, "IDLE", 4) != 0 && (best == n1 || delta[i] > delta[best])) {
                best = i;
            }
        }
        if (best == n1 || 100.0f * delta[best] / span < 0.1f) {
            break;
        }
        int core = after[best].xCoreID;
        keryx_console_printf("  %-16s core %s prio %2u %5.1f %%  stack free %u\n", after[best].pcTaskName,
                             core == 0 ? "0" : core == 1 ? "1" : "-", (unsigned)after[best].uxCurrentPriority,
                             100.0f * delta[best] / span, (unsigned)after[best].usStackHighWaterMark);
        delta[best] = 0;
    }
    keryx_console_printf("ok\n");
    free(before);
    free(after);
}

static void run_command(const char *cmd)
{
    if (strcmp(cmd, "bootloader") == 0) {
        restart_into_bootloader();
    } else if (strcmp(cmd, "reboot") == 0) {
        ESP_LOGI(TAG, "rebooting");
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    } else if (strcmp(cmd, "top") == 0 || strncmp(cmd, "top ", 4) == 0) {
        int seconds = cmd[3] ? atoi(cmd + 4) : 5;
        show_top(seconds > 0 && seconds <= 60 ? seconds : 5);
    } else if (strncmp(cmd, "log ", 4) == 0) {
        set_log_level(cmd + 4);
    } else if (cmd[0] != '\0' && (app_command == NULL || !app_command(cmd))) {
        keryx_console_printf("unknown command: %s (try: bootloader, reboot)\n", cmd);
    }
}

// A line submitted from elsewhere (the bridge): runs it here, as if typed, and hands over what it printed.
static void run_remote(const remote_t *r)
{
    capture_len = 0;
    capture[0] = '\0';
    capturing = true;
    run_command(r->line);
    capturing = false;
    r->done(r->id, capture);
}

static void console_task(void *arg)
{
    char cmd[256];  // room for `set token <token>`
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
        remote_t remote;
        if (xQueueReceive(remote_queue, &remote, pdMS_TO_TICKS(POLL_MS)) == pdTRUE) {
            run_remote(&remote);
        }
    }
}

esp_err_t keryx_console_start(const char *banner, keryx_console_command_fn command)
{
    banner_text = banner;
    app_command = command;
    write_lock = xSemaphoreCreateMutex();
    remote_queue = xQueueCreate(REMOTE_QUEUE, sizeof(remote_t));
    capture = heap_caps_malloc(REMOTE_OUTPUT_MAX, MALLOC_CAP_SPIRAM);
    if (write_lock == NULL || remote_queue == NULL || capture == NULL) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(console_task, "console", 4096, NULL, 2, &console_task_handle, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    esp_log_set_vprintf(log_vprintf);
    return ESP_OK;
}

bool keryx_console_submit(int id, const char *line, keryx_console_done_fn done)
{
    if (remote_queue == NULL || strlen(line) >= REMOTE_LINE_MAX) {
        return false;
    }
    remote_t r = {.id = id, .done = done};
    strcpy(r.line, line);
    return xQueueSend(remote_queue, &r, 0) == pdTRUE;
}
