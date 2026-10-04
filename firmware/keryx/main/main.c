/*
 * Keryx: the "Hey Keryx" wake word and a USB sound card in one firmware.
 *
 * One task reads the XVF3800's I2S capture (48 kHz, 32-bit; L = processed beam, R = ASR beam) in 5 ms blocks and
 * feeds both users from it:
 * - the wake word (components/keryx_wakeword) on the ASR channel, as in the wakeword firmware;
 * - the USB microphone: 16-bit stereo through a stream buffer to the UAC callback. The XVF3800 clocks I2S and the
 *   host clocks USB, so the buffer drifts; whatever is older than MAX_LAG_MS when the host reads is dropped.
 * Playback: the host sends 1 ms packets at its own clock. They go into a second stream buffer, and a playback task
 * writes I2S without a break in 5 ms blocks: silence when the host is quiet, PLAY_PRIME_MS of its audio queued
 * before playing starts, the excess dropped when the host runs ahead of the XVF3800's clock. Writing each packet
 * to I2S as it came (what the UAC component's example does) left the DMA a few ms of slack and played gaps.
 * A detection goes to the host as a `wake score=0.973` line on the serial port (keryx_console), which also carries
 * the log, and plays a chime. `sound thinking` there loops a quiet "thinking" sound until `sound stop`, until the
 * host's audio starts or for THINKING_MAX_MS at most. Both are mixed into the playback (keryx_sounds.h, made by
 * firmware/sounds/make_sounds.py). The XVF3800 reads the playback line as its echo reference, so none of it
 * reaches the ASR channel. `loop on` on the serial port puts that reference on the left
 * capture channel instead of the processed beam, to hear or measure what really reached the headphone jack.
 *
 * The XVF3800 must run the I2S firmware, which clocks the bus; the ESP32 is the I2S slave. The USB PHY stays with
 * the USB serial/JTAG console for a few seconds after boot, then TinyUSB takes it for the sound card and the
 * serial port; the `bootloader` command there restarts into the ROM download mode (firmware/flash.sh sends it).
 * The same port pairs the board with the voice bridge: Wi-Fi and bridge settings, kept in NVS (keryx_net).
 * Over Wi-Fi the board talks to the bridge (keryx_link): a wake sends the 16 kHz ASR audio from just before it
 * until the bridge stops listening, and the bridge's answer is mixed into the playback like the host's.
 */

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "keryx_console.h"
#include "keryx_link.h"
#include "keryx_net.h"
#include "keryx_sounds.h"
#include "kww_decimate.h"
#include "kww_frontend.h"
#include "kww_model.h"
#include "usb_device_uac.h"
#include "xvf_control.h"

static const char *TAG = "keryx";

// XIAO ESP32S3 on the reSpeaker Flex
#define PIN_I2C_SDA GPIO_NUM_5   // D4
#define PIN_I2C_SCL GPIO_NUM_6   // D5
#define PIN_I2S_BCLK GPIO_NUM_8  // D9
#define PIN_I2S_LRCK GPIO_NUM_7  // D8
#define PIN_I2S_DOUT GPIO_NUM_44 // D7, to XVF3800 I2S DATA0 (playback and AEC reference)
#define PIN_I2S_DIN GPIO_NUM_43  // D6, from XVF3800 I2S DATA1 (L processed, R ASR)

#define SAMPLE_RATE 48000
#define BLOCK_FRAMES 240        // 5 ms; a multiple of 3 for the 48 -> 16 kHz decimator
#define DMA_BLOCKS 4            // I2S DMA queue: 20 ms each way
#define MIC_BUFFER_MS 100       // stream buffer between capture and the USB microphone
#define MAX_LAG_MS 20           // older audio is dropped when the host reads
#define PLAY_BUFFER_MS 100      // stream buffer between the USB speaker and the playback task
#define PLAY_PRIME_MS 30        // queued before playing starts: covers USB and scheduling jitter (15 was not enough with Wi-Fi traffic)
#define PLAY_MAX_MS 80          // more queued than this: the host runs ahead, the excess is dropped
#define PLAY_TARGET_MS 40       // the USB feedback steers the host's rate to keep this much queued
#define PLAY_STEER 0.0002f      // rate change per ms of difference: 10 ms off asks for 0.2 % more or less
#define PLAY_STEER_MAX 0.005f
#define SETTLE_MS 1000          // silence after starting I2S; sound sent earlier comes out with noise
#define CONSOLE_GRACE_MS 3000   // keep the serial/JTAG console (and esptool) before TinyUSB takes the USB PHY
#define ASR_GAIN 4.0f           // AEC_ASROUTGAIN, +12 dB: the wake word model learned the channel at this gain
#define REFRACTORY_MS 1000      // one detection per phrase
#define WARMUP_MS 2500          // the model needs KWW_RECEPTIVE frames of history before its scores mean anything
#define REPORT_MS 5000
#define PLAYBACK_IDLE_MS 20     // the host counts as not playing after this long without audio
#define THINKING_MAX_MS 60000   // the thinking sound stops by itself after this, should nobody stop it
#define SOUND_FADE_FRAMES 480   // 10 ms fade when a sound is stopped while it sounds
#define HOST_AUDIBLE 64         // host audio peaking above this (-54 dBFS) stops the thinking sound

