#include "keryx_link.h"

#include <math.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "keryx_console.h"
#include "keryx_net.h"
#include "lwip/sockets.h"

static const char *TAG = "link";

#define UP_RATE 16000
#define UP_FRAME 320                // 20 ms per binary frame
#define UP_RING 32768               // 2 s of uplink audio; a power of two
#define PREROLL_MS 500
#define STREAM_MAX_MS 120000        // a stream nobody stops ends here
#define PLAY_BUFFER_BYTES (10 * 24000 * 2)  // 10 s at 24 kHz
#define PLAY_PRIME_MS 150           // queued before the bridge's audio starts playing
#define FADE_FRAMES 480             // 10 ms at 48 kHz, on play_stop
#define POWER_SAVE_AFTER_MS 5000    // Wi-Fi power save comes back this long after a conversation
#define TAPS_PER_PHASE 24           // upsampler: 48 taps for 24 kHz, 72 for 16 kHz
#define TEXT_MAX 1024
#define URL_MAX 201
#define TOKEN_MAX 201

typedef enum { LINK_OFF, LINK_CONNECTING, LINK_HELLO, LINK_READY } link_state_t;
static const char *const STATE_NAMES[] = {"off", "connecting", "hello", "ready"};

static keryx_link_callbacks_t callbacks;
static SemaphoreHandle_t client_lock;  // around client: restart replaces it while the link task uses it
static esp_websocket_client_handle_t client;
static volatile link_state_t state;
static TaskHandle_t link_task_handle;
static char url[URL_MAX], token[TOKEN_MAX];
static volatile bool hello_pending;

// uplink: the capture task writes, the link task reads and sends
static int16_t *up_ring;
static volatile uint32_t up_written;  // samples ever written
static uint32_t up_sent;
static volatile bool streaming;
static volatile bool wake_pending;
static volatile float wake_score;
static int64_t stream_started_us;

// downlink: the WebSocket task writes play_src, the playback task reads it
static StreamBufferHandle_t play_src;
static volatile int play_rate = 24000;
static volatile bool play_open;      // from play_start until `played` is due
static volatile bool play_ending;    // play_end came: finish what is queued
static volatile bool play_stop_req;  // play_stop came, or the bridge went away
static volatile bool played_pending;
static volatile int volume_pending = -1;  // a volume to report, or -1
static volatile int mute_pending = -1;    // 0/1 to report, or -1

// playback task only
static bool play_active;  // primed and playing
static int fade_left;
static float hist[2 * TAPS_PER_PHASE];  // upsampler history, written twice for a linear read
static int hist_pos;
static float taps2[2 * TAPS_PER_PHASE], taps3[3 * TAPS_PER_PHASE];

// counters for the report
static volatile uint32_t frames_up, skipped_ms, bytes_down, play_underruns, play_overflows;
static int64_t last_activity_us;
static bool power_save_off;

// Windowed-sinc low-pass for upsampling by L: cutoff just under the source's Nyquist, gain L.
static void design_taps(float *h, int L)
{
    const int n = L * TAPS_PER_PHASE;
    const double fc = 0.45 / L;  // cycles per output sample
    for (int i = 0; i < n; i++) {
        double m = i - (n - 1) / 2.0;
        double sinc = m == 0 ? 2 * fc : sin(2 * M_PI * fc * m) / (M_PI * m);
        double w = 0.42 - 0.5 * cos(2 * M_PI * i / (n - 1)) + 0.08 * cos(4 * M_PI * i / (n - 1));  // Blackman
        h[i] = (float)(L * sinc * w);
    }
}

static void send_json(cJSON *msg)
{
    char *text = cJSON_PrintUnformatted(msg);
    if (text != NULL && client != NULL) {
        esp_websocket_client_send_text(client, text, strlen(text), pdMS_TO_TICKS(1000));
    }
    cJSON_free(text);
    cJSON_Delete(msg);
}

