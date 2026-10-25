#include "keryx_console.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/usb_serial_jtag_ll.h"
#include "nvs.h"
#include "soc/rtc_cntl_reg.h"
#include "tusb.h"

static const char *TAG = "console";

#define POLL_MS 20
// in internal RAM, as commands write NVS; `status` overflowed 4 KB
#define CONSOLE_STACK 6144
#define LOG_LINE_MAX 256
#define REMOTE_LINE_MAX 128
#define REMOTE_OUTPUT_MAX 3072
#define REMOTE_QUEUE 2
#define JOURNAL_SIZE 8192        // the recent lines, in PSRAM
#define JOURNAL_TAIL 2700        // what `log recent` shows and the saved journal keeps: one answer over Wi-Fi
#define NVS_NAMESPACE "keryx"

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
// the journal: every line but the periodic report, each with the time it began
static char *journal;
static size_t journal_written;  // bytes ever written
static bool journal_line_start = true;
static portMUX_TYPE journal_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t boot_id;      // tells this run's saved journal from an earlier one's
static bool journal_saved;    // one is in NVS; kept here, as flash cannot be read from tasks with stacks in PSRAM

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

static void journal_put(const char *data, size_t len)
{
    while (len > 0) {
        size_t at = journal_written % JOURNAL_SIZE, n = JOURNAL_SIZE - at < len ? JOURNAL_SIZE - at : len;
        memcpy(journal + at, data, n);
        journal_written += n;
        data += n;
        len -= n;
    }
}

static void journal_append(const char *text, size_t len)
{
    // the time of day once SNTP has set it, until then the seconds since boot
    char stamp[24];
    time_t now = time(NULL);
    struct tm tm;
    if (now > 1700000000 && localtime_r(&now, &tm) != NULL) {
        strftime(stamp, sizeof(stamp), "%H:%M:%S ", &tm);
    } else {
        snprintf(stamp, sizeof(stamp), "+%llds ", esp_timer_get_time() / 1000000);
    }
    taskENTER_CRITICAL(&journal_lock);
    while (len > 0) {
        if (journal_line_start) {
            journal_put(stamp, strlen(stamp));
        }
        const char *newline = memchr(text, '\n', len);
        size_t n = newline != NULL ? (size_t)(newline - text) + 1 : len;
        journal_put(text, n);
        journal_line_start = newline != NULL;
        text += n;
        len -= n;
    }
    taskEXIT_CRITICAL(&journal_lock);
}

// The journal's last bytes, from the start of a line, into buf; returns their length.
static size_t journal_tail(char *buf, size_t size)
{
    taskENTER_CRITICAL(&journal_lock);
    size_t len = journal_written < JOURNAL_SIZE ? journal_written : JOURNAL_SIZE;
    len = len < size - 1 ? len : size - 1;
    for (size_t i = 0; i < len; i++) {
        buf[i] = journal[(journal_written - len + i) % JOURNAL_SIZE];
    }
    bool whole = len == journal_written;
    taskEXIT_CRITICAL(&journal_lock);
    buf[len] = '\0';
    char *first = whole ? buf : strchr(buf, '\n');
    if (first == NULL) {
        return 0;
    }
    if (first != buf) {
        len -= first + 1 - buf;
        memmove(buf, first + 1, len + 1);
    }
    return len;
}

// To the host, into a running capture and, if `journaled`, into the journal.
static void emit(const char *text, size_t len, bool journaled)
{
    write_bytes(text, len);
    if (capturing && xTaskGetCurrentTaskHandle() == console_task_handle) {
        size_t room = REMOTE_OUTPUT_MAX - 1 - capture_len;
        size_t take = len < room ? len : room;
        memcpy(capture + capture_len, text, take);
        capture_len += take;
        capture[capture_len] = '\0';
    }
    if (journaled && journal != NULL) {
        journal_append(text, len);
    }
}

static void write_vprintf(const char *fmt, va_list args, bool journaled)
{
    char line[LOG_LINE_MAX];
    int n = vsnprintf(line, sizeof(line), fmt, args);
    if (n > 0) {
        n = n < (int)sizeof(line) ? n : (int)sizeof(line) - 1;
        emit(line, n, journaled);
    }
}