#define FRAME_BYTES (2 * sizeof(int16_t))
#define MS_BYTES (SAMPLE_RATE / 1000 * FRAME_BYTES)

// XVF3800 output mux of the left capture channel (category, source); see docs/respeaker-flex-xvf3800.md
#define XVF_AUDIO_MGR_RESID 35
#define XVF_AUDIO_MGR_OP_L 15
static const uint8_t OP_L_BEAM[2] = {8, 0};       // user chosen channel: the processed auto-select beam
static const uint8_t OP_L_REFERENCE[2] = {4, 0};  // far end: the echo reference, i.e. our playback
static const uint8_t OP_L_MIC[2] = {11, 0};       // amplified microphone 0 before the system delay: echo before AEC

static i2s_chan_handle_t i2s_tx, i2s_rx;
static StreamBufferHandle_t mic_buffer, play_buffer;
static char banner[160];

// statistics for the report, reset with it
static volatile int64_t last_mic_read_us;      // the host is recording while this is recent
static volatile uint32_t mic_dropped_bytes;    // dropped for lag
static volatile uint32_t mic_short_reads;      // the callback had less than asked for
static volatile uint32_t capture_overruns;     // blocks that did not fit into the stream buffer
static volatile uint32_t play_underruns;       // the host's audio ran out while it was still playing
static volatile uint32_t play_overflows;       // packets that did not fit into the playback buffer
static volatile uint32_t play_dropped_bytes;   // dropped because the host ran ahead
static volatile uint32_t play_received_bytes;  // from the host, to measure its real rate
static volatile uint32_t i2s_tx_late;          // DMA blocks that went out as silence: the playback task was late

// speaker gain in Q15, from the host's volume and mute controls
static volatile int32_t speaker_gain = 32767;
static volatile uint32_t speaker_volume = 100;
static volatile bool speaker_muted;
static volatile int64_t last_playback_us;

// interface sounds: requests from other tasks; the playback task keeps the positions
static volatile bool wake_sound_requested;
static volatile bool thinking_requested;
static volatile int64_t thinking_started_us;

static void update_speaker_gain(void)
{
    // the component maps the host's -50..0 dB volume range onto 0..100
    double db = speaker_volume / 2.0 - 50.0;
    speaker_gain = speaker_muted || speaker_volume == 0 ? 0 : (int32_t)(pow(10.0, db / 20.0) * 32767.0);
}

static bool IRAM_ATTR on_tx_late(i2s_chan_handle_t handle, i2s_event_data_t *event, void *ctx)
{
    i2s_tx_late++;
    return false;
}

static void i2s_start(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = DMA_BLOCKS;
    chan_cfg.dma_frame_num = BLOCK_FRAMES;
    chan_cfg.auto_clear = true; // silence if the playback task ever falls behind
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &i2s_tx, &i2s_rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_I2S_BCLK,
            .ws = PIN_I2S_LRCK,
            .dout = PIN_I2S_DOUT,
            .din = PIN_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(i2s_tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(i2s_rx, &std_cfg));
    const i2s_event_callbacks_t tx_callbacks = {.on_send_q_ovf = on_tx_late};
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(i2s_tx, &tx_callbacks, NULL));
    ESP_ERROR_CHECK(i2s_channel_enable(i2s_tx));
    ESP_ERROR_CHECK(i2s_channel_enable(i2s_rx));
}

