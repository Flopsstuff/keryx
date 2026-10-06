#include "keryx_ota.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "keryx_console.h"

static const char *TAG = "ota";

// GitHub answers with a redirect to a signed URL of several hundred characters: the response header buffer must hold
// it, and so must the request buffer when the request goes there
#define RELEASES "https://github.com/Flopsstuff/keryx/releases/latest/download/"
#define HTTP_BUFFER 8192
#define HTTP_BUFFER_TX 4096
#define HTTP_TIMEOUT_MS 15000
#define TASK_STACK 8192  // in internal RAM: the task writes flash, and a TLS handshake needs the room
#define TRIAL_MS 30000
#define VERSION_MAX 32
#define URL_MAX 256

typedef enum { JOB_CHECK, JOB_UPDATE } job_t;

static volatile int progress = -1;
static volatile bool busy;
static job_t job;
static bool job_force;
static char job_url[URL_MAX];
static char latest[VERSION_MAX];  // what the last check found
static esp_err_t check_result;
static SemaphoreHandle_t check_done;

static void http_config(esp_http_client_config_t *cfg, const char *url)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->url = url;
    cfg->crt_bundle_attach = esp_crt_bundle_attach;
    cfg->buffer_size = HTTP_BUFFER;
    cfg->buffer_size_tx = HTTP_BUFFER_TX;
    cfg->timeout_ms = HTTP_TIMEOUT_MS;
    cfg->keep_alive_enable = true;
}

// The latest release's VERSION file into `latest`.
static esp_err_t fetch_latest(void)
{
    esp_http_client_config_t cfg;
    http_config(&cfg, RELEASES "VERSION");
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return ESP_ERR_NO_MEM;
    }
    char body[VERSION_MAX] = "";
    esp_err_t err = esp_http_client_open(client, 0);
    int status = 0;
    for (int hops = 0; err == ESP_OK; hops++) {
        esp_http_client_fetch_headers(client);
        status = esp_http_client_get_status_code(client);
        if ((status != 301 && status != 302 && status != 303 && status != 307 && status != 308) || hops == 5) {
            break;
        }
        // esp_http_client_open does not follow redirects by itself: GitHub sends two
        err = esp_http_client_set_redirection(client);
        if (err == ESP_OK) {
            esp_http_client_close(client);
            err = esp_http_client_open(client, 0);
        }
    }
    if (err == ESP_OK && status != 200) {
        ESP_LOGW(TAG, "%s: HTTP %d", RELEASES "VERSION", status);
        err = ESP_ERR_NOT_FOUND;
    }
    if (err == ESP_OK) {
        int n = esp_http_client_read(client, body, sizeof(body) - 1);
        body[n > 0 ? n : 0] = '\0';
        body[strcspn(body, " \r\n")] = '\0';
        err = body[0] ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
    }
    esp_http_client_cleanup(client);
    if (err == ESP_OK) {
        strlcpy(latest, body, sizeof(latest));
    }
    return err;
}

// Downloads job_url into the other app slot, then restarts into it; returns only if that failed.
static esp_err_t download(void)
{
    esp_http_client_config_t http;
    http_config(&http, job_url);
    esp_https_ota_config_t cfg = {.http_config = &http};
    esp_https_ota_handle_t ota = NULL;
    esp_err_t err = esp_https_ota_begin(&cfg, &ota);
    if (err != ESP_OK) {
        return err;
    }
    esp_app_desc_t incoming;
    if (esp_https_ota_get_img_desc(ota, &incoming) == ESP_OK) {
        keryx_console_printf("ota: downloading %s %s into %s\n", incoming.project_name, incoming.version,
                             esp_ota_get_next_update_partition(NULL)->label);
        if (strcmp(incoming.project_name, esp_app_get_description()->project_name) != 0) {
            esp_https_ota_abort(ota);
            keryx_console_printf("ota: not a %s image\n", esp_app_get_description()->project_name);
            return ESP_ERR_INVALID_VERSION;
        }
    }
    const int size = esp_https_ota_get_image_size(ota);
    int shown = -10;
    progress = 0;
    while ((err = esp_https_ota_perform(ota)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        const int read = esp_https_ota_get_image_len_read(ota);
        progress = size > 0 ? (int)(100LL * read / size) : 0;
        if (progress >= shown + 10) {
            shown = progress - progress % 10;
            keryx_console_printf("ota: %d %% (%d of %d bytes)\n", progress, read, size);
        }
    }
    if (err == ESP_OK && !esp_https_ota_is_complete_data_received(ota)) {
        err = ESP_ERR_INVALID_SIZE;
    }
    if (err != ESP_OK) {
        esp_https_ota_abort(ota);
        progress = -1;
        return err;
    }
    err = esp_https_ota_finish(ota);  // checks the image and makes it the one to boot
    progress = -1;
    if (err != ESP_OK) {
        return err;
    }
    keryx_console_printf("ota: done, restarting into the new firmware (on trial for %d s)\n", TRIAL_MS / 1000);
    vTaskDelay(pdMS_TO_TICKS(500));  // let the line and the bridge's console reply out
    esp_restart();
    return ESP_OK;
}

static void ota_task(void *arg)
{
    if (job == JOB_CHECK) {
        check_result = fetch_latest();
        xSemaphoreGive(check_done);
    } else {
        esp_err_t err = ESP_OK;
        if (job_url[0] == '\0') {  // the latest release
            err = fetch_latest();
            const char *running = esp_app_get_description()->version;
            if (err != ESP_OK) {
                keryx_console_printf("ota: cannot read the latest release: %s\n", esp_err_to_name(err));
            } else if (strcmp(latest, running) == 0 && !job_force) {
                keryx_console_printf("ota: %s is the latest release already (ota update force reinstalls it)\n",
                                     running);
                err = ESP_FAIL;
            } else {
                strlcpy(job_url, RELEASES "keryx.bin", sizeof(job_url));
            }
        }
        if (err == ESP_OK) {
            keryx_console_printf("ota: from %s\n", job_url);
            err = download();
            keryx_console_printf("ota: failed: %s; still running %s\n", esp_err_to_name(err),
                                 esp_app_get_description()->version);
        }
    }
    busy = false;
    vTaskDelete(NULL);
}

static bool start_job(job_t what, const char *url, bool force)
{
    if (busy) {
        return false;
    }
    busy = true;
    job = what;
    job_force = force;
    strlcpy(job_url, url ? url : "", sizeof(job_url));
    if (xTaskCreatePinnedToCore(ota_task, "ota", TASK_STACK, NULL, 4, NULL, 0) != pdPASS) {
        busy = false;
        return false;
    }
    return true;
}

static void trial_over(void *arg)
{
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        ESP_LOGI(TAG, "%s kept: it ran %d s", esp_app_get_description()->version, TRIAL_MS / 1000);
    }
}

