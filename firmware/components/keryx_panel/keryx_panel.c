#include "keryx_panel.h"

#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"

static const char *TAG = "panel";

#define ENCODER_ADDR 0x36
#define DRIVER_ADDR 0x60
#define I2C_HZ 100000
#define I2C_TIMEOUT_MS 20
#define FRAME_MS 33             // 30 frames a second
#define RETRY_MS 2000           // a missing module is looked for again this often
#define LONG_PRESS_MS 1000
#define VOLUME_SHOW_MS 1500     // the ring shows the volume this long after it last changed
#define MAX_STEPS 50            // more detents than this in one frame is a misread, not a turn

#define RING_PIXELS 24

// seesaw registers: module, then function
#define SS_STATUS 0x00
#define SS_STATUS_HW_ID 0x01
#define SS_GPIO 0x01
#define SS_GPIO_DIRCLR 0x03
#define SS_GPIO_BULK 0x04
#define SS_GPIO_BULK_SET 0x05
#define SS_GPIO_PULLENSET 0x0B
#define SS_NEOPIXEL 0x0E
#define SS_NEOPIXEL_PIN 0x01
#define SS_NEOPIXEL_SPEED 0x02
#define SS_NEOPIXEL_BUF_LENGTH 0x03
#define SS_NEOPIXEL_BUF 0x04
#define SS_NEOPIXEL_SHOW 0x05
#define SS_ENCODER 0x11
#define SS_ENCODER_DELTA 0x40   // counts since the last read; reading clears it

#define ENCODER_BUTTON_PIN 24
#define DRIVER_PIN 15           // the NeoDriver's output
#define READ_DELAY_MS 5         // seesaw (the encoder's SAMD09) needs this long between the register and the read
#define CHUNK 24                // pixel bytes per write: seesaw takes 32-byte writes, 4 go to the header

typedef struct {
    const char *name;
    uint8_t addr;
    i2c_master_dev_handle_t dev;
    bool ok;
    int64_t retry_us;
} module_t;

static i2c_master_bus_handle_t bus;
static keryx_panel_callbacks_t cb;
static module_t encoder = {.name = "encoder", .addr = ENCODER_ADDR};
static module_t driver = {.name = "ring", .addr = DRIVER_ADDR};
static volatile uint32_t i2c_errors;

static uint8_t frame[RING_PIXELS * 3];  // GRB, as the WS2812 takes it
static uint8_t shown[RING_PIXELS * 3];
static bool shown_valid;

static esp_err_t ss_write(module_t *m, uint8_t module, uint8_t function, const uint8_t *data, size_t len)
{
    uint8_t buf[4 + CHUNK];
    if (len > sizeof(buf) - 2) {
        return ESP_ERR_INVALID_SIZE;
    }
    buf[0] = module;
    buf[1] = function;
    if (len > 0) {
        memcpy(buf + 2, data, len);
    }
    return i2c_master_transmit(m->dev, buf, 2 + len, I2C_TIMEOUT_MS);
}

static esp_err_t ss_read(module_t *m, uint8_t module, uint8_t function, uint8_t *out, size_t len)
{
    const uint8_t reg[2] = {module, function};
    esp_err_t err = i2c_master_transmit(m->dev, reg, sizeof(reg), I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(READ_DELAY_MS));
    return i2c_master_receive(m->dev, out, len, I2C_TIMEOUT_MS);
}

// An I2C error: the module is looked for again later. A device that hung mid-transfer may hold the bus; the reset
// clocks it free.
static void lost(module_t *m, esp_err_t err, int64_t now)
{
    i2c_errors++;
    m->ok = false;
    m->retry_us = now + RETRY_MS * 1000LL;
    ESP_LOGW(TAG, "%s (0x%02x) stopped answering: %s", m->name, m->addr, esp_err_to_name(err));
    i2c_master_bus_reset(bus);
}

static esp_err_t encoder_setup(void)
{
    uint8_t id;
    esp_err_t err = ss_read(&encoder, SS_STATUS, SS_STATUS_HW_ID, &id, 1);
    // the button: input with pull-up, low while pressed
    const uint8_t mask[4] = {1u << (ENCODER_BUTTON_PIN - 24), 0, 0, 0};
    if (err == ESP_OK) {
        err = ss_write(&encoder, SS_GPIO, SS_GPIO_DIRCLR, mask, sizeof(mask));
    }
    if (err == ESP_OK) {
        err = ss_write(&encoder, SS_GPIO, SS_GPIO_PULLENSET, mask, sizeof(mask));
    }
    if (err == ESP_OK) {
        err = ss_write(&encoder, SS_GPIO, SS_GPIO_BULK_SET, mask, sizeof(mask));
    }
    uint8_t delta[4];
    if (err == ESP_OK) {
        err = ss_read(&encoder, SS_ENCODER, SS_ENCODER_DELTA, delta, sizeof(delta));  // turns from before: dropped
    }
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "encoder at 0x%02x, seesaw hardware id 0x%02x", ENCODER_ADDR, id);
    }
    return err;
}