static void sound_thinking(bool on)
{
    thinking_started_us = esp_timer_get_time();
    thinking_requested = on;
}

// Writes I2S without a break, at the XVF3800's pace: the host's audio from the playback buffer, or silence, with the
// interface sounds mixed in.
static void playback_task(void *arg)
{
    static int16_t in[BLOCK_FRAMES * 2];
    static int32_t out[BLOCK_FRAMES * 2];
    bool playing = false;
    int wake_pos = KERYX_SOUND_WAKE_LEN;  // < LEN while the chime sounds
    bool thinking = false;
    int thinking_pos = 0, thinking_fade = 0;  // fade: frames left of a fade-out, 0 when not fading
    for (;;) {
        size_t queued = xStreamBufferBytesAvailable(play_buffer);
        // asynchronous USB: the host follows our clock (the XVF3800's) through the feedback endpoint
        static float level_ms = PLAY_TARGET_MS, rate_set = 1.0f;
        level_ms += 0.05f * ((float)queued / MS_BYTES - level_ms);  // ~100 ms average
        float rate = 1.0f + PLAY_STEER * (PLAY_TARGET_MS - level_ms);
        rate = rate > 1.0f + PLAY_STEER_MAX ? 1.0f + PLAY_STEER_MAX : rate < 1.0f - PLAY_STEER_MAX ? 1.0f - PLAY_STEER_MAX : rate;
        if (fabsf(rate - rate_set) > 0.00005f && uac_device_speaker_rate(rate) == ESP_OK) {
            rate_set = rate;
        }
        if (!playing && queued >= PLAY_PRIME_MS * MS_BYTES) {
            playing = true;
        }
        size_t got = 0;
        if (playing) {
            while (queued > PLAY_MAX_MS * MS_BYTES) {
                size_t n = queued - PLAY_MAX_MS * MS_BYTES;
                n = n < sizeof(in) ? n - n % FRAME_BYTES : sizeof(in);
                n = xStreamBufferReceive(play_buffer, in, n, 0);
                play_dropped_bytes += n;
                queued -= n;
            }
            got = xStreamBufferReceive(play_buffer, in, sizeof(in), 0);
            if (got < sizeof(in)) {
                // ran dry: the host stopped, or its audio comes late; queue PLAY_PRIME_MS again before going on
                if (esp_timer_get_time() - last_playback_us < PLAYBACK_IDLE_MS * 1000LL) {
                    play_underruns++;
                }
                playing = false;
            }
        }
        memset((uint8_t *)in + got, 0, sizeof(in) - got);
        static int16_t bridge[BLOCK_FRAMES];
        memset(bridge, 0, sizeof(bridge));
        keryx_link_play(bridge, BLOCK_FRAMES);

        if (wake_sound_requested) {
            wake_sound_requested = false;
            wake_pos = 0;
        }
        int host_peak = 0;
        for (int i = 0; i < BLOCK_FRAMES * 2; i++) {
            int level = in[i] < 0 ? -in[i] : in[i];
            host_peak = level > host_peak ? level : host_peak;
        }
        for (int i = 0; i < BLOCK_FRAMES; i++) {
            int level = bridge[i] < 0 ? -bridge[i] : bridge[i];
            host_peak = level > host_peak ? level : host_peak;
        }
        if (thinking_requested && (host_peak > HOST_AUDIBLE ||
                                   esp_timer_get_time() - thinking_started_us > THINKING_MAX_MS * 1000LL)) {
            thinking_requested = false;  // the answer has started, or nobody stopped it
        }
        if (thinking_requested && !thinking) {
            thinking = true;
            thinking_pos = thinking_fade = 0;
        } else if (!thinking_requested && thinking && thinking_fade == 0) {
            // between the taps it can stop at once; within one, fade out
            if (thinking_pos < KERYX_SOUND_THINKING_LEN) {
                thinking_fade = SOUND_FADE_FRAMES;
            } else {
                thinking = false;
            }
        }

        int32_t gain = speaker_gain;
        for (int f = 0; f < BLOCK_FRAMES; f++) {
            int32_t sound = bridge[f];  // the bridge's answer, mono, not under the host's volume
            if (wake_pos < KERYX_SOUND_WAKE_LEN) {
                sound += keryx_sound_wake[wake_pos++];
            }
            if (thinking) {
                int32_t v = thinking_pos < KERYX_SOUND_THINKING_LEN ? keryx_sound_thinking[thinking_pos] : 0;
                if (thinking_fade > 0) {
                    v = v * thinking_fade / SOUND_FADE_FRAMES;
                    thinking = --thinking_fade > 0;
                }
                sound += v;
                thinking_pos = (thinking_pos + 1) % KERYX_SOUND_THINKING_LOOP;
            }
            for (int c = 0; c < 2; c++) {
                // Q15 gain; stays inside int32 even at full scale
                int64_t v = (int64_t)in[2 * f + c] * gain * 2 + ((int64_t)sound << 16);
                out[2 * f + c] = v > INT32_MAX ? INT32_MAX : v < INT32_MIN ? INT32_MIN : (int32_t)v;
            }
        }
        size_t written;
        i2s_channel_write(i2s_tx, out, sizeof(out), &written, portMAX_DELAY);
    }
}