void keryx_ota_start_trial(void)
{
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        const esp_timer_create_args_t args = {.callback = trial_over, .name = "ota_trial"};
        esp_timer_handle_t timer;
        if (esp_timer_create(&args, &timer) == ESP_OK) {
            esp_timer_start_once(timer, TRIAL_MS * 1000LL);
        }
        ESP_LOGI(TAG, "new firmware on trial: kept after %d s, rolled back if it restarts sooner", TRIAL_MS / 1000);
    }
    check_done = xSemaphoreCreateBinary();
}

static const char *state_name(void)
{
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) != ESP_OK) {
        return "flashed over USB";
    }
    return state == ESP_OTA_IMG_PENDING_VERIFY ? "on trial"
           : state == ESP_OTA_IMG_VALID        ? "kept"
           : state == ESP_OTA_IMG_NEW          ? "new"
                                               : "flashed over USB";
}

bool keryx_ota_command(const char *cmd)
{
    if (strcmp(cmd, "ota") == 0) {
        keryx_console_printf("firmware %s in %s, %s%s\n", esp_app_get_description()->version,
                             esp_ota_get_running_partition()->label, state_name(),
                             busy ? "; an update is running" : "");
        keryx_console_printf("ok\n");
    } else if (strcmp(cmd, "ota check") == 0) {
        if (check_done == NULL || !start_job(JOB_CHECK, NULL, false)) {
            keryx_console_printf("error an update is running\n");
            return true;
        }
        // the TLS handshake needs more stack than the console has: the task fetches, the console waits
        if (xSemaphoreTake(check_done, pdMS_TO_TICKS(2 * HTTP_TIMEOUT_MS + 5000)) != pdTRUE) {
            keryx_console_printf("error no answer from GitHub\n");
        } else if (check_result != ESP_OK) {
            keryx_console_printf("error cannot read the latest release: %s\n", esp_err_to_name(check_result));
        } else {
            const char *running = esp_app_get_description()->version;
            keryx_console_printf("ok latest %s, running %s%s\n", latest, running,
                                 strcmp(latest, running) == 0 ? " (up to date)" : " (ota update installs it)");
        }
    } else if (strcmp(cmd, "ota update") == 0 || strcmp(cmd, "ota update force") == 0) {
        if (!start_job(JOB_UPDATE, NULL, cmd[10] != '\0')) {
            keryx_console_printf("error an update is running\n");
        } else {
            keryx_console_printf("ok updating from the latest GitHub release; the ring and the log show how it goes\n");
        }
    } else if (strncmp(cmd, "ota url ", 8) == 0) {
        const char *url = cmd + 8;
        if ((strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) || strlen(url) >= URL_MAX) {
            keryx_console_printf("error usage: ota url <http(s)://…>\n");
        } else if (!start_job(JOB_UPDATE, url, true)) {
            keryx_console_printf("error an update is running\n");
        } else {
            keryx_console_printf("ok updating from %s; the ring and the log show how it goes\n", url);
        }
    } else {
        return false;
    }
    return true;
}

int keryx_ota_progress(void)
{
    return progress;
}
