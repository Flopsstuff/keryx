/*
 * "Hey Keryx" wake word on the XIAO ESP32S3.
 *
 * The XVF3800's ASR output (the right I2S channel, 48 kHz) goes down to 16 kHz with the filter the training audio
 * went through, into the micro_speech frontend and the streaming model, all from components/keryx_wakeword. A
 * detection is logged and answered with a short beep on the headphone jack; the XVF3800 reads that line as its
 * echo reference, so the beep does not reach the ASR channel. Once a second the log shows the highest score, the
 * ASR channel's peak level and how long processing 10 ms of audio takes, split into decimation, features and model.
 *
 * The XVF3800 must run the I2S firmware, which clocks the bus; the ESP32 is the I2S slave. Logs go over the
 * XIAO's USB serial/JTAG console.
 */

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "kww_decimate.h"
#include "kww_frontend.h"
#include "kww_model.h"
#include "xvf_control.h"

static const char *TAG = "wakeword";

// XIAO ESP32S3 on the reSpeaker Flex
#define PIN_I2C_SDA GPIO_NUM_5   // D4
#define PIN_I2C_SCL GPIO_NUM_6   // D5
#define PIN_I2S_BCLK GPIO_NUM_8  // D9
#define PIN_I2S_LRCK GPIO_NUM_7  // D8
#define PIN_I2S_DOUT GPIO_NUM_44 // D7, to XVF3800 I2S DATA0 (playback and AEC reference)
#define PIN_I2S_DIN GPIO_NUM_43  // D6, from XVF3800 I2S DATA1 (L processed, R ASR)

#define SAMPLE_RATE 48000
#define BLOCK_FRAMES 480        // 10 ms at 48 kHz
#define ASR_GAIN 4.0f           // AEC_ASROUTGAIN, as in usb-soundcard and the recordings the model learned from
#define REFRACTORY_MS 1000      // one detection per phrase
#define WARMUP_MS 2500          // the model needs KWW_RECEPTIVE frames of history before its scores mean anything
#define REPORT_MS 1000
#define BEEP_HZ 880.0f
#define BEEP_MS 120
#define BEEP_DBFS -18.0f

static i2s_chan_handle_t i2s_tx, i2s_rx;
static TaskHandle_t beep_task_handle;
static int decimate_check;  // kww_decimate_self_check() at start-up, logged with the first report

static void i2s_start(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = BLOCK_FRAMES / 2;
    chan_cfg.auto_clear = true; // silence whenever no beep is playing
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
    ESP_ERROR_CHECK(i2s_channel_enable(i2s_tx));
    ESP_ERROR_CHECK(i2s_channel_enable(i2s_rx));
}

// Plays a short tone, with 10 ms fades so it does not click, each time it is notified.
static void beep_task(void *arg)
{
    static int32_t tone[SAMPLE_RATE * BEEP_MS / 1000 * 2];
    const int frames = SAMPLE_RATE * BEEP_MS / 1000;
    const int fade = SAMPLE_RATE / 100;
    const float amplitude = powf(10.0f, BEEP_DBFS / 20.0f) * 2147483647.0f;
    for (int i = 0; i < frames; i++) {
        float env = i < fade ? (float)i / fade : i > frames - fade ? (float)(frames - i) / fade : 1.0f;
        int32_t v = (int32_t)(amplitude * env * sinf(2.0f * (float)M_PI * BEEP_HZ * i / SAMPLE_RATE));
        tone[2 * i] = tone[2 * i + 1] = v;
    }
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        size_t written;
        i2s_channel_write(i2s_tx, tone, sizeof(tone), &written, 1000);
    }
}

static void wake_task(void *arg)
{
    static int32_t raw[BLOCK_FRAMES * 2];
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
    int64_t last_report = start, last_detection = 0, decimate_us = 0, frontend_us = 0, model_us = 0;
    int blocks = 0, peak_level = 0;
    float peak_score = 0.0f;

    for (;;) {
        size_t got = 0;
        if (i2s_channel_read(i2s_rx, raw, sizeof(raw), &got, 1000) != ESP_OK || got != sizeof(raw)) {
            ESP_LOGW(TAG, "I2S read returned %u bytes", (unsigned)got);
            continue;
        }
        int64_t t0 = esp_timer_get_time();

        // the right slot is the ASR output; its top 16 bits are what usb-soundcard sent and we recorded
        for (int i = 0; i < BLOCK_FRAMES; i++) {
            asr[i] = (int16_t)(raw[2 * i + 1] >> 16);
            int level = asr[i] < 0 ? -asr[i] : asr[i];
            peak_level = level > peak_level ? level : peak_level;
        }
        size_t n16 = kww_decimate(&decimate, asr, BLOCK_FRAMES, audio16);
        int64_t t1 = esp_timer_get_time();
        decimate_us += t1 - t0;
        int nframes = kww_frontend_process(&frontend, audio16, n16, frames, 4);
        t0 = esp_timer_get_time();
        frontend_us += t0 - t1;
        for (int f = 0; f < nframes; f++) {
            float score;
            t1 = esp_timer_get_time();
            bool ready = kww_push(&model, frames[f], &score);
            model_us += esp_timer_get_time() - t1;
            if (!ready) {
                continue;
            }
            const int64_t now = esp_timer_get_time();
            if (now - start < WARMUP_MS * 1000LL) {
                continue;
            }
            peak_score = score > peak_score ? score : peak_score;
            if (score >= KWW_THRESHOLD && now - last_detection > REFRACTORY_MS * 1000LL) {
                last_detection = now;
                ESP_LOGI(TAG, ">>> Hey Keryx! (score %.3f)", score);
                xTaskNotifyGive(beep_task_handle);
            }
        }
        blocks++;

        const int64_t now = esp_timer_get_time();
        if (now - last_report >= REPORT_MS * 1000LL) {
            ESP_LOGI(TAG, "peak score %.3f | ASR peak %.1f dBFS | per 10 ms: %.3f ms = decimate %.3f + features %.3f "
                          "+ model %.3f", peak_score, 20.0f * log10f((peak_level + 1) / 32768.0f),
                     (decimate_us + frontend_us + model_us) / 1000.0f / blocks, decimate_us / 1000.0f / blocks,
                     frontend_us / 1000.0f / blocks, model_us / 1000.0f / blocks);
            if (decimate_check != INT32_MIN) {
                // logged here, not at start-up: the USB console reconnects too late for the first lines
                ESP_LOGI(TAG, "esp-dsp decimator vs the plain C one: max difference %d (16-bit steps; -2: esp-dsp "
                              "unavailable)", decimate_check);
                decimate_check = INT32_MIN;
            }
            last_report = now;
            decimate_us = frontend_us = model_us = 0;
            blocks = 0;
            peak_level = 0;
            peak_score = 0.0f;
        }
    }
}

void app_main(void)
{
    // Until I2S takes over, the playback line floats while the XVF3800 keeps clocking the bus, and the codec plays
    // whatever it picks up; hold it at zero instead. We are its only driver.
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

    decimate_check = kww_decimate_self_check();
    i2s_start();
    ESP_LOGI(TAG, "listening for \"Hey Keryx\" (threshold %.2f); scores start after %d ms", KWW_THRESHOLD, WARMUP_MS);
    xTaskCreatePinnedToCore(beep_task, "beep", 3072, NULL, 4, &beep_task_handle, 0);
    xTaskCreatePinnedToCore(wake_task, "wake", 8192, NULL, 5, NULL, 1);
}