// Reads I2S, hands the audio to the USB microphone and runs the wake word on the ASR channel.
static void capture_task(void *arg)
{
    static int32_t raw[BLOCK_FRAMES * 2];
    static int16_t pcm[BLOCK_FRAMES * 2];
    static int16_t asr[BLOCK_FRAMES];
    static int16_t audio16[BLOCK_FRAMES / 3 + 2];
    static float frames[4][40];
    static kww_decimate_t decimate;
    static kww_frontend_t frontend;
    static kww_state_t model;

    kww_decimate_reset(&decimate);
    if (!kww_frontend_init(&frontend)) {
        ESP_LOGE(TAG, "micro_speech frontend failed to initialise");
        vTaskDelete(NULL);
    }
    kww_reset(&model);

    const int64_t start = esp_timer_get_time();
    int64_t last_report = start, last_detection = 0, busy_us = 0;
    int blocks = 0, peak_level = 0;
    float peak_score = 0.0f;

    for (;;) {
        size_t got = 0;
        if (i2s_channel_read(i2s_rx, raw, sizeof(raw), &got, 1000) != ESP_OK || got != sizeof(raw)) {
            ESP_LOGW(TAG, "I2S read returned %u bytes", (unsigned)got);
            continue;
        }
        int64_t t0 = esp_timer_get_time();

        // the top 16 bits are what goes to the host; the right slot is the ASR output the model learned from
        for (int i = 0; i < BLOCK_FRAMES * 2; i++) {
            pcm[i] = (int16_t)(raw[i] >> 16);
        }
        // whole blocks only, so the channels never swap; the buffer fills up while the host is not recording
        if (xStreamBufferSpacesAvailable(mic_buffer) >= sizeof(pcm)) {
            xStreamBufferSend(mic_buffer, pcm, sizeof(pcm), 0);
        } else if (esp_timer_get_time() - last_mic_read_us < 100000) {
            capture_overruns++;
        }

        for (int i = 0; i < BLOCK_FRAMES; i++) {
            asr[i] = pcm[2 * i + 1];
            int level = asr[i] < 0 ? -asr[i] : asr[i];
            peak_level = level > peak_level ? level : peak_level;
        }
        size_t n16 = kww_decimate(&decimate, asr, BLOCK_FRAMES, audio16);
        keryx_link_audio_up(audio16, n16);
        int nframes = kww_frontend_process(&frontend, audio16, n16, frames, 4);
        for (int f = 0; f < nframes; f++) {
            float score;
            if (!kww_push(&model, frames[f], &score)) {
                continue;
            }
            const int64_t now = esp_timer_get_time();
            if (now - start < WARMUP_MS * 1000LL) {
                continue;
            }
            peak_score = score > peak_score ? score : peak_score;
            if (score >= KWW_THRESHOLD && now - last_detection > REFRACTORY_MS * 1000LL) {
                last_detection = now;
                keryx_console_printf("wake score=%.3f\n", score);
                wake_sound_requested = true;
                keryx_link_wake(score);
            }
        }
        busy_us += esp_timer_get_time() - t0;
        blocks++;

        const int64_t now = esp_timer_get_time();
        if (now - last_report >= REPORT_MS * 1000LL) {
            bool recording = now - last_mic_read_us < 100000;
            bool playing = now - last_playback_us < PLAYBACK_IDLE_MS * 1000LL;
            ESP_LOGI(TAG, "peak score %.3f | ASR peak %.1f dBFS | %.2f ms per 5 ms | USB mic %s, lag %u ms, "
                          "dropped %u ms, short reads %u, overruns %u | USB speaker %s, queued %u ms, "
                          "underruns %u, overflows %u, dropped %u ms, rate %u Hz | I2S late %u", peak_score,
                     20.0f * log10f((peak_level + 1) / 32768.0f), busy_us / 1000.0f / blocks,
                     recording ? "on" : "off", (unsigned)(xStreamBufferBytesAvailable(mic_buffer) / MS_BYTES),
                     (unsigned)(mic_dropped_bytes / MS_BYTES), (unsigned)mic_short_reads,
                     (unsigned)capture_overruns, playing ? "on" : "off",
                     (unsigned)(xStreamBufferBytesAvailable(play_buffer) / MS_BYTES), (unsigned)play_underruns,
                     (unsigned)play_overflows, (unsigned)(play_dropped_bytes / MS_BYTES),
                     (unsigned)((uint64_t)play_received_bytes * 1000000 / FRAME_BYTES / (now - last_report)),
                     (unsigned)i2s_tx_late);
            last_report = now;
            busy_us = 0;
            blocks = 0;
            peak_level = 0;
            peak_score = 0.0f;
            static char link_line[160];
            keryx_link_report(link_line, sizeof(link_line));
            ESP_LOGI(TAG, "%s", link_line);
            mic_dropped_bytes = mic_short_reads = capture_overruns = 0;
            play_underruns = play_overflows = play_dropped_bytes = i2s_tx_late = play_received_bytes = 0;
        }
    }
}