static esp_err_t driver_setup(void)
{
    uint8_t id;
    esp_err_t err = ss_read(&driver, SS_STATUS, SS_STATUS_HW_ID, &id, 1);
    const uint8_t pin = DRIVER_PIN, speed = 1;  // 800 kHz
    const uint8_t length[2] = {sizeof(frame) >> 8, sizeof(frame) & 0xFF};
    if (err == ESP_OK) {
        err = ss_write(&driver, SS_NEOPIXEL, SS_NEOPIXEL_PIN, &pin, 1);
    }
    if (err == ESP_OK) {
        err = ss_write(&driver, SS_NEOPIXEL, SS_NEOPIXEL_SPEED, &speed, 1);
    }
    if (err == ESP_OK) {
        err = ss_write(&driver, SS_NEOPIXEL, SS_NEOPIXEL_BUF_LENGTH, length, sizeof(length));
    }
    if (err == ESP_OK) {
        shown_valid = false;  // whatever the ring shows now, send a whole frame
        ESP_LOGI(TAG, "ring of %d at 0x%02x, seesaw hardware id 0x%02x", RING_PIXELS, DRIVER_ADDR, id);
    }
    return err;
}

static void module_check(module_t *m, esp_err_t (*setup)(void), int64_t now)
{
    if (m->ok || now < m->retry_us) {
        return;
    }
    m->ok = i2c_master_probe(bus, m->addr, I2C_TIMEOUT_MS) == ESP_OK && setup() == ESP_OK;
    if (!m->ok) {
        m->retry_us = now + RETRY_MS * 1000LL;
    }
}

static void encoder_poll(int64_t now)
{
    static bool was_down, long_done;
    static int64_t down_since;
    uint8_t delta[4], gpio[4];
    esp_err_t err = ss_read(&encoder, SS_ENCODER, SS_ENCODER_DELTA, delta, sizeof(delta));
    if (err == ESP_OK) {
        err = ss_read(&encoder, SS_GPIO, SS_GPIO_BULK, gpio, sizeof(gpio));
    }
    if (err != ESP_OK) {
        lost(&encoder, err, now);
        was_down = false;
        return;
    }
    // the count goes down clockwise (Adafruit's examples negate it too)
    const int32_t counts = (int32_t)((uint32_t)delta[0] << 24 | (uint32_t)delta[1] << 16 | delta[2] << 8 | delta[3]);
    if (counts != 0 && counts >= -MAX_STEPS && counts <= MAX_STEPS) {
        cb.turned(-counts);
    }
    const bool down = !(gpio[0] & (1u << (ENCODER_BUTTON_PIN - 24)));
    if (down && !was_down) {
        down_since = now;
        long_done = false;
    } else if (down && !long_done && now - down_since >= LONG_PRESS_MS * 1000LL) {
        long_done = true;
        cb.long_pressed();
    } else if (!down && was_down && !long_done) {
        cb.pressed();
    }
    was_down = down;
}

// The ring as light, summed: where things overlap (a hand over a mark) their colours add up. Index 0 is 12 o'clock,
// going clockwise; ring_top and ring_reversed map that onto the pixels.
static float light[RING_PIXELS][3];
static volatile int ring_top;
static volatile bool ring_reversed;
// the most any channel gets, in % of full: by day and by night, and the minutes after midnight each begins
static volatile int day_percent = 20, night_percent = 5, day_from = 7 * 60, night_from = 22 * 60;

// whether it is night now; day while the clock is not set
static bool is_night(void)
{
    time_t now = time(NULL);
    if (now < 1700000000) {
        return false;
    }
    struct tm tm;
    localtime_r(&now, &tm);
    const int minute = tm.tm_hour * 60 + tm.tm_min, day = day_from, night = night_from;
    return day <= night ? minute < day || minute >= night : minute >= night && minute < day;
}

