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
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "keryx_console.h"
#include "keryx_link.h"
#include "keryx_net.h"
#include "keryx_ota.h"
#include "keryx_panel.h"
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
#define PIN_PERIPH_SDA GPIO_NUM_1 // D0, our own I2C peripherals (STEMMA QT), on a bus of their own
#define PIN_PERIPH_SCL GPIO_NUM_4 // D3
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
static volatile int64_t last_i2s_read_us;      // the XVF3800 clocks I2S: no reads means it runs other firmware
static volatile uint32_t play_underruns;       // the host's audio ran out while it was still playing
static volatile uint32_t play_overflows;       // packets that did not fit into the playback buffer
static volatile uint32_t play_dropped_bytes;   // dropped because the host ran ahead
static volatile uint32_t play_received_bytes;  // from the host, to measure its real rate
static volatile uint32_t i2s_tx_late;          // DMA blocks that went out as silence: the playback task was late

// speaker gain in Q15, from the host's volume and mute controls
static volatile int32_t speaker_gain = 32767;

// the board's own volume, 0..100, over everything it plays; kept in NVS
#define VOLUME_DEFAULT 100
#define VOLUME_STEP 10
#define VOLUME_SAVE_MS 2000     // a knob turned quickly writes flash once
static volatile int volume = VOLUME_DEFAULT;
static esp_timer_handle_t settings_save_timer;  // volume and mute go to NVS a moment after they change

// the microphone muted: nothing captured leaves the board (bridge, USB) and the wake word does not run; kept in NVS
#define MUTE_SAVE_MS 200        // sooner than the volume: a mute that a power cut forgets is no mute
static volatile bool mic_muted;
// What goes to speech-to-text (the bridge): the processed beam (L: the XVF3800's residual echo suppression, noise
// suppression and AGC), the ASR beam (R: what the wake word hears, no post-processing, so Keryx's own voice stays in
// it), or auto: L while the board plays and STT_AUTO_HOLD_MS after, R otherwise, with a STT_FADE_MS crossfade. L is
// brought to R's level (scaled by ASR gain / AGC gain), so the level does not jump between them. `stt channel`.
typedef enum { STT_L, STT_R, STT_AUTO } stt_channel_t;
#define STT_FADE_MS 30
#define STT_AUTO_HOLD_MS 1000   // the echo's tail, after the last audible block
#define STT_AGC_POLL_MS 500
#define OUT_AUDIBLE 64          // an output block peaking above this (-54 dBFS) counts as the board playing
static volatile stt_channel_t stt_channel = STT_L;
static volatile float stt_l_scale = ASR_GAIN / 28.0f;  // until the AGC gain has been read
static volatile int64_t last_audible_out_us;          // the playback task's last audible block
static volatile float stt_l_weight;                   // 0: all R, 1: all L; for `status`

static int ring_top;          // the ring's pixel at 12 o'clock, kept in NVS (`ring top`)
static bool ring_reversed;    // its pixel numbers run anticlockwise (`ring reverse`)
static int ring_day = 20, ring_night = 5;           // brightness in % (`ring brightness`)
static int ring_day_from = 7 * 60, ring_night_from = 22 * 60;  // minutes after midnight
static volatile uint32_t speaker_volume = 100;
static volatile bool speaker_muted;
static volatile int64_t last_playback_us;

// interface sounds: requests from other tasks; the playback task keeps the positions
static volatile bool wake_sound_requested;
static volatile int mute_sound_requested;  // 1: muted, 2: unmuted
static volatile bool thinking_requested;
static volatile int bridge_peak;  // of the last block of the bridge's answer, for the ring
static volatile int64_t thinking_started_us;