// Host is recording: hand over the next `len` bytes of 16-bit stereo from the stream buffer.
static esp_err_t uac_input_cb(uint8_t *buf, size_t len, size_t *bytes_read, void *ctx)
{
    const int64_t now = esp_timer_get_time();
    if (now - last_mic_read_us > 100000) {
        // the host just started: whatever piled up meanwhile is stale
        size_t stale = xStreamBufferBytesAvailable(mic_buffer);
        while (stale >= len) {
            stale -= xStreamBufferReceive(mic_buffer, buf, len, 0);
        }
    }
    last_mic_read_us = now;

    // keep at most MAX_LAG_MS queued beyond this read
    size_t queued = xStreamBufferBytesAvailable(mic_buffer);
    while (queued > len + MAX_LAG_MS * MS_BYTES) {
        size_t n = queued - len - MAX_LAG_MS * MS_BYTES;
        n = n < len ? n - n % FRAME_BYTES : len;
        if (n == 0) {
            break;
        }
        n = xStreamBufferReceive(mic_buffer, buf, n, 0);
        mic_dropped_bytes += n;
        queued -= n;
    }

    // wait for the rest, at the XVF3800's pace
    size_t done = 0;
    while (done < len) {
        size_t n = xStreamBufferReceive(mic_buffer, buf + done, len - done, pdMS_TO_TICKS(20));
        if (n == 0) {
            break;
        }
        done += n;
    }
    if (done < len) {
        mic_short_reads++;
    }
    *bytes_read = done;
    return ESP_OK;
}

// Host is playing: queue its 16-bit stereo for the playback task. Called every 1 ms; must not block.
static esp_err_t uac_output_cb(uint8_t *buf, size_t len, void *ctx)
{
    last_playback_us = esp_timer_get_time();
    len -= len % FRAME_BYTES;
    play_received_bytes += len;
    if (xStreamBufferSpacesAvailable(play_buffer) >= len) {
        xStreamBufferSend(play_buffer, buf, len, 0);
    } else {
        play_overflows++;
    }
    return ESP_OK;
}

static void uac_set_mute_cb(uint32_t mute, void *ctx)
{
    speaker_muted = mute != 0;
    update_speaker_gain();
}

static void uac_set_volume_cb(uint32_t volume, void *ctx)
{
    speaker_volume = volume;
    update_speaker_gain();
}

static const char *reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_SW: return "restart";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INTERRUPT WATCHDOG";
    case ESP_RST_TASK_WDT: return "TASK WATCHDOG";
    case ESP_RST_WDT: return "WATCHDOG";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_USB: return "usb";
    case ESP_RST_EXT: return "reset pin";
    default: return "other";
    }
}

// USB serial number: one per board, so each gets its own serial port name on the host.
const char *uac_serial_number(void)
{
    return keryx_net_id();
}

