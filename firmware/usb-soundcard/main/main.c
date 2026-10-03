/*
 * USB sound card prototype: the XIAO ESP32S3 bridges the XVF3800's I2S bus to a USB Audio Class device.
 *
 * Microphone: XVF3800 capture (48 kHz, 32-bit; L = processed beam, R = ASR beam) goes to the host as 16-bit stereo.
 * Speaker: 16-bit stereo from the host goes out over I2S to the codec and the headphone jack. The XVF3800 reads the
 * same line as its echo-cancellation reference, so AEC keeps working in this mode.
 * The ASR output gain is set over I2C (ASR_GAIN): the chip resets it to 1.0, which leaves speech at a distance
 * around -27 dBFS peak; xvf_control keeps trying until the XVF3800 has booted and taken the value.
 *
 * The XVF3800 must run the I2S firmware, which clocks the bus; the ESP32 is the I2S slave. The USB PHY stays with
 * the USB serial/JTAG console for a few seconds after boot so the start-up log can be read, then TinyUSB takes it
 * and the board is only a sound card. To flash again, hold BOOT while pressing RESET.
 */

#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "usb_device_uac.h"
#include "xvf_control.h"

static const char *TAG = "soundcard";

// XIAO ESP32S3 on the reSpeaker Flex
#define PIN_I2C_SDA GPIO_NUM_5   // D4
#define PIN_I2C_SCL GPIO_NUM_6   // D5
#define PIN_I2S_BCLK GPIO_NUM_8  // D9
#define PIN_I2S_LRCK GPIO_NUM_7  // D8
#define PIN_I2S_DOUT GPIO_NUM_44 // D7, to XVF3800 I2S DATA0 (playback and AEC reference)
#define PIN_I2S_DIN GPIO_NUM_43  // D6, from XVF3800 I2S DATA1 (processed audio)

#define SAMPLE_RATE 48000
#define DMA_FRAMES 240          // 5 ms per DMA buffer
#define CHUNK_FRAMES 480        // conversion scratch size, 10 ms
#define SETTLE_MS 1000          // silence after starting I2S; sound sent earlier comes out with noise
#define CONSOLE_GRACE_MS 3000   // keep the serial console before TinyUSB takes the USB PHY
#define ASR_GAIN 4.0f           // AEC_ASROUTGAIN, +12 dB; the ASR path has no limiter, so leave headroom

static i2s_chan_handle_t i2s_tx, i2s_rx;

// speaker gain in Q15, from the host's volume and mute controls
static volatile int32_t speaker_gain = 32767;
static volatile uint32_t speaker_volume = 100;
static volatile bool speaker_muted;

static void update_speaker_gain(void)
{
    // the component maps the host's -50..0 dB volume range onto 0..100
    double db = speaker_volume / 2.0 - 50.0;
    speaker_gain = speaker_muted || speaker_volume == 0 ? 0 : (int32_t)(pow(10.0, db / 20.0) * 32767.0);
}

static void i2s_start(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = DMA_FRAMES;
    chan_cfg.auto_clear = true; // silence whenever the host is not playing
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

// Host is recording: hand over the next `len` bytes of 16-bit stereo, read from I2S at the XVF3800's pace.
static esp_err_t uac_input_cb(uint8_t *buf, size_t len, size_t *bytes_read, void *ctx)
{
    static int32_t raw[CHUNK_FRAMES * 2];
    int16_t *out = (int16_t *)buf;
    size_t frames = len / (2 * sizeof(int16_t));
    size_t done = 0;
    while (done < frames) {
        size_t want = frames - done < CHUNK_FRAMES ? frames - done : CHUNK_FRAMES;
        size_t got = 0;
        if (i2s_channel_read(i2s_rx, raw, want * 2 * sizeof(int32_t), &got, 100) != ESP_OK || got == 0) {
            break;
        }
        size_t samples = got / sizeof(int32_t);
        for (size_t i = 0; i < samples; i++) {
            out[done * 2 + i] = (int16_t)(raw[i] >> 16);
        }
        done += samples / 2;
    }
    *bytes_read = done * 2 * sizeof(int16_t);
    return ESP_OK;
}

// Host is playing: widen 16-bit stereo to the 32-bit I2S slots, applying the host's volume.
static esp_err_t uac_output_cb(uint8_t *buf, size_t len, void *ctx)
{
    static int32_t pcm[CHUNK_FRAMES * 2];
    const int16_t *in = (const int16_t *)buf;
    size_t samples = len / sizeof(int16_t);
    int32_t gain = speaker_gain;
    for (size_t start = 0; start < samples; start += CHUNK_FRAMES * 2) {
        size_t n = samples - start < CHUNK_FRAMES * 2 ? samples - start : CHUNK_FRAMES * 2;
        for (size_t i = 0; i < n; i++) {
            pcm[i] = in[start + i] * gain * 2; // Q15 gain; stays inside int32 even at full scale
        }
        size_t written;
        i2s_channel_write(i2s_tx, pcm, n * sizeof(int32_t), &written, 100);
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

void app_main(void)
{
    // Until I2S takes over, the playback line floats while the XVF3800 keeps clocking the bus, and the codec plays
    // whatever it picks up; hold it at zero instead. We are its only driver.
    gpio_config_t dout_cfg = {.pin_bit_mask = 1ULL << PIN_I2S_DOUT, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&dout_cfg);
    gpio_set_level(PIN_I2S_DOUT, 0);

    // the sound card works without it, just quieter on the ASR channel
    esp_err_t err = xvf_control_init(PIN_I2C_SDA, PIN_I2C_SCL);
    if (err == ESP_OK) {
        xvf_set_float_when_ready(XVF_AEC_RESID, XVF_AEC_ASROUTGAIN, ASR_GAIN, "XVF3800 ASR output gain");
    } else {
        ESP_LOGW(TAG, "no I2C to the XVF3800, ASR output gain left at its default: %s", esp_err_to_name(err));
    }

    i2s_start();
    vTaskDelay(pdMS_TO_TICKS(SETTLE_MS));
    ESP_LOGI(TAG, "I2S slave running, %d Hz; USB sound card starts in %d s", SAMPLE_RATE, CONSOLE_GRACE_MS / 1000);
    vTaskDelay(pdMS_TO_TICKS(CONSOLE_GRACE_MS));
    ESP_LOGI(TAG, "handing USB to TinyUSB; the serial console goes away now");

    uac_device_config_t config = {
        .output_cb = uac_output_cb,
        .input_cb = uac_input_cb,
        .set_mute_cb = uac_set_mute_cb,
        .set_volume_cb = uac_set_volume_cb,
    };
    ESP_ERROR_CHECK(uac_device_init(&config));
}