void keryx_console_printf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    write_vprintf(fmt, args, true);
    va_end(args);
}

void keryx_console_report(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    write_vprintf(fmt, args, false);
    va_end(args);
}

static int log_vprintf(const char *fmt, va_list args)
{
    write_vprintf(fmt, args, true);
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

esp_err_t keryx_console_journal_save(void)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    uint32_t saved_by = 0;
    if (journal_saved && nvs_get_u32(nvs, "journal_boot", &saved_by) == ESP_OK && saved_by != boot_id) {
        nvs_close(nvs);
        return ESP_ERR_INVALID_STATE;  // an earlier run's, not yet read: it saw the trouble begin
    }
    char *text = heap_caps_malloc(JOURNAL_TAIL + 64, MALLOC_CAP_SPIRAM);
    if (text == NULL) {
        nvs_close(nvs);
        return ESP_ERR_NO_MEM;
    }
    time_t now = time(NULL);
    struct tm tm;
    int n = now > 1700000000 && localtime_r(&now, &tm) != NULL
                ? (int)strftime(text, 32, "saved %Y-%m-%d %H:%M:%S", &tm)
                : snprintf(text, 32, "saved");
    n += snprintf(text + n, 32, ", up %lld s\n", esp_timer_get_time() / 1000000);
    n += journal_tail(text + n, JOURNAL_TAIL + 64 - n);
    err = nvs_set_blob(nvs, "journal", text, n);
    if (err == ESP_OK) {
        err = nvs_set_u32(nvs, "journal_boot", boot_id);
    }
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    free(text);
    if (err == ESP_OK) {
        journal_saved = true;
    }
    return err;
}

bool keryx_console_journal_saved(void)
{
    return journal_saved;
}

bool keryx_console_host(void)
{
    return tud_mounted();
}

// "log recent", "log saved", "log saved clear"
static void journal_command(const char *what)
{
    char *text = heap_caps_malloc(JOURNAL_TAIL + 64, MALLOC_CAP_SPIRAM);
    if (text == NULL) {
        keryx_console_printf("error out of memory\n");
        return;
    }
    nvs_handle_t nvs;
    if (strcmp(what, "recent") == 0) {
        emit(text, journal_tail(text, JOURNAL_TAIL + 1), false);
        keryx_console_printf("ok\n");
    } else if (strcmp(what, "saved") == 0) {
        size_t len = JOURNAL_TAIL + 63;
        if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
            if (nvs_get_blob(nvs, "journal", text, &len) != ESP_OK) {
                len = 0;
            }
            nvs_close(nvs);
        } else {
            len = 0;
        }
        emit(text, len, false);
        keryx_console_printf(len > 0 ? "ok\n" : "ok nothing saved\n");
    } else if (strcmp(what, "saved clear") == 0) {
        esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
        if (err == ESP_OK) {
            nvs_erase_key(nvs, "journal");
            nvs_erase_key(nvs, "journal_boot");
            err = nvs_commit(nvs);
            nvs_close(nvs);
        }
        if (err == ESP_OK) {
            journal_saved = false;
            keryx_console_printf("ok saved journal cleared\n");
        } else {
            keryx_console_printf("error %s\n", esp_err_to_name(err));
        }
    } else {
        keryx_console_printf("error usage: log recent | log saved [clear]\n");
    }
    free(text);
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
    } else if (strcmp(cmd, "log recent") == 0 || strncmp(cmd, "log saved", 9) == 0) {
        journal_command(cmd + 4);
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
    journal = heap_caps_malloc(JOURNAL_SIZE, MALLOC_CAP_SPIRAM);
    if (write_lock == NULL || remote_queue == NULL || capture == NULL || journal == NULL) {
        return ESP_ERR_NO_MEM;
    }
    boot_id = esp_random();
    nvs_handle_t nvs;
    size_t len = 0;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        journal_saved = nvs_get_blob(nvs, "journal", NULL, &len) == ESP_OK && len > 0;
        nvs_close(nvs);
    }
    if (xTaskCreatePinnedToCore(console_task, "console", CONSOLE_STACK, NULL, 2, &console_task_handle, 0) != pdPASS) {
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