// lwIP has no TCP_NODELAY switch in esp_websocket_client: find the socket connected to the bridge's port and set it,
// or 20 ms frames wait for the bridge's ACKs (Nagle).
static void set_nodelay(void)
{
    int port = 80;
    const char *colon = strchr(url + 5, ':');
    if (colon != NULL) {
        port = atoi(colon + 1);
    }
    for (int fd = LWIP_SOCKET_OFFSET; fd < LWIP_SOCKET_OFFSET + CONFIG_LWIP_MAX_SOCKETS; fd++) {
        struct sockaddr_in peer;
        socklen_t len = sizeof(peer);
        if (getpeername(fd, (struct sockaddr *)&peer, &len) == 0 && ntohs(peer.sin_port) == port) {
            int one = 1;
            setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        }
    }
}

static void handle_message(const char *text)
{
    cJSON *msg = cJSON_Parse(text);
    const cJSON *type = cJSON_GetObjectItem(msg, "type");
    if (!cJSON_IsString(type)) {
        keryx_console_printf("bridge sent something unexpected: %.80s\n", text);
    } else if (strcmp(type->valuestring, "ready") == 0) {
        state = LINK_READY;
        keryx_console_printf("bridge ready\n");
    } else if (strcmp(type->valuestring, "listen_stop") == 0) {
        streaming = false;
    } else if (strcmp(type->valuestring, "sound") == 0) {
        const cJSON *name = cJSON_GetObjectItem(msg, "name");
        if (cJSON_IsString(name) && callbacks.sound != NULL) {
            callbacks.sound(name->valuestring);
        }
    } else if (strcmp(type->valuestring, "play_start") == 0) {
        const cJSON *rate = cJSON_GetObjectItem(msg, "rate");
        int r = cJSON_IsNumber(rate) ? rate->valueint : 0;
        if (r != 16000 && r != 24000) {
            keryx_console_printf("bridge play_start: rate %d not supported (16000, 24000)\n", r);
        } else if (play_open && r != play_rate) {
            keryx_console_printf("bridge play_start: rate %d while %d is still playing, ignored\n", r, play_rate);
        } else {
            play_rate = r;
            play_ending = false;
            play_open = true;
        }
    } else if (strcmp(type->valuestring, "play_end") == 0) {
        play_ending = true;
    } else if (strcmp(type->valuestring, "volume") == 0) {
        const cJSON *value = cJSON_GetObjectItem(msg, "value"), *delta = cJSON_GetObjectItem(msg, "delta");
        if (callbacks.volume != NULL && cJSON_IsNumber(value)) {
            callbacks.volume(false, value->valueint);
        } else if (callbacks.volume != NULL && cJSON_IsNumber(delta)) {
            callbacks.volume(true, delta->valueint);
        } else if (callbacks.get_volume != NULL) {
            keryx_link_volume_changed(callbacks.get_volume());  // {"type":"volume"} alone asks for it
        }
    } else if (strcmp(type->valuestring, "mute") == 0) {
        const cJSON *value = cJSON_GetObjectItem(msg, "value");
        if (callbacks.mute != NULL && cJSON_IsBool(value)) {
            callbacks.mute(cJSON_IsTrue(value));
        } else if (callbacks.get_muted != NULL) {
            keryx_link_mute_changed(callbacks.get_muted());  // {"type":"mute"} alone asks for it
        }
    } else if (strcmp(type->valuestring, "play_stop") == 0) {
        if (play_open) {
            play_stop_req = true;
        }
    } else {
        keryx_console_printf("bridge sent unknown type %s\n", type->valuestring);
    }
    cJSON_Delete(msg);
}

