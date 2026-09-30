/*
 * Bring-up for the XIAO ESP32S3 on the reSpeaker Flex with the XVF3800 running the I2S firmware.
 *
 * 1. Scans the I2C bus and reads the XVF3800's version and output routing over its I2C control protocol.
 * 2. Listens to BCLK, LRCLK and MCLK as inputs to find out who drives the I2S clocks, without driving them.
 * 3. Starts full-duplex I2S as slave if the XVF3800 already clocks the bus, or as master otherwise, then logs the
 *    level of both capture channels and plays a quiet 440 Hz beep to the headphone jack.
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/pulse_cnt.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "bringup";

// XIAO ESP32S3 on the reSpeaker Flex (Seeed wiki schematic and I2S example)
#define PIN_I2C_SDA GPIO_NUM_5   // D4
#define PIN_I2C_SCL GPIO_NUM_6   // D5
#define PIN_I2S_MCLK GPIO_NUM_9  // D10, never driven by us
#define PIN_I2S_BCLK GPIO_NUM_8  // D9
#define PIN_I2S_LRCK GPIO_NUM_7  // D8
#define PIN_I2S_DOUT GPIO_NUM_44 // D7, to XVF3800 I2S DATA0 (playback and AEC reference)
#define PIN_I2S_DIN GPIO_NUM_43  // D6, from XVF3800 I2S DATA1 (processed audio)

#define XVF_ADDR 0x2C
#define XVF_STATUS_OK 0
#define XVF_STATUS_RETRY 64

#define SAMPLE_RATE 48000
#define FRAMES_PER_BLOCK 480 // 10 ms
#define REPORT_MS 500
#define TONE_HZ 440.0
#define TONE_DBFS -24.0
#define TONE_ON_MS 400
#define TONE_PERIOD_MS 2000
#define SETTLE_MS 300 // after enabling I2S: play silence and ignore capture while the slave locks onto the frames
#define LEAD_IN_MS 1000 // silence before the first beep, to tell start-up noise apart from the tone

static i2c_master_bus_handle_t i2c_bus;
static i2c_master_dev_handle_t xvf;

// latest DOA_VALUE, refreshed by doa_task so the audio loop never waits on I2C
static volatile uint16_t doa_degrees;
static volatile bool doa_speech, doa_valid;

// ---------- XVF3800 control over I2C ----------

// Write {resid, cmd | 0x80, length + 1}, then read back a status byte followed by the payload.
static esp_err_t xvf_read(uint8_t resid, uint8_t cmd, uint8_t *out, size_t len)
{
    uint8_t request[3] = {resid, (uint8_t)(cmd | 0x80), (uint8_t)(len + 1)};
    uint8_t response[64];
    ESP_RETURN_ON_FALSE(len + 1 <= sizeof(response), ESP_ERR_INVALID_SIZE, TAG, "read too long");
    for (int attempt = 0; attempt < 50; attempt++) {
        ESP_RETURN_ON_ERROR(i2c_master_transmit(xvf, request, sizeof(request), 100), TAG, "request %u:%u", resid, cmd);
        ESP_RETURN_ON_ERROR(i2c_master_receive(xvf, response, len + 1, 100), TAG, "response %u:%u", resid, cmd);
        if (response[0] == XVF_STATUS_OK) {
            memcpy(out, response + 1, len);
            return ESP_OK;
        }
        if (response[0] != XVF_STATUS_RETRY) {
            ESP_LOGW(TAG, "resid %u cmd %u: status %u", resid, cmd, response[0]);
            return ESP_FAIL;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return ESP_ERR_TIMEOUT;
}

static void i2c_start(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = false, // the board has 4.7 kΩ pull-ups to VDDIO
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &i2c_bus));

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_ADDR,
        .scl_speed_hz = 100000,
        .scl_wait_us = 20000, // the XVF3800 stretches the clock while it prepares a response
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(i2c_bus, &dev_cfg, &xvf));
}

static void i2c_scan(void)
{
    char found[128] = "";
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
        if (i2c_master_probe(i2c_bus, addr, 20) == ESP_OK) {
            size_t used = strlen(found);
            snprintf(found + used, sizeof(found) - used, " 0x%02X", addr);
        }
    }
    ESP_LOGI(TAG, "I2C devices:%s", found[0] ? found : " none");
}

static void xvf_report(void)
{
    uint8_t version[3], build[50] = {0}, op_l[2], op_r[2], upsample[2], packed[2], hp_level;
    if (xvf_read(48, 0, version, sizeof(version)) != ESP_OK) {
        ESP_LOGE(TAG, "XVF3800 does not answer on I2C 0x%02X", XVF_ADDR);
        return;
    }
    ESP_LOGI(TAG, "XVF3800 firmware %u.%u.%u", version[0], version[1], version[2]);
    if (xvf_read(48, 1, build, sizeof(build) - 1) == ESP_OK) {
        ESP_LOGI(TAG, "XVF3800 build '%s'", (char *)build);
    }
    if (xvf_read(35, 15, op_l, 2) == ESP_OK && xvf_read(35, 19, op_r, 2) == ESP_OK) {
        ESP_LOGI(TAG, "output mux: L = %u,%u  R = %u,%u", op_l[0], op_l[1], op_r[0], op_r[1]);
    }
    if (xvf_read(35, 14, upsample, 2) == ESP_OK && xvf_read(35, 13, packed, 2) == ESP_OK) {
        ESP_LOGI(TAG, "upsample L,R = %u,%u  packed L,R = %u,%u", upsample[0], upsample[1], packed[0], packed[1]);
    }
    if (xvf_read(48, 11, &hp_level, 1) == ESP_OK) {
        ESP_LOGI(TAG, "AIC3104 headphone level %u (0..9)", hp_level);
    }
}

// ---------- clock detection ----------

// Count rising edges on a pin for a while; the pin only ever acts as an input here.
static double measure_hz(gpio_num_t pin)
{
    const int limit = 30000;
    pcnt_unit_config_t unit_cfg = {.low_limit = -limit, .high_limit = limit, .flags.accum_count = 1};
    pcnt_unit_handle_t unit;
    ESP_ERROR_CHECK(pcnt_new_unit(&unit_cfg, &unit));
    pcnt_chan_config_t chan_cfg = {.edge_gpio_num = pin, .level_gpio_num = -1};
    pcnt_channel_handle_t chan;
    ESP_ERROR_CHECK(pcnt_new_channel(unit, &chan_cfg, &chan));
    ESP_ERROR_CHECK(pcnt_channel_set_edge_action(chan, PCNT_CHANNEL_EDGE_ACTION_INCREASE, PCNT_CHANNEL_EDGE_ACTION_HOLD));
    ESP_ERROR_CHECK(pcnt_unit_add_watch_point(unit, limit)); // lets the driver accumulate past the 16-bit limit
    ESP_ERROR_CHECK(pcnt_unit_enable(unit));
    ESP_ERROR_CHECK(pcnt_unit_clear_count(unit));
    ESP_ERROR_CHECK(pcnt_unit_start(unit));
    int64_t started = esp_timer_get_time();
    vTaskDelay(pdMS_TO_TICKS(100));
    int count = 0;
    ESP_ERROR_CHECK(pcnt_unit_get_count(unit, &count));
    int64_t elapsed = esp_timer_get_time() - started;
    pcnt_unit_stop(unit);
    pcnt_unit_disable(unit);
    pcnt_unit_remove_watch_point(unit, limit);
    pcnt_del_channel(chan);
    pcnt_del_unit(unit);
    return count * 1e6 / elapsed;
}

// ---------- audio ----------

static double to_dbfs(double value)
{
    return 20.0 * log10(value + 1e-12);
}

static void audio_task(void *arg)
{
    i2s_role_t role = (i2s_role_t)(intptr_t)arg;
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, role);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = FRAMES_PER_BLOCK;
    chan_cfg.auto_clear = true; // send silence rather than stale buffers if we fall behind
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

    static int32_t in[FRAMES_PER_BLOCK * 2], out[FRAMES_PER_BLOCK * 2];
    memset(out, 0, sizeof(out));
    size_t written;
    i2s_channel_preload_data(tx, out, sizeof(out), &written);
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));
    ESP_LOGI(TAG, "I2S running as %s, %d Hz, 32-bit stereo", role == I2S_ROLE_MASTER ? "master" : "slave", SAMPLE_RATE);

    const double amplitude = pow(10.0, TONE_DBFS / 20.0) * 2147483647.0;
    const double step = 2.0 * M_PI * TONE_HZ / SAMPLE_RATE;
    double phase = 0;
    uint32_t frame_counter = 0;
    const uint32_t settle_frames = SAMPLE_RATE * SETTLE_MS / 1000;
    uint32_t settled = 0;
    double sum_sq[2] = {0}, peak[2] = {0};
    uint32_t frames = 0, timeouts = 0;
    int64_t last_report = esp_timer_get_time();

    while (true) {
        // short beeps so the tone is easy to tell apart from anything else in the headphones
        bool settling = settled < settle_frames;
        for (int i = 0; i < FRAMES_PER_BLOCK; i++) {
            const uint32_t lead_in = SAMPLE_RATE * LEAD_IN_MS / 1000;
            bool on = !settling && frame_counter >= lead_in &&
                      ((frame_counter - lead_in) % (SAMPLE_RATE * TONE_PERIOD_MS / 1000)) < SAMPLE_RATE * TONE_ON_MS / 1000;
            int32_t s = on ? (int32_t)(amplitude * sin(phase)) : 0;
            if (on) phase = fmod(phase + step, 2.0 * M_PI);
            if (!settling) frame_counter++;
            out[2 * i] = s;
            out[2 * i + 1] = s;
        }
        i2s_channel_write(tx, out, sizeof(out), &written, 1000);

        size_t got = 0;
        if (i2s_channel_read(rx, in, sizeof(in), &got, 1000) != ESP_OK || got == 0) {
            timeouts++;
        }
        if (settling) {
            settled += got / (2 * sizeof(int32_t));
            last_report = esp_timer_get_time();
            continue;
        }
        for (size_t i = 0; i < got / sizeof(int32_t); i++) {
            double v = in[i] / 2147483648.0;
            int ch = i & 1;
            sum_sq[ch] += v * v;
            if (fabs(v) > peak[ch]) peak[ch] = fabs(v);
        }
        frames += got / (2 * sizeof(int32_t));

        int64_t now = esp_timer_get_time();
        if (now - last_report >= REPORT_MS * 1000) {
            double seconds = (now - last_report) / 1e6;
            ESP_LOGI(TAG, "L rms %6.1f pk %6.1f | R rms %6.1f pk %6.1f dBFS | %5.0f fps%s | DoA %s%u° %s",
                     to_dbfs(frames ? sqrt(sum_sq[0] / frames) : 0), to_dbfs(peak[0]),
                     to_dbfs(frames ? sqrt(sum_sq[1] / frames) : 0), to_dbfs(peak[1]),
                     frames / seconds, timeouts ? " (read timeouts)" : "",
                     doa_valid ? "" : "?", doa_degrees, doa_speech ? "speech" : "");
            sum_sq[0] = sum_sq[1] = peak[0] = peak[1] = 0;
            frames = timeouts = 0;
            last_report = now;
        }
    }
}

static void doa_task(void *arg)
{
    while (true) {
        uint8_t doa[4];
        doa_valid = xvf_read(20, 18, doa, sizeof(doa)) == ESP_OK;
        if (doa_valid) {
            doa_degrees = doa[0] | (doa[1] << 8);
            doa_speech = doa[2] != 0;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void app_main(void)
{
    // Until I2S takes over, the playback data line floats while the XVF3800 keeps clocking the bus, and the codec
    // plays whatever it picks up; hold it at zero (silence) instead. We are its only driver.
    gpio_config_t dout_cfg = {.pin_bit_mask = 1ULL << PIN_I2S_DOUT, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&dout_cfg);
    gpio_set_level(PIN_I2S_DOUT, 0);

    // the USB serial port re-enumerates after a reset; give the host time to reopen it before the summary
    vTaskDelay(pdMS_TO_TICKS(2500));
    ESP_LOGI(TAG, "reSpeaker Flex bring-up");
    i2c_start();
    i2c_scan();
    xvf_report();

    double bclk = measure_hz(PIN_I2S_BCLK);
    double lrck = measure_hz(PIN_I2S_LRCK);
    double mclk = measure_hz(PIN_I2S_MCLK);
    ESP_LOGI(TAG, "clocks before we drive anything: BCLK %.0f Hz, LRCLK %.0f Hz, MCLK %.0f Hz", bclk, lrck, mclk);

    // If the XVF3800 already clocks the bus, driving BCLK/LRCLK too would fight it
    i2s_role_t role = (bclk > 1000 || lrck > 1000) ? I2S_ROLE_SLAVE : I2S_ROLE_MASTER;
    xTaskCreatePinnedToCore(audio_task, "audio", 8192, (void *)(intptr_t)role, 5, NULL, 1);
    xTaskCreatePinnedToCore(doa_task, "doa", 4096, NULL, 2, NULL, 0);
}