// "xvf get <resid> <cmd> <int32|float|uint8> [count]" / "xvf set <resid> <cmd> <type> <value>...": any XVF3800
// parameter over I2C, as tools/xvf does over USB (ids in docs/respeaker-flex-xvf3800.md and the XMOS user guide).
static void xvf_command(const char *args)
{
    char op[4], type[8];
    int resid, id, consumed = 0;
    if (sscanf(args, "%3s %d %d %7s %n", op, &resid, &id, type, &consumed) != 4 ||
        (strcmp(type, "int32") != 0 && strcmp(type, "float") != 0 && strcmp(type, "uint8") != 0)) {
        keryx_console_printf("error usage: xvf get|set <resid> <cmd> <int32|float|uint8> [count | values...]\n");
        return;
    }
    const size_t size = type[0] == 'u' ? 1 : 4;
    uint8_t buf[64];
    size_t count = 0;
    if (strcmp(op, "get") == 0) {
        count = args[consumed] ? (size_t)atoi(args + consumed) : 1;
        if (count < 1 || count * size > sizeof(buf)) {
            keryx_console_printf("error count\n");
            return;
        }
        esp_err_t err = xvf_read(resid, id, buf, count * size);
        if (err != ESP_OK) {
            keryx_console_printf("error read: %s\n", esp_err_to_name(err));
            return;
        }
    } else if (strcmp(op, "set") == 0) {
        const char *p = args + consumed;
        char *end;
        while (*p && count * size < sizeof(buf)) {
            if (type[0] == 'f') {
                float v = strtof(p, &end);
                memcpy(buf + count * 4, &v, 4);
            } else {
                long v = strtol(p, &end, 0);
                if (size == 1) {
                    buf[count] = (uint8_t)v;
                } else {
                    int32_t v32 = (int32_t)v;
                    memcpy(buf + count * 4, &v32, 4);
                }
            }
            if (end == p) {
                break;
            }
            count++;
            p = end;
        }
        esp_err_t err = count ? xvf_write(resid, id, buf, count * size) : ESP_ERR_INVALID_ARG;
        if (err == ESP_OK) {
            err = xvf_read(resid, id, buf, count * size);  // what the chip took
        }
        if (err != ESP_OK) {
            keryx_console_printf("error write: %s\n", esp_err_to_name(err));
            return;
        }
    } else {
        keryx_console_printf("error usage: xvf get|set ...\n");
        return;
    }
    char line[200];
    int n = snprintf(line, sizeof(line), "ok %d %d =", resid, id);
    for (size_t i = 0; i < count && n < (int)sizeof(line) - 16; i++) {
        if (type[0] == 'f') {
            float v;
            memcpy(&v, buf + i * 4, 4);
            n += snprintf(line + n, sizeof(line) - n, " %g", v);
        } else if (size == 1) {
            n += snprintf(line + n, sizeof(line) - n, " %u", buf[i]);
        } else {
            int32_t v;
            memcpy(&v, buf + i * 4, 4);
            n += snprintf(line + n, sizeof(line) - n, " %ld", (long)v);
        }
    }
    keryx_console_printf("%s\n", line);
}

// Serial port commands beyond keryx_console's own.
static bool console_command(const char *cmd)
{
    if (keryx_net_command(cmd)) {
        if (strncmp(cmd, "set bridge ", 11) == 0 || strncmp(cmd, "set token ", 10) == 0 || strcmp(cmd, "erase") == 0) {
            keryx_link_restart();
        }
        return true;
    }
    if (strcmp(cmd, "wake") == 0) {
        // as if the wake word fired, for testing the bridge without saying it
        wake_sound_requested = true;
        keryx_link_wake(1.0f);
        keryx_console_printf("wake score=1.000 (console)\n");
        return true;
    }
    if (strcmp(cmd, "sound wake") == 0) {
        wake_sound_requested = true;
        return true;
    }
    if (strcmp(cmd, "sound thinking") == 0 || strcmp(cmd, "sound stop") == 0) {
        sound_thinking(cmd[6] == 't');
        return true;
    }
    if (strncmp(cmd, "xvf ", 4) == 0) {
        xvf_command(cmd + 4);
        return true;
    }
    const uint8_t *op = strcmp(cmd, "loop on") == 0    ? OP_L_REFERENCE
                        : strcmp(cmd, "loop mic") == 0 ? OP_L_MIC
                        : strcmp(cmd, "loop off") == 0 ? OP_L_BEAM
                                                       : NULL;
    if (op == NULL) {
        return false;
    }
    uint8_t now[2] = {0, 0};
    esp_err_t err = xvf_write(XVF_AUDIO_MGR_RESID, XVF_AUDIO_MGR_OP_L, op, 2);
    if (err == ESP_OK) {
        err = xvf_read(XVF_AUDIO_MGR_RESID, XVF_AUDIO_MGR_OP_L, now, 2);
    }
    keryx_console_printf("left capture channel: %s (mux %u, %u)%s\n",
                         op == OP_L_REFERENCE ? "echo reference" : op == OP_L_MIC ? "microphone 0, before AEC" :
                         "processed beam", now[0], now[1], err == ESP_OK && memcmp(now, op, 2) == 0 ? "" : " FAILED");
    return true;
}