static void on_ws_event(void *arg, esp_event_base_t base, int32_t id, void *event_data)
{
    const esp_websocket_event_data_t *data = event_data;
    static char text[TEXT_MAX];
    static uint8_t frame_op;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        state = LINK_HELLO;
        set_nodelay();
        hello_pending = true;
        xTaskNotifyGive(link_task_handle);
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        if (state != LINK_CONNECTING) {
            keryx_console_printf("bridge disconnected\n");
        }
        state = LINK_CONNECTING;
        streaming = false;
        if (play_open) {
            play_stop_req = true;
        }
        break;
    case WEBSOCKET_EVENT_DATA:
        if (data->payload_offset == 0 && data->op_code != 0) {
            frame_op = data->op_code;  // later chunks of a long frame, and continuation frames, keep its type
        }
        if (frame_op == 0x1) {
            if (data->payload_offset == 0) {
                text[0] = '\0';
            }
            if (data->payload_offset + data->data_len < TEXT_MAX) {
                memcpy(text + data->payload_offset, data->data_ptr, data->data_len);
                text[data->payload_offset + data->data_len] = '\0';
            }
            if (data->payload_offset + data->data_len >= data->payload_len) {
                handle_message(text);
            }
        } else if (frame_op == 0x2) {
            if (!play_open || play_stop_req) {
                break;  // audio outside play_start .. play_end
            }
            bytes_down += data->data_len;
            // blocking here holds back the TCP stream: the bridge may send faster than real time
            size_t sent = xStreamBufferSend(play_src, data->data_ptr, data->data_len, pdMS_TO_TICKS(3000));
            if (sent < (size_t)data->data_len) {
                play_overflows++;
            }
        } else if (frame_op == 0x8 && data->data_len >= 2) {
            int code = ((uint8_t)data->data_ptr[0] << 8) | (uint8_t)data->data_ptr[1];
            keryx_console_printf("bridge closed the connection: %d%s\n", code,
                                 code == 4001 ? " (token rejected; set token <token>)" : "");
        }
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGD(TAG, "websocket error");
        break;
    default:
        break;
    }
}

static void power_save(bool on)
{
    if (on == !power_save_off) {
        return;
    }
    esp_wifi_set_ps(on ? WIFI_PS_MIN_MODEM : WIFI_PS_NONE);
    power_save_off = !on;
}

static void send_hello(void)
{
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "type", "hello");
    cJSON_AddStringToObject(msg, "id", keryx_net_id());
    cJSON_AddStringToObject(msg, "token", token);
    cJSON_AddStringToObject(msg, "firmware", esp_app_get_description()->version);
    if (callbacks.get_volume != NULL) {
        cJSON_AddNumberToObject(msg, "volume", callbacks.get_volume());
    }
    if (callbacks.get_muted != NULL) {
        cJSON_AddBoolToObject(msg, "muted", callbacks.get_muted());
    }
    send_json(msg);
}

static void send_wake(void)
{
    bool fresh = !streaming;
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "type", "wake");
    cJSON_AddNumberToObject(msg, "score", roundf(wake_score * 1000) / 1000);
    cJSON_AddNumberToObject(msg, "preroll_ms", fresh ? PREROLL_MS : 0);
    if (fresh) {
        // the stream starts PREROLL_MS before now, so the start of the request is not lost to the detection delay
        up_sent = up_written - PREROLL_MS * UP_RATE / 1000;
        stream_started_us = esp_timer_get_time();
    }
    send_json(msg);
    streaming = true;
}

static void send_audio(void)
{
    static int16_t frame[UP_FRAME];
    uint32_t avail = up_written - up_sent;
    if (avail > UP_RING - UP_RATE / 2) {
        // more than 1.5 s behind: the ring is about to overwrite what is unsent; jump ahead
        uint32_t skip = avail - UP_FRAME;
        skipped_ms += skip * 1000 / UP_RATE;
        up_sent += skip;
        avail -= skip;
        keryx_console_printf("bridge stream fell behind, skipped %u ms\n", (unsigned)(skip * 1000 / UP_RATE));
    }
    while (avail >= UP_FRAME && streaming) {
        for (int i = 0; i < UP_FRAME; i++) {
            frame[i] = up_ring[(up_sent + i) & (UP_RING - 1)];
        }
        if (esp_websocket_client_send_bin(client, (const char *)frame, sizeof(frame), pdMS_TO_TICKS(500)) < 0) {
            return;  // try again next round; a lost connection ends the stream through its event
        }
        up_sent += UP_FRAME;
        avail -= UP_FRAME;
        frames_up++;
    }
}