static void settings_save(void *arg)
{
    nvs_handle_t nvs;
    if (nvs_open("keryx", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, "volume", (uint8_t)volume);
        nvs_set_u8(nvs, "muted", mic_muted ? 1 : 0);
        nvs_set_u8(nvs, "ring_top", (uint8_t)ring_top);
        nvs_set_u8(nvs, "ring_rev", ring_reversed ? 1 : 0);
        nvs_set_u8(nvs, "stt_ch", (uint8_t)stt_channel);
        nvs_set_u8(nvs, "ring_day", (uint8_t)ring_day);
        nvs_set_u8(nvs, "ring_night", (uint8_t)ring_night);
        nvs_set_u16(nvs, "ring_day_at", (uint16_t)ring_day_from);
        nvs_set_u16(nvs, "ring_night_at", (uint16_t)ring_night_from);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

static void settings_save_in(int ms)
{
    if (settings_save_timer != NULL) {
        esp_timer_stop(settings_save_timer);
        esp_timer_start_once(settings_save_timer, ms * 1000LL);
    }
}

// Mutes or unmutes the microphone, with a sound when it changes; the bridge hears about it either way.
static void mute_set(bool on)
{
    if (on != mic_muted) {
        mic_muted = on;
        mute_sound_requested = on ? 1 : 2;
        settings_save_in(MUTE_SAVE_MS);
        keryx_console_printf("microphone %s\n", on ? "muted" : "on");
    }
    keryx_link_mute_changed(on);
}

static TaskHandle_t xvf_task_handle;

// The volume in dB: 0.5 dB per step from -50 dB at 1 to 0 dB at 100; 0 is silence
static float volume_db(int value)
{
    return value == 0 ? -100.0f : value / 2.0f - 50.0f;
}

// The volume is the codec's: the TLV320AIC3104's analog volume in front of each output driver (page 0, Table 10-51:
// 0.5 dB a step down to -50 dB, soft-stepped by the codec). It comes after the DAC, whose hiss goes down with it (a
// software gain before the DAC left the hiss as loud at any volume), and after the point the XVF3800 takes its AEC
// reference, so the XVF3800 is told it as AEC_FAR_EXTGAIN. Volume v > 0 sets the headphone outputs to 100 - v steps
// (0 dB at 100, where the XVF3800 has them) and the line output, which mixes both DACs for the amplifier on the
// SPEAKER connector, 12 steps lower: 0 dB of mix for the same signal on both. The XVF3800 had it at -12 dB and our
// volume before it. 0 mutes the routes and the output drivers. The XVF3800 sets up the codec at boot, possibly after
// we do, so the registers are checked and set again when they change.
#define CODEC_DAC_L_TO_HPLOUT 47
#define CODEC_DAC_R_TO_HPROUT 64
#define CODEC_DAC_L_TO_LOP 82
#define CODEC_DAC_R_TO_LOP 85
#define CODEC_HPLOUT_LEVEL 51
#define CODEC_HPROUT_LEVEL 65
#define CODEC_LOP_LEVEL 86
#define CODEC_ROUTED 0x80      // in a volume register: the DAC goes to that output
#define CODEC_VOLUME_MUTE 118  // the analog volume's mute setting
#define CODEC_NOT_MUTED 0x08   // in a level register
#define CODEC_LINE_HEADROOM 12
#define CODEC_CHECK_MS 2000

static esp_err_t codec_set_volume(int v)
{
    const uint8_t hp = CODEC_ROUTED | (v == 0 ? CODEC_VOLUME_MUTE : 100 - v);
    const uint8_t line = CODEC_ROUTED | (v == 0 ? CODEC_VOLUME_MUTE : 100 - v + CODEC_LINE_HEADROOM);
    const uint8_t volumes[][2] = {{CODEC_DAC_L_TO_HPLOUT, hp}, {CODEC_DAC_R_TO_HPROUT, hp},
                                  {CODEC_DAC_L_TO_LOP, line}, {CODEC_DAC_R_TO_LOP, line}};
    esp_err_t err = ESP_OK;
    for (size_t i = 0; i < sizeof(volumes) / sizeof(volumes[0]) && err == ESP_OK; i++) {
        err = codec_write(volumes[i][0], volumes[i][1]);
    }
    // the drivers' levels stay as the XVF3800 set them; only their mute bit changes (bit 1 is a read-only status)
    const uint8_t levels[] = {CODEC_HPLOUT_LEVEL, CODEC_HPROUT_LEVEL, CODEC_LOP_LEVEL};
    for (size_t i = 0; i < sizeof(levels) && err == ESP_OK; i++) {
        uint8_t level;
        err = codec_read(levels[i], &level);
        const uint8_t want = (level & ~(CODEC_NOT_MUTED | 0x02)) | (v == 0 ? 0 : CODEC_NOT_MUTED);
        if (err == ESP_OK && want != (level & ~0x02)) {
            err = codec_write(levels[i], want);
        }
    }
    return err;
}

// Whether the codec still has volume v (the XVF3800 rewrites it when it restarts)
static bool codec_has_volume(int v)
{
    uint8_t line, level;
    return codec_read(CODEC_DAC_L_TO_LOP, &line) == ESP_OK && codec_read(CODEC_LOP_LEVEL, &level) == ESP_OK &&
           line == (CODEC_ROUTED | (v == 0 ? CODEC_VOLUME_MUTE : 100 - v + CODEC_LINE_HEADROOM)) &&
           (level & CODEC_NOT_MUTED) == (v == 0 ? 0 : CODEC_NOT_MUTED);
}

// AEC_FAR_EXTGAIN is the gain between the AEC reference and the loudspeaker (XMOS' user guide 4.1.2: the USB variant
// applies the host's volume there), so it follows the volume. While the volume was a software gain before the
// reference, setting it so was wrong: told -23 dB at 54, the residual echo suppression expected that much less echo
// than there was, and Keryx's own voice came through the processed beam at +20.6 dB over silence instead of +3.1 dB.
// PP_NLATTENONOFF is the last stage of the processed beam's echo suppression: it attenuates the non-linear echo
// (distortion, the cabinet's vibration) the AEC cannot subtract. Seeed's firmware has it off; measured in the speaker,
// it takes Keryx's voice in the processed beam from +6 dB over the room's noise to below it, at volume 54 and at 80,
// with the user's voice over it as loud as before. The XVF3800 forgets both at power-off, and does not answer at
// first after power-up: everything is retried.
// The task also follows the AGC gain of the processed beam, for bringing L to R's level (stt_l_scale). A task of its
// own, as the XVF3800 stretches the clock.
static esp_err_t xvf_set_int(uint8_t resid, uint8_t cmd, int32_t value)
{
    int32_t check;
    esp_err_t err = xvf_write(resid, cmd, &value, sizeof(value));
    if (err == ESP_OK) {
        err = xvf_read(resid, cmd, &check, sizeof(check));
    }
    return err == ESP_OK && check != value ? ESP_ERR_INVALID_RESPONSE : err;
}

static void xvf_task(void *arg)
{
    while (xvf_set_int(XVF_PP_RESID, XVF_PP_NLATTENONOFF, 1) != ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    ESP_LOGI(TAG, "XVF3800: non-linear echo attenuation on");
    int on_codec = -1;
    float far_gain = NAN;
    int64_t checked = 0, polled = 0;
    for (;;) {
        const int v = volume;
        const int64_t now = esp_timer_get_time();
        if (v != on_codec) {
            const esp_err_t err = codec_set_volume(v);
            if (err == ESP_OK) {
                on_codec = v;
                checked = now;
            } else {
                ESP_LOGW(TAG, "codec volume %d: %s, trying again", v, esp_err_to_name(err));
            }
        } else if (now - checked > CODEC_CHECK_MS * 1000LL) {
            checked = now;
            if (!codec_has_volume(v)) {
                ESP_LOGI(TAG, "codec volume registers changed (the XVF3800 set the codec up): setting volume %d", v);
                on_codec = -1;
                continue;
            }
        }
        if (v > 0 && volume_db(v) != far_gain &&
            xvf_set_float(XVF_AEC_RESID, XVF_AEC_FAR_EXTGAIN, volume_db(v)) == ESP_OK) {
            far_gain = volume_db(v);
        }
        float agc;
        if (now - polled >= STT_AGC_POLL_MS * 1000LL) {
            polled = now;
            if (stt_channel != STT_R && xvf_read(XVF_PP_RESID, XVF_PP_AGCGAIN, &agc, sizeof(agc)) == ESP_OK &&
                agc >= 0.1f && agc <= 1000.0f) {
                stt_l_scale = ASR_GAIN / agc;
            }
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(on_codec == v ? STT_AGC_POLL_MS : 500));
    }
}

// 0 is silence; above it, 0.5 dB per step from -50 dB at 0 to 0 dB at 100, as the host's volume control. The codec
// applies it (xvf_task).
static void volume_set(int value)
{
    value = value < 0 ? 0 : value > 100 ? 100 : value;
    volume = value;
    settings_save_in(VOLUME_SAVE_MS);
    keryx_link_volume_changed(value);
    if (xvf_task_handle != NULL) {
        xTaskNotifyGive(xvf_task_handle);
    }
}

static void settings_load(void)
{
    nvs_handle_t nvs;
    uint8_t stored = VOLUME_DEFAULT, muted = 0, top = 0, reversed = 0;
    if (nvs_open("keryx", NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_u8(nvs, "volume", &stored);
        nvs_get_u8(nvs, "muted", &muted);
        nvs_get_u8(nvs, "ring_top", &top);
        nvs_get_u8(nvs, "ring_rev", &reversed);
        uint8_t channel;
        if (nvs_get_u8(nvs, "stt_ch", &channel) == ESP_OK && channel <= STT_AUTO) {
            stt_channel = (stt_channel_t)channel;
        }
        uint8_t day, night;
        uint16_t day_from, night_from;
        if (nvs_get_u8(nvs, "ring_day", &day) == ESP_OK && day >= 1 && day <= 100) {
            ring_day = day;
        }
        if (nvs_get_u8(nvs, "ring_night", &night) == ESP_OK && night >= 1 && night <= 100) {
            ring_night = night;
        }
        if (nvs_get_u16(nvs, "ring_day_at", &day_from) == ESP_OK && day_from < 24 * 60) {
            ring_day_from = day_from;
        }
        if (nvs_get_u16(nvs, "ring_night_at", &night_from) == ESP_OK && night_from < 24 * 60) {
            ring_night_from = night_from;
        }
        nvs_close(nvs);
    }
    keryx_panel_set_brightness(ring_day, ring_night, ring_day_from, ring_night_from);
    ring_top = top;
    ring_reversed = reversed != 0;
    keryx_panel_set_layout(ring_top, ring_reversed);
    const esp_timer_create_args_t timer = {.callback = settings_save, .name = "settings_save"};
    esp_timer_create(&timer, &settings_save_timer);
    mic_muted = muted != 0;
    volume = stored > 100 ? VOLUME_DEFAULT : stored;
}

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
    const int16_t *shot = NULL;  // a one-shot sound: the wake chime or a mute sound
    int shot_len = 0, shot_pos = 0;
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
            shot = keryx_sound_wake, shot_len = KERYX_SOUND_WAKE_LEN, shot_pos = 0;
        }
        int mute_sound = mute_sound_requested;
        if (mute_sound) {
            mute_sound_requested = 0;
            shot = mute_sound == 1 ? keryx_sound_mute_on : keryx_sound_mute_off;
            shot_len = mute_sound == 1 ? KERYX_SOUND_MUTE_ON_LEN : KERYX_SOUND_MUTE_OFF_LEN;
            shot_pos = 0;
        }
        int host_peak = 0;
        for (int i = 0; i < BLOCK_FRAMES * 2; i++) {
            int level = in[i] < 0 ? -in[i] : in[i];
            host_peak = level > host_peak ? level : host_peak;
        }
        int peak = 0;
        for (int i = 0; i < BLOCK_FRAMES; i++) {
            int level = bridge[i] < 0 ? -bridge[i] : bridge[i];
            peak = level > peak ? level : peak;
        }
        bridge_peak = peak;
        host_peak = peak > host_peak ? peak : host_peak;
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
            if (shot_pos < shot_len) {
                sound += shot[shot_pos++];
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
        for (int i = 0; i < BLOCK_FRAMES; i++) {
            const int32_t s = out[2 * i] >> 16;
            if (s > OUT_AUDIBLE || s < -OUT_AUDIBLE) {
                last_audible_out_us = esp_timer_get_time();  // for the auto STT channel
                break;
            }
        }
        size_t written;
        i2s_channel_write(i2s_tx, out, sizeof(out), &written, portMAX_DELAY);
    }
}

// The 16 kHz audio for speech-to-text, from R (the ASR beam, as the wake word hears it) and L (the processed beam):
// see stt_channel. The weight of L moves towards its target by one STT_FADE_MS ramp; L is scaled to R's level.
static void stt_mix(const int16_t *r, const int16_t *l, size_t n, int16_t *out)
{
    static float weight;
    const stt_channel_t mode = stt_channel;
    const bool playing = esp_timer_get_time() - last_audible_out_us < STT_AUTO_HOLD_MS * 1000LL;
    const float target = mode == STT_L ? 1.0f : mode == STT_R ? 0.0f : playing ? 1.0f : 0.0f;
    const float step = 1.0f / (STT_FADE_MS * 16), scale = stt_l_scale;
    for (size_t i = 0; i < n; i++) {
        weight = weight < target ? fminf(target, weight + step) : fmaxf(target, weight - step);
        const float v = (1.0f - weight) * r[i] + weight * scale * l[i];
        out[i] = v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : (int16_t)lrintf(v);
    }
    stt_l_weight = weight;
}

// Reads I2S, hands the audio to the USB microphone and runs the wake word on the ASR channel.
static void capture_task(void *arg)
{
    static int32_t raw[BLOCK_FRAMES * 2];
    static int16_t pcm[BLOCK_FRAMES * 2];
    static int16_t asr[BLOCK_FRAMES];
    static int16_t audio16[BLOCK_FRAMES / 3 + 2];
    static int16_t proc[BLOCK_FRAMES];               // L, the processed beam, for speech-to-text
    static int16_t proc16[BLOCK_FRAMES / 3 + 2];
    static int16_t stt16[BLOCK_FRAMES / 3 + 2];
    static float frames[4][40];
    static kww_decimate_t decimate;
    static kww_frontend_t frontend;
    static kww_state_t model;

    kww_decimate_reset(&decimate);
    // L's decimator in PSRAM (16-byte aligned, as esp-dsp wants): internal RAM is short and speed matters less here
    kww_decimate_t *decimate_l = heap_caps_aligned_alloc(16, sizeof(kww_decimate_t), MALLOC_CAP_SPIRAM);
    ESP_ERROR_CHECK(decimate_l == NULL ? ESP_ERR_NO_MEM : ESP_OK);
    kww_decimate_reset(decimate_l);
    if (!kww_frontend_init(&frontend)) {
        ESP_LOGE(TAG, "micro_speech frontend failed to initialise");
        vTaskDelete(NULL);
    }
    kww_reset(&model);

    const int64_t start = esp_timer_get_time();
    int64_t listening_since = start;  // the model needs a warm-up after boot and after a mute
    bool was_muted = false;
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
        last_i2s_read_us = t0;

        // the top 16 bits are what goes to the host; the right slot is the ASR output the model learned from
        const bool muted = mic_muted;
        for (int i = 0; i < BLOCK_FRAMES * 2; i++) {
            pcm[i] = muted ? 0 : (int16_t)(raw[i] >> 16);
        }
        // whole blocks only, so the channels never swap; the buffer fills up while the host is not recording
        if (xStreamBufferSpacesAvailable(mic_buffer) >= sizeof(pcm)) {
            xStreamBufferSend(mic_buffer, pcm, sizeof(pcm), 0);
        } else if (esp_timer_get_time() - last_mic_read_us < 100000) {
            capture_overruns++;
        }

        if (muted) {
            // silence for the bridge's preroll too, and no wake word at all
            memset(audio16, 0, BLOCK_FRAMES / 3 * sizeof(int16_t));
            keryx_link_audio_up(audio16, BLOCK_FRAMES / 3);
            was_muted = true;
        } else if (was_muted) {
            // start listening afresh: no state left from before the mute
            kww_decimate_reset(&decimate);
            kww_decimate_reset(decimate_l);
            FrontendReset(&frontend.state);
            kww_reset(&model);
            listening_since = esp_timer_get_time();
            was_muted = false;
        }
        for (int i = 0; i < BLOCK_FRAMES && !muted; i++) {
            asr[i] = pcm[2 * i + 1];
            proc[i] = pcm[2 * i];
            int level = asr[i] < 0 ? -asr[i] : asr[i];
            peak_level = level > peak_level ? level : peak_level;
        }
        size_t n16 = muted ? 0 : kww_decimate(&decimate, asr, BLOCK_FRAMES, audio16);
        if (!muted) {
            kww_decimate(decimate_l, proc, BLOCK_FRAMES, proc16);  // the same count: both see whole blocks
            stt_mix(audio16, proc16, n16, stt16);
            keryx_link_audio_up(stt16, n16);
        }
        int nframes = muted ? 0 : kww_frontend_process(&frontend, audio16, n16, frames, 4);
        for (int f = 0; f < nframes; f++) {
            float score;
            if (!kww_push(&model, frames[f], &score)) {
                continue;
            }
            const int64_t now = esp_timer_get_time();
            if (now - listening_since < WARMUP_MS * 1000LL) {
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

static i2c_master_bus_handle_t periph_bus;  // NULL if it could not be set up

// Our peripherals do not share the XVF3800's bus: with the Adafruit rotary encoder (seesaw on a SAMD09) on it, the
// XVF3800 holds SCL for up to half a second after its address and stops answering. The modules have 10 kΩ pull-ups.
static void periph_bus_init(void)
{
    i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_NUM_1,
        .sda_io_num = PIN_PERIPH_SDA,
        .scl_io_num = PIN_PERIPH_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,  // keeps the lines high when nothing is plugged in
    };
    esp_err_t err = i2c_new_master_bus(&cfg, &periph_bus);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no I2C bus for the peripherals: %s", esp_err_to_name(err));
        periph_bus = NULL;
    }
}

// Every address that acknowledges on one bus, with what we expect there, and every address where the probe failed
// other than by a NACK (the bus was held, e.g. by a hung device).
static int i2c_scan_bus(i2c_master_bus_handle_t bus, const char *bus_name)
{
    int found = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
        esp_err_t err = i2c_master_probe(bus, addr, 20);
        if (err == ESP_ERR_NOT_FOUND) {
            continue;
        }
        const char *name = addr == 0x18           ? "TLV320AIC3104 codec"
                           : addr == XVF_I2C_ADDR ? "XVF3800"
                           : addr == 0x36         ? "seesaw rotary encoder"
                           : addr == 0x49         ? "seesaw ATtiny817"
                           : addr == 0x60         ? "seesaw NeoDriver"
                                                  : "unknown";
        if (err != ESP_OK) {
            keryx_console_printf("%s 0x%02x %s: %s\n", bus_name, addr, esp_err_to_name(err), name);
            continue;
        }
        keryx_console_printf("%s 0x%02x %s\n", bus_name, addr, name);
        found++;
    }
    return found;
}

// "i2c scan": both buses, the XVF3800's (D4/D5) and the peripherals' (D0/D3)
static void i2c_scan_command(void)
{
    int found = i2c_scan_bus(xvf_i2c_bus(), "xvf");
    if (periph_bus != NULL) {
        found += i2c_scan_bus(periph_bus, "periph");
    }
    keryx_console_printf("ok %d devices\n", found);
}

// "i2c read [xvf] <addr> <n> [bytes...]": write the bytes (a register address), wait 5 ms (seesaw needs the time),
// read n bytes. "i2c write [xvf] <addr> <bytes...>". Numbers in C notation (0x36). On the peripherals' bus, or with
// `xvf` on the XVF3800's (the codec at 0x18; the XVF3800 itself has the `xvf` command), where a read is one
// transaction instead.
static void i2c_raw_command(const char *args)
{
    const bool read = strncmp(args, "read ", 5) == 0;
    char *p = (char *)args + (read ? 5 : 6);
    i2c_master_bus_handle_t bus = periph_bus;
    if (strncmp(p, "xvf ", 4) == 0) {
        bus = xvf_i2c_bus();
        p += 4;
    }
    const long addr = strtol(p, &p, 0);
    const long n = read ? strtol(p, &p, 0) : 0;
    uint8_t out[16], in[32];
    size_t count = 0;
    for (char *end; count < sizeof(out); p = end) {
        const long v = strtol(p, &end, 0);
        if (end == p) {
            break;
        }
        out[count++] = (uint8_t)v;
    }
    if (bus == NULL) {
        keryx_console_printf("error no peripheral bus\n");
        return;
    }
    if (addr < 0x08 || addr > 0x77 || n < 0 || n > (long)sizeof(in) || (read ? n == 0 : count == 0)) {
        keryx_console_printf("error usage: i2c read [xvf] <addr> <n> [bytes...] | i2c write [xvf] <addr> <bytes...>\n");
        return;
    }
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = (uint16_t)addr,
        .scl_speed_hz = 100000,
    };
    i2c_master_dev_handle_t dev;
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &dev);
    if (err == ESP_OK) {
        if (read && count > 0 && bus != periph_bus) {
            // the codec wants a repeated start: after a stop it reads from the register after the one written
            err = i2c_master_transmit_receive(dev, out, count, in, n, 50);
        } else if (count > 0) {
            err = i2c_master_transmit(dev, out, count, 50);
        }
        if (err == ESP_OK && read && (count == 0 || bus == periph_bus)) {
            vTaskDelay(pdMS_TO_TICKS(5));
            err = i2c_master_receive(dev, in, n, 50);
        }
        i2c_master_bus_rm_device(dev);
    }
    if (err != ESP_OK) {
        keryx_console_printf("error %s\n", esp_err_to_name(err));
        return;
    }
    char line[16 + 3 * sizeof(in)];
    int len = snprintf(line, sizeof(line), "ok");
    for (long i = 0; i < n; i++) {
        len += snprintf(line + len, sizeof(line) - len, " %02x", in[i]);
    }
    keryx_console_printf("%s\n", line);
}