// adds a colour (r, g, b, each 0..1) at level 0..1, gamma 2 so that fading looks even
static void add(int i, float r, float g, float b, float level)
{
    level = level < 0 ? 0 : level > 1 ? 1 : level;
    float *p = light[(i % RING_PIXELS + RING_PIXELS) % RING_PIXELS];
    p[0] += r * level * level;
    p[1] += g * level * level;
    p[2] += b * level * level;
}

// as add, but over whatever is there
static void put(int i, float r, float g, float b, float level)
{
    memset(light[(i % RING_PIXELS + RING_PIXELS) % RING_PIXELS], 0, sizeof(light[0]));
    add(i, r, g, b, level);
}

static void fill(float r, float g, float b, float level)
{
    for (int i = 0; i < RING_PIXELS; i++) {
        add(i, r, g, b, level);
    }
}

// light -> frame: GRB bytes, at most the day's or the night's brightness, laid out as the ring is mounted
static void to_frame(void)
{
    const float full = 255 * (is_night() ? night_percent : day_percent) / 100.0f;
    for (int i = 0; i < RING_PIXELS; i++) {
        const int at = ((ring_reversed ? -i : i) + ring_top + RING_PIXELS) % RING_PIXELS;
        uint8_t *px = frame + at * 3;
        for (int c = 0; c < 3; c++) {
            const float v = light[i][c] > 1 ? 1 : light[i][c];
            // at a night's few steps a weak share would round to 0 and change the colour: keep it at 1
            const float x = v * full;
            px[c == 0 ? 1 : c == 1 ? 0 : 2] = x <= 0 ? 0 : x < 1 ? 1 : (uint8_t)(x + 0.5f);  // r, g, b -> G R B
        }
    }
}

// Marks at 12, 3, 6 and 9: amber by day, yellow at night (at a night's 2 % the eye takes R 2 G 1 for red), red when
// muted, the minute hand one blue pixel, the hour hand two green ones;
// nothing until SNTP has set the clock.
static void clock_face(bool muted)
{
    time_t now = time(NULL);
    if (now < 1700000000) {
        return;
    }
    struct tm tm;
    localtime_r(&now, &tm);
    const bool night = is_night();
    for (int mark = 0; mark < RING_PIXELS; mark += RING_PIXELS / 4) {
        if (muted) {
            add(mark, 1, 0, 0, 0.7f);
        } else {
            add(mark, 1, night ? 1 : 0.5f, 0, 0.7f);  // R 12 G 6 at 10 %, R 2 G 2 at 2 %
        }
    }
    // The hands cover the marks; the minute hand, one pixel, goes over the hour hand. Their level: at 2 % (night) the
    // hour hand is G 4 and the minute hand B 4 G 1, at 10 % (day) five times that.
    const float hand = 0.885f;
    const float minutes = tm.tm_min + tm.tm_sec / 60.0f;
    const float hour = (tm.tm_hour % 12 + minutes / 60) * RING_PIXELS / 12;
    const int first = (int)floorf(hour - 0.5f);  // the two pixels nearest the hour hand's angle
    put(first, 0, 1, 0, hand);
    put(first + 1, 0, 1, 0, hand);
    put((int)(minutes * RING_PIXELS / 60), 0, 0.25f, 1, hand);
}