static void link_task(void *arg)
{
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
        const int64_t now = esp_timer_get_time();
        xSemaphoreTake(client_lock, portMAX_DELAY);
        if (client != NULL) {
            if (hello_pending) {
                hello_pending = false;
                send_hello();
            }
            if (wake_pending) {
                wake_pending = false;
                if (state == LINK_READY) {
                    power_save(false);
                    send_wake();
                } else {
                    keryx_console_printf("bridge %s: wake not sent\n", STATE_NAMES[state]);
                }
            }
            int volume = volume_pending;
            if (volume >= 0) {
                volume_pending = -1;
                if (state == LINK_READY) {
                    cJSON *msg = cJSON_CreateObject();
                    cJSON_AddStringToObject(msg, "type", "volume");
                    cJSON_AddNumberToObject(msg, "value", volume);
                    send_json(msg);
                }
            }
            int mute = mute_pending;
            if (mute >= 0) {
                mute_pending = -1;
                if (state == LINK_READY) {
                    cJSON *msg = cJSON_CreateObject();
                    cJSON_AddStringToObject(msg, "type", "mute");
                    cJSON_AddBoolToObject(msg, "value", mute != 0);
                    send_json(msg);
                }
            }
            if (played_pending) {
                played_pending = false;
                if (state == LINK_READY) {
                    cJSON *msg = cJSON_CreateObject();
                    cJSON_AddStringToObject(msg, "type", "played");
                    send_json(msg);
                }
            }
            if (streaming && now - stream_started_us > STREAM_MAX_MS * 1000LL) {
                streaming = false;
                keryx_console_printf("bridge stream stopped after %d s without listen_stop\n", STREAM_MAX_MS / 1000);
            }
            if (streaming && state == LINK_READY) {
                send_audio();
            }
        }
        xSemaphoreGive(client_lock);

        if (streaming || play_open) {
            // also for answers the bridge starts on its own, with no wake before them (Hermes speaking first)
            power_save(false);
            last_activity_us = now;
        } else if (power_save_off && now - last_activity_us > POWER_SAVE_AFTER_MS * 1000LL) {
            power_save(true);
        }
    }
}

void keryx_link_restart(void)
{
    xSemaphoreTake(client_lock, portMAX_DELAY);
    if (client != NULL) {
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        client = NULL;
    }
    state = LINK_OFF;
    streaming = false;
    if (play_open) {
        play_stop_req = true;
    }
    if (keryx_net_bridge(url, sizeof(url), token, sizeof(token))) {
        esp_websocket_client_config_t cfg = {
            .uri = url,
            .reconnect_timeout_ms = 2000,
            .network_timeout_ms = 5000,
            .ping_interval_sec = 10,
            .pingpong_timeout_sec = 25,
            .buffer_size = 4096,  // above CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL: in PSRAM
            .task_stack = 4096,   // internal RAM: the library creates its task itself
            .task_prio = 5,
            .task_core_id_set = true,
            .task_core_id = 0,
        };
        client = esp_websocket_client_init(&cfg);
        if (client != NULL) {
            esp_websocket_register_events(client, WEBSOCKET_EVENT_ANY, on_ws_event, NULL);
            state = LINK_CONNECTING;
            esp_websocket_client_start(client);
        }
    }
    xSemaphoreGive(client_lock);
}