// "ring brightness [day|night <1-100> [HH:MM]]": the ring's brightness by day and by night, and when each begins
static void ring_brightness_command(const char *args)
{
    if (*args != '\0') {
        char which[8], at[8] = "";
        int percent, hours, minutes;
        const int n = sscanf(args, "%7s %d %7s", which, &percent, at);
        const bool day = strcmp(which, "day") == 0;
        if (n < 2 || (!day && strcmp(which, "night") != 0) || percent < 1 || percent > 100 ||
            (n == 3 && (sscanf(at, "%d:%d", &hours, &minutes) != 2 || hours < 0 || hours > 23 || minutes < 0 ||
                        minutes > 59))) {
            keryx_console_printf("error usage: ring brightness [day|night <1-100> [HH:MM]]\n");
            return;
        }
        *(day ? &ring_day : &ring_night) = percent;
        if (n == 3) {
            *(day ? &ring_day_from : &ring_night_from) = hours * 60 + minutes;
        }
        keryx_panel_set_brightness(ring_day, ring_night, ring_day_from, ring_night_from);
        settings_save_in(VOLUME_SAVE_MS);
    }
    keryx_console_printf("ok ring brightness day=%d%% from %02d:%02d, night=%d%% from %02d:%02d, now %s\n", ring_day,
                         ring_day_from / 60, ring_day_from % 60, ring_night, ring_night_from / 60,
                         ring_night_from % 60, keryx_panel_night() ? "night" : "day");
}