// {"type":"sound"} from the bridge
static void bridge_sound(const char *name)
{
    if (strcmp(name, "wake") == 0) {
        wake_sound_requested = true;
    } else if (strcmp(name, "thinking") == 0 || strcmp(name, "stop") == 0) {
        sound_thinking(name[0] == 't');
    }
}

void app_main(void)
{
    // Until I2S takes over, the playback line floats while the XVF3800 keeps clocking the bus, and the codec plays
    // whatever it picks up; hold it at zero instead. We are its only driver. From then on the playback task keeps
    // writing, silence included.
    gpio_config_t dout_cfg = {.pin_bit_mask = 1ULL << PIN_I2S_DOUT, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&dout_cfg);
    gpio_set_level(PIN_I2S_DOUT, 0);

    // the model was trained on the ASR channel at this gain; without it everything arrives 12 dB quieter
    esp_err_t err = xvf_control_init(PIN_I2C_SDA, PIN_I2C_SCL);
    if (err == ESP_OK) {
        xvf_set_float_when_ready(XVF_AEC_RESID, XVF_AEC_ASROUTGAIN, ASR_GAIN, "XVF3800 ASR output gain");
    } else {
        ESP_LOGW(TAG, "no I2C to the XVF3800, ASR output gain left at its default: %s", esp_err_to_name(err));
    }

    // printed whenever a host opens the serial port: the start-up log is long gone by then
    snprintf(banner, sizeof(banner), "Keryx %s: last reset %s, wake word threshold %.2f, esp-dsp decimator check %d "
             "(0 or 1 is fine)", keryx_net_id(), reset_reason(), KWW_THRESHOLD, kww_decimate_self_check());
    mic_buffer = xStreamBufferCreate(MIC_BUFFER_MS * MS_BYTES, 1);
    play_buffer = xStreamBufferCreate(PLAY_BUFFER_MS * MS_BYTES, 1);
    ESP_ERROR_CHECK(mic_buffer == NULL || play_buffer == NULL ? ESP_ERR_NO_MEM : ESP_OK);

    i2s_start();
    xTaskCreatePinnedToCore(playback_task, "playback", 4096, NULL, 7, NULL, 1);
    xTaskCreatePinnedToCore(capture_task, "capture", 8192, NULL, 6, NULL, 1);
    vTaskDelay(pdMS_TO_TICKS(SETTLE_MS));
    ESP_LOGI(TAG, "I2S slave running, %d Hz; USB starts in %d s", SAMPLE_RATE, CONSOLE_GRACE_MS / 1000);
    vTaskDelay(pdMS_TO_TICKS(CONSOLE_GRACE_MS));
    ESP_LOGI(TAG, "handing USB to TinyUSB; the log moves to its serial port");

    uac_device_config_t config = {
        .output_cb = uac_output_cb,
        .input_cb = uac_input_cb,
        .set_mute_cb = uac_set_mute_cb,
        .set_volume_cb = uac_set_volume_cb,
    };
    ESP_ERROR_CHECK(uac_device_init(&config));
    ESP_ERROR_CHECK(keryx_console_start(banner, console_command));
    err = keryx_net_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi did not start: %s", esp_err_to_name(err));
    }
    const keryx_link_callbacks_t link_callbacks = {.sound = bridge_sound};
    err = keryx_link_start(&link_callbacks);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bridge link did not start: %s", esp_err_to_name(err));
    }
}