static void render(int64_t now)
{
    static int last_volume = -1;
    static int64_t volume_until;
    static float speech;  // the speech level, smoothed
    const float t = now / 1e6f;
    const float breath = 0.5f + 0.5f * sinf(2 * (float)M_PI * t / 3.0f);  // one breath in 3 s

    memset(light, 0, sizeof(light));
    const int volume = cb.volume();
    if (volume != last_volume) {
        if (last_volume >= 0) {
            volume_until = now + VOLUME_SHOW_MS * 1000LL;
        }
        last_volume = volume;
    }
    const float level = cb.speech_level() / 8000.0f;
    speech += ((level > 1 ? 1 : level) - speech) * (level > speech ? 0.6f : 0.15f);  // quick up, slow down

    const int update = cb.update_progress != NULL ? cb.update_progress() : -1;
    if (update >= 0) {
        // new firmware coming in: a white arc as long as the download so far, the rest faintly blue
        const float done = update * RING_PIXELS / 100.0f;
        for (int i = 0; i < RING_PIXELS; i++) {
            const float part = done - i;
            if (part > 0) {
                add(i, 1, 1, 1, part >= 1 ? 1 : part);
            } else {
                add(i, 0, 0.3f, 1, 0.3f);
            }
        }
    } else if (now < volume_until) {
        // an arc clockwise from 12 o'clock, as long as the volume; the last pixel lit in part
        const float lit = volume * RING_PIXELS / 100.0f;
        for (int i = 0; i < RING_PIXELS; i++) {
            const float part = lit - i;
            add(i, 1.0f, 0.75f, 0.4f, part >= 1 ? 1 : part > 0 ? part : 0.12f);
        }
    } else {
        switch (cb.state()) {
        case KERYX_PANEL_MUTED:
            clock_face(true);
            break;
        case KERYX_PANEL_OFFLINE:
            add(0, 1.0f, 0.35f, 0, 0.2f + 0.3f * breath);
            break;
        case KERYX_PANEL_LISTENING:
            fill(0, 0.6f, 1, 0.65f + 0.35f * breath);
            break;
        case KERYX_PANEL_THINKING: {
            // a comet going round clockwise once a second, its tail a third of the ring
            const float head = fmodf(t, 1.0f) * RING_PIXELS;
            const float tail = RING_PIXELS / 3.0f;
            for (int i = 0; i < RING_PIXELS; i++) {
                const float behind = fmodf(head - i + RING_PIXELS, RING_PIXELS);
                if (behind < tail) {
                    add(i, 0.6f, 0, 1, 1 - behind / tail);
                }
            }
            break;
        }
        case KERYX_PANEL_SPEAKING:
            fill(0.2f, 1, 0.6f, 0.35f + 0.65f * speech);
            break;
        case KERYX_PANEL_IDLE:
            clock_face(false);
            break;
        }
    }
    to_frame();
}

static esp_err_t ring_show(void)
{
    uint8_t buf[2 + CHUNK];
    for (size_t off = 0; off < sizeof(frame); off += CHUNK) {
        const size_t n = sizeof(frame) - off < CHUNK ? sizeof(frame) - off : CHUNK;
        buf[0] = off >> 8;
        buf[1] = off & 0xFF;
        memcpy(buf + 2, frame + off, n);
        esp_err_t err = ss_write(&driver, SS_NEOPIXEL, SS_NEOPIXEL_BUF, buf, 2 + n);
        if (err != ESP_OK) {
            return err;
        }
    }
    return ss_write(&driver, SS_NEOPIXEL, SS_NEOPIXEL_SHOW, NULL, 0);
}

static void panel_task(void *arg)
{
    TickType_t wake = xTaskGetTickCount();
    for (;;) {
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
        int64_t now = esp_timer_get_time();
        module_check(&encoder, encoder_setup, now);
        module_check(&driver, driver_setup, now);
        if (encoder.ok) {
            encoder_poll(now);
        }
        if (driver.ok) {
            render(now);
            // only what changed goes out: an idle ring costs no bus time
            if (!shown_valid || memcmp(frame, shown, sizeof(frame)) != 0) {
                esp_err_t err = ring_show();
                if (err == ESP_OK) {
                    memcpy(shown, frame, sizeof(frame));
                    shown_valid = true;
                } else {
                    lost(&driver, err, esp_timer_get_time());
                }
            }
        }
    }
}

esp_err_t keryx_panel_start(i2c_master_bus_handle_t i2c_bus, const keryx_panel_callbacks_t *callbacks)
{
    bus = i2c_bus;
    cb = *callbacks;
    module_t *modules[] = {&encoder, &driver};
    for (int i = 0; i < 2; i++) {
        i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = modules[i]->addr,
            .scl_speed_hz = I2C_HZ,
        };
        esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &modules[i]->dev);
        if (err != ESP_OK) {
            return err;
        }
    }
    // never writes flash, so its stack can live in PSRAM
    if (xTaskCreatePinnedToCoreWithCaps(panel_task, "panel", 3072, NULL, 3, NULL, 0, MALLOC_CAP_SPIRAM) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void keryx_panel_report(char *buf, size_t size)
{
    snprintf(buf, size, "panel encoder=%s ring=%s i2c_errors=%lu", encoder.ok ? "ok" : "missing",
             driver.ok ? "ok" : "missing", (unsigned long)i2c_errors);
}

void keryx_panel_set_layout(int top, bool reversed)
{
    ring_top = ((top % RING_PIXELS) + RING_PIXELS) % RING_PIXELS;
    ring_reversed = reversed;
}

void keryx_panel_set_brightness(int day, int night, int day_minute, int night_minute)
{
    day_percent = day;
    night_percent = night;
    day_from = day_minute;
    night_from = night_minute;
}

bool keryx_panel_night(void)
{
    return is_night();
}