// Serial port commands beyond keryx_console's own.
static bool console_command(const char *cmd)
{
    if (keryx_ota_command(cmd)) {
        return true;
    }
    if (keryx_net_command(cmd)) {
        if (strncmp(cmd, "set bridge ", 11) == 0 || strncmp(cmd, "set token ", 10) == 0 || strcmp(cmd, "erase") == 0) {
            keryx_link_restart();
        }
        return true;
    }
    if (strcmp(cmd, "mute") == 0 || strcmp(cmd, "mute on") == 0 || strcmp(cmd, "mute off") == 0) {
        if (cmd[4] != '\0') {
            mute_set(cmd[6] == 'n');
        }
        keryx_console_printf("ok muted=%d\n", mic_muted ? 1 : 0);
        return true;
    }
    if (strcmp(cmd, "wake") == 0 && mic_muted) {
        keryx_console_printf("error the microphone is muted (mute off)\n");
        return true;
    }
    if (strcmp(cmd, "wake") == 0) {
        // as if the wake word fired, for testing the bridge without saying it
        wake_sound_requested = true;
        keryx_link_wake(1.0f);
        keryx_console_printf("wake score=1.000 (console)\n");
        keryx_console_printf("ok\n");
        return true;
    }
    if (strcmp(cmd, "sound wake") == 0) {
        wake_sound_requested = true;
        keryx_console_printf("ok %s\n", cmd);
        return true;
    }
    if (strcmp(cmd, "sound thinking") == 0 || strcmp(cmd, "sound stop") == 0) {
        sound_thinking(cmd[6] == 't');
        keryx_console_printf("ok %s\n", cmd);
        return true;
    }
    if (strcmp(cmd, "volume") == 0 || strncmp(cmd, "volume ", 7) == 0) {
        const char *arg = cmd + 6;
        while (*arg == ' ') {
            arg++;
        }
        if (strcmp(arg, "up") == 0 || strcmp(arg, "down") == 0) {
            volume_set(volume + (arg[0] == 'u' ? VOLUME_STEP : -VOLUME_STEP));
        } else if (*arg != '\0') {
            char *end;
            long v = strtol(arg, &end, 10);
            if (*end != '\0' || v < 0 || v > 100) {
                keryx_console_printf("error usage: volume [0-100 | up | down]\n");
                return true;
            }
            volume_set((int)v);
        }
        keryx_console_printf("ok volume %d\n", volume);
        return true;
    }
    if (strncmp(cmd, "xvf ", 4) == 0) {
        xvf_command(cmd + 4);
        return true;
    }
    if (strcmp(cmd, "stt channel") == 0 || strncmp(cmd, "stt channel ", 12) == 0) {
        // what speech-to-text hears: l (processed), r (ASR beam) or a (auto: l while playing)
        const char *arg = cmd + 11 + (cmd[11] == ' ');
        if (*arg != '\0') {
            if ((arg[0] != 'l' && arg[0] != 'r' && arg[0] != 'a') || arg[1] != '\0') {
                keryx_console_printf("error usage: stt channel [l|r|a]\n");
                return true;
            }
            stt_channel = arg[0] == 'l' ? STT_L : arg[0] == 'r' ? STT_R : STT_AUTO;
            settings_save_in(VOLUME_SAVE_MS);
        }
        keryx_console_printf("ok stt channel %c, now %.0f %% L, L scaled by %.1f dB\n", "lra"[stt_channel],
                             stt_l_weight * 100.0f, 20.0f * log10f(stt_l_scale));
        return true;
    }
    if (strcmp(cmd, "ring brightness") == 0 || strncmp(cmd, "ring brightness ", 16) == 0) {
        ring_brightness_command(cmd + 15 + (cmd[15] == ' '));
        return true;
    }
    if (strcmp(cmd, "ring") == 0 || strncmp(cmd, "ring top ", 9) == 0 || strcmp(cmd, "ring reverse") == 0) {
        // where 12 o'clock is and which way is clockwise, as the ring is mounted
        if (cmd[4] != '\0' && cmd[5] == 't') {
            char *end;
            long top = strtol(cmd + 9, &end, 10);
            if (*end != '\0' || top < 0 || top > 23) {
                keryx_console_printf("error usage: ring top <0-23>\n");
                return true;
            }
            ring_top = (int)top;
        } else if (cmd[4] != '\0') {
            ring_reversed = !ring_reversed;
        }
        keryx_panel_set_layout(ring_top, ring_reversed);
        settings_save_in(VOLUME_SAVE_MS);
        keryx_console_printf("ok ring top=%d reversed=%d\n", ring_top, ring_reversed ? 1 : 0);
        return true;
    }
    if (strcmp(cmd, "i2c scan") == 0) {
        i2c_scan_command();
        return true;
    }
    if (strncmp(cmd, "i2c read ", 9) == 0 || strncmp(cmd, "i2c write ", 10) == 0) {
        i2c_raw_command(cmd + 4);
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

// `status`: is the XVF3800 there, on which firmware, and does it clock I2S (only its I2S firmware does)
static void status_xvf(void)
{
    uint8_t version[3];
    bool answers = xvf_read(48, 0, version, sizeof(version)) == ESP_OK;  // VERSION
    bool i2s = esp_timer_get_time() - last_i2s_read_us < 1000000;
    if (answers) {
        float far_gain = NAN;
        int32_t nl_atten = -1;
        xvf_read(XVF_AEC_RESID, XVF_AEC_FAR_EXTGAIN, &far_gain, sizeof(far_gain));
        xvf_read(XVF_PP_RESID, XVF_PP_NLATTENONOFF, &nl_atten, sizeof(nl_atten));
        keryx_console_printf("xvf=ok version=%u.%u.%u i2s=%s far_extgain=%.1fdB nl_atten=%ld\n", version[0],
                             version[1], version[2], i2s ? "running" : "no_clock", far_gain, (long)nl_atten);
        uint8_t hp = 0, line = 0, level = 0;
        if (codec_read(CODEC_DAC_L_TO_HPLOUT, &hp) == ESP_OK && codec_read(CODEC_DAC_L_TO_LOP, &line) == ESP_OK &&
            codec_read(CODEC_LOP_LEVEL, &level) == ESP_OK) {
            keryx_console_printf("codec=ok headphones=0x%02x line=0x%02x line_level=0x%02x\n", hp, line, level);
        } else {
            keryx_console_printf("codec=no_answer\n");
        }
    } else {
        keryx_console_printf("xvf=no_answer i2s=%s\n", i2s ? "running" : "no_clock");
    }
    keryx_console_printf("volume=%d muted=%d\n", volume, mic_muted ? 1 : 0);
    char line[96];
    keryx_panel_report(line, sizeof(line));
    keryx_console_printf("%s\n", line);
}

// The console over Wi-Fi ({"type":"console"} from the bridge): only commands that cannot lock us out, brick the
// board or break the echo cancellation. Not: set, erase, wifi scan, bootloader, reboot, loop, xvf set, i2c write.
// `ota` is in: a new image that does not survive 30 s is rolled back by the bootloader.
static const char *const REMOTE_COMMANDS[] = {
    "status", "config", "volume", "mute", "wake", "sound", "ring", "top", "log", "xvf get", "i2c scan", "i2c read",
    "net check", "ota", "stt channel",
};

static void bridge_console(int id, const char *line)
{
    bool allowed = false;
    for (size_t i = 0; i < sizeof(REMOTE_COMMANDS) / sizeof(REMOTE_COMMANDS[0]) && !allowed; i++) {
        const size_t n = strlen(REMOTE_COMMANDS[i]);
        allowed = strncmp(line, REMOTE_COMMANDS[i], n) == 0 && (line[n] == '\0' || line[n] == ' ');
    }
    if (!allowed) {
        keryx_link_console_reply(id, "error not allowed over Wi-Fi (only on the serial port)\n");
    } else if (!keryx_console_submit(id, line, keryx_link_console_reply)) {
        keryx_link_console_reply(id, "error busy, or the line is too long\n");
    } else {
        keryx_console_printf("bridge console: %s\n", line);
    }
}

// {"type":"volume"} from the bridge
static void bridge_volume(bool relative, int amount)
{
    volume_set(relative ? volume + amount : amount);
}

static int get_volume(void)
{
    return volume;
}

static bool get_muted(void)
{
    return mic_muted;
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

// The panel: the encoder and the ring (components/keryx_panel)

#define CLOCK_TZ "CET-1CEST,M3.5.0,M10.5.0/3"  // Europe/Warsaw, as POSIX TZ
#define KNOB_VOLUME_STEP 2  // per detent: 0.5 dB a step, so 1 dB a click, the whole range in 2.5 turns

static void panel_turned(int steps)
{
    volume_set(volume + steps * KNOB_VOLUME_STEP);
}

// A short press ends a running conversation, and otherwise acts as the wake word.
static void panel_pressed(void)
{
    if (keryx_link_streaming() || keryx_link_playing()) {
        keryx_console_printf("button: stop\n");
        keryx_link_stop();
    } else if (!mic_muted) {
        keryx_console_printf("button: wake\n");
        wake_sound_requested = true;
        keryx_link_wake(1.0f);
    }
}

// A long press mutes or unmutes the microphone.
static void panel_long_pressed(void)
{
    mute_set(!mic_muted);
}

static keryx_panel_state_t panel_state(void)
{
    if (mic_muted) {
        return KERYX_PANEL_MUTED;
    }
    if (!keryx_link_ready()) {
        return KERYX_PANEL_OFFLINE;
    }
    if (keryx_link_playing()) {
        return KERYX_PANEL_SPEAKING;
    }
    if (thinking_requested) {
        return KERYX_PANEL_THINKING;
    }
    return keryx_link_streaming() ? KERYX_PANEL_LISTENING : KERYX_PANEL_IDLE;
}

static int panel_speech_level(void)
{
    return bridge_peak;
}

void app_main(void)
{
    // Until I2S takes over, the playback line floats while the XVF3800 keeps clocking the bus, and the codec plays
    // whatever it picks up; hold it at zero instead. We are its only driver. From then on the playback task keeps
    // writing, silence included.
    gpio_config_t dout_cfg = {.pin_bit_mask = 1ULL << PIN_I2S_DOUT, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&dout_cfg);
    gpio_set_level(PIN_I2S_DOUT, 0);

    // the volume and, above all, the mute apply from the first captured block
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    settings_load();
    keryx_ota_start_trial();

    // the model was trained on the ASR channel at this gain; without it everything arrives 12 dB quieter
    esp_err_t err = xvf_control_init(PIN_I2C_SDA, PIN_I2C_SCL);
    if (err == ESP_OK) {
        xvf_set_float_when_ready(XVF_AEC_RESID, XVF_AEC_ASROUTGAIN, ASR_GAIN, "XVF3800 ASR output gain");
        // never writes flash: the stack can be in PSRAM
        xTaskCreatePinnedToCoreWithCaps(xvf_task, "xvf", 6144, NULL, 2, &xvf_task_handle, 0, MALLOC_CAP_SPIRAM);
    } else {
        ESP_LOGW(TAG, "no I2C to the XVF3800, ASR output gain left at its default: %s", esp_err_to_name(err));
    }
    periph_bus_init();

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
    keryx_net_status_hook(status_xvf);
    // the clock on the ring: SNTP keeps trying until Wi-Fi is up, then every hour
    setenv("TZ", CLOCK_TZ, 1);
    tzset();
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    sntp.wait_for_sync = false;
    if (esp_netif_sntp_init(&sntp) != ESP_OK) {
        ESP_LOGW(TAG, "SNTP did not start: the ring shows no clock");
    }
    const keryx_link_callbacks_t link_callbacks = {.sound = bridge_sound, .volume = bridge_volume,
                                                   .get_volume = get_volume, .mute = mute_set,
                                                   .get_muted = get_muted, .console = bridge_console};
    err = keryx_link_start(&link_callbacks);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bridge link did not start: %s", esp_err_to_name(err));
    }
    if (periph_bus != NULL) {
        const keryx_panel_callbacks_t panel_callbacks = {.turned = panel_turned, .pressed = panel_pressed,
                                                         .long_pressed = panel_long_pressed, .state = panel_state,
                                                         .volume = get_volume, .speech_level = panel_speech_level,
                                                         .update_progress = keryx_ota_progress};
        err = keryx_panel_start(periph_bus, &panel_callbacks);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "panel did not start: %s", esp_err_to_name(err));
        }
    }
}
