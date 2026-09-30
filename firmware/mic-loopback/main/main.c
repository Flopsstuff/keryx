/*
 * Microphone loopback on the XIAO ESP32S3: the XVF3800's I2S capture goes straight back out to the headphone jack,
 * processed beam into the left ear and ASR beam into the right, with about 10 ms of buffering on the ESP32.
 *
 * The XVF3800 must run the I2S firmware, which clocks the bus; the ESP32 is the I2S slave.
 */

#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "loopback";

// XIAO ESP32S3 on the reSpeaker Flex
#define PIN_I2S_BCLK GPIO_NUM_8  // D9
#define PIN_I2S_LRCK GPIO_NUM_7  // D8
#define PIN_I2S_DOUT GPIO_NUM_44 // D7, to XVF3800 I2S DATA0 (playback and AEC reference)
#define PIN_I2S_DIN GPIO_NUM_43  // D6, from XVF3800 I2S DATA1 (processed audio)

#define SAMPLE_RATE 48000
#define FRAMES_PER_BLOCK 96 // 2 ms, keeps the loopback delay short
#define DMA_BLOCKS 4
#define SILENCE_MS 1300     // after starting I2S; sound sent earlier comes out with noise
#define REPORT_MS 1000

static double to_dbfs(double value)
{
    return 20.0 * log10(value + 1e-12);
}

static void loopback_task(void *arg)
{
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = DMA_BLOCKS;
    chan_cfg.dma_frame_num = FRAMES_PER_BLOCK;
    chan_cfg.auto_clear = true; // silence rather than stale buffers if we fall behind
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx, &rx));

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
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));
    ESP_LOGI(TAG, "I2S slave running, %d Hz; loopback starts in %d ms", SAMPLE_RATE, SILENCE_MS);

    static int32_t block[FRAMES_PER_BLOCK * 2];
    const int64_t loop_from = esp_timer_get_time() + SILENCE_MS * 1000;
    double sum_sq[2] = {0}, peak[2] = {0};
    uint32_t frames = 0;
    int64_t last_report = loop_from;

    while (true) {
        size_t got = 0, written;
        if (i2s_channel_read(rx, block, sizeof(block), &got, 1000) != ESP_OK || got == 0) {
            ESP_LOGW(TAG, "no I2S data; is the XVF3800 running the I2S firmware?");
            continue;
        }
        int64_t now = esp_timer_get_time();
        if (now < loop_from) {
            memset(block, 0, got);
        } else {
            for (size_t i = 0; i < got / sizeof(int32_t); i++) {
                double v = block[i] / 2147483648.0;
                sum_sq[i & 1] += v * v;
                if (fabs(v) > peak[i & 1]) peak[i & 1] = fabs(v);
            }
            frames += got / (2 * sizeof(int32_t));
        }
        i2s_channel_write(tx, block, got, &written, 1000);

        if (frames && now - last_report >= REPORT_MS * 1000) {
            ESP_LOGI(TAG, "L (processed) rms %6.1f pk %6.1f | R (ASR) rms %6.1f pk %6.1f dBFS",
                     to_dbfs(sqrt(sum_sq[0] / frames)), to_dbfs(peak[0]),
                     to_dbfs(sqrt(sum_sq[1] / frames)), to_dbfs(peak[1]));
            sum_sq[0] = sum_sq[1] = peak[0] = peak[1] = 0;
            frames = 0;
            last_report = now;
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

    xTaskCreatePinnedToCore(loopback_task, "loopback", 4096, NULL, 5, NULL, 1);
}