esp_err_t keryx_link_start(const keryx_link_callbacks_t *cb)
{
    if (cb != NULL) {
        callbacks = *cb;
    }
    // the library logs every failed attempt, every 2 s while the bridge is down; `status` and the report say enough
    const char *const noisy[] = {"websocket_client", "transport_ws", "transport_base", "esp-tls", "trans_tcp"};
    for (size_t i = 0; i < sizeof(noisy) / sizeof(noisy[0]); i++) {
        esp_log_level_set(noisy[i], ESP_LOG_NONE);
    }
    design_taps(taps2, 2);
    design_taps(taps3, 3);
    up_ring = heap_caps_calloc(UP_RING, sizeof(int16_t), MALLOC_CAP_SPIRAM);
    play_src = xStreamBufferCreateWithCaps(PLAY_BUFFER_BYTES, 1, MALLOC_CAP_SPIRAM);
    client_lock = xSemaphoreCreateMutex();
    if (up_ring == NULL || play_src == NULL || client_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }
    // the stack in PSRAM: internal RAM is short, and this task never writes flash
    if (xTaskCreatePinnedToCoreWithCaps(link_task, "link", 4096, NULL, 5, &link_task_handle, 0, MALLOC_CAP_SPIRAM) !=
        pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    keryx_link_restart();
    return ESP_OK;
}

void keryx_link_audio_up(const int16_t *samples, size_t count)
{
    if (up_ring == NULL) {
        return;
    }
    uint32_t w = up_written;
    for (size_t i = 0; i < count; i++) {
        up_ring[(w + i) & (UP_RING - 1)] = samples[i];
    }
    up_written = w + count;
    if (streaming && link_task_handle != NULL) {
        xTaskNotifyGive(link_task_handle);
    }
}

void keryx_link_volume_changed(int volume)
{
    volume_pending = volume;
    if (link_task_handle != NULL) {
        xTaskNotifyGive(link_task_handle);
    }
}

void keryx_link_mute_changed(bool muted)
{
    if (muted) {
        streaming = false;  // nothing more leaves the board; the bridge ends the conversation
    }
    mute_pending = muted;
    if (link_task_handle != NULL) {
        xTaskNotifyGive(link_task_handle);
    }
}

void keryx_link_wake(float score)
{
    if (link_task_handle == NULL || state == LINK_OFF) {
        return;
    }
    wake_score = score;
    wake_pending = true;
    xTaskNotifyGive(link_task_handle);
}

// Upsamples one source sample into L output samples.
static void upsample(int16_t x, int L, int16_t *out)
{
    const float *h = L == 2 ? taps2 : taps3;
    hist_pos = hist_pos == 0 ? TAPS_PER_PHASE - 1 : hist_pos - 1;
    hist[hist_pos] = hist[hist_pos + TAPS_PER_PHASE] = x;  // newest first
    for (int p = 0; p < L; p++) {
        float acc = 0;
        for (int k = 0; k < TAPS_PER_PHASE; k++) {
            acc += h[p + k * L] * hist[hist_pos + k];
        }
        out[p] = acc > 32767.0f ? 32767 : acc < -32768.0f ? -32768 : (int16_t)acc;
    }
}

// Ends a playback: drops what is queued and tells the bridge.
static void play_finish(void)
{
    uint8_t scrap[256];
    while (xStreamBufferReceive(play_src, scrap, sizeof(scrap), 0) > 0) {
    }
    memset(hist, 0, sizeof(hist));
    play_active = false;
    play_ending = false;
    play_stop_req = false;
    play_open = false;
    played_pending = true;
    xTaskNotifyGive(link_task_handle);
}

size_t keryx_link_play(int16_t *out, size_t frames)
{
    if (!play_open) {
        return 0;
    }
    const int L = play_rate == 24000 ? 2 : 3;
    if (play_stop_req && !play_active) {
        play_finish();
        return 0;
    }
    if (!play_active) {
        size_t queued = xStreamBufferBytesAvailable(play_src);
        if (queued < (size_t)(PLAY_PRIME_MS * play_rate / 1000 * 2) && !play_ending) {
            return 0;
        }
        play_active = true;
        fade_left = FADE_FRAMES;
    }

    static int16_t src[3 * 240];
    size_t want = frames / L;
    if (want > sizeof(src) / sizeof(src[0])) {
        want = sizeof(src) / sizeof(src[0]);
    }
    size_t avail = xStreamBufferBytesAvailable(play_src) & ~(size_t)1;
    size_t bytes = avail < want * 2 ? avail : want * 2;
    size_t got = xStreamBufferReceive(play_src, src, bytes, 0) / 2;
    for (size_t i = 0; i < got; i++) {
        upsample(src[i], L, out + i * L);
    }
    size_t produced = got * L;

    if (play_stop_req) {
        for (size_t i = 0; i < produced; i++) {
            out[i] = (int16_t)(out[i] * fade_left / FADE_FRAMES);
            fade_left = fade_left > 0 ? fade_left - 1 : 0;
        }
        if (fade_left == 0 || got < want) {
            play_finish();
        }
    } else if (got < want) {
        if (play_ending && xStreamBufferBytesAvailable(play_src) < 2) {
            play_finish();  // all played
        } else {
            play_underruns++;  // the bridge's audio came late: queue PLAY_PRIME_MS again
            play_active = false;
        }
    }
    return produced;
}

void keryx_link_report(char *buf, size_t size)
{
    snprintf(buf, size, "bridge %s%s, stream %s: %u frames up%s, %u bytes down, play underruns %u overflows %u",
             STATE_NAMES[state], power_save_off ? " (power save off)" : "", streaming ? "on" : "off",
             (unsigned)frames_up, skipped_ms ? " (skipped some)" : "", (unsigned)bytes_down,
             (unsigned)play_underruns, (unsigned)play_overflows);
    frames_up = bytes_down = play_underruns = play_overflows = skipped_ms = 0;
}
