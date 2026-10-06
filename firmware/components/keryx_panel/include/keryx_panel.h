/*
 * The control panel on the peripherals' I2C bus, both modules Adafruit seesaw boards on STEMMA QT:
 *   - the I2C QT Rotary Encoder (0x36): turns, a short press and a long press, handed to the application;
 *   - the NeoDriver (0x60) with a ring of WS2812: what Keryx is doing, and the volume for a moment after it changes.
 * Either module may be missing or plugged in later: each is looked for again every 2 s until it answers, and again
 * after an I2C error.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

typedef enum {
    KERYX_PANEL_IDLE,       // a clock, once SNTP has set the time
    KERYX_PANEL_OFFLINE,    // no bridge: one dim pixel breathing
    KERYX_PANEL_LISTENING,  // the microphone streams to the bridge
    KERYX_PANEL_THINKING,   // waiting for the answer: a comet going round
    KERYX_PANEL_SPEAKING,   // playing the answer: the ring follows the speech
    KERYX_PANEL_MUTED,      // the microphone is muted: the clock with red marks
} keryx_panel_state_t;

typedef struct {
    void (*turned)(int steps);           // detents, clockwise positive
    void (*pressed)(void);               // a short press, on release
    void (*long_pressed)(void);          // held for 1 s, while still held; no short press follows
    keryx_panel_state_t (*state)(void);  // asked every frame
    int (*volume)(void);                 // 0..100; shown on the ring for a moment whenever it changes
    int (*speech_level)(void);           // peak of the answer being played, 0..32767
    int (*update_progress)(void);        // 0..100 while new firmware downloads (shown above everything), else -1
} keryx_panel_callbacks_t;

// Starts the panel task; call once the callbacks can be used.
esp_err_t keryx_panel_start(i2c_master_bus_handle_t bus, const keryx_panel_callbacks_t *callbacks);

// How the ring is mounted: the pixel at 12 o'clock, and whether the pixel numbers run anticlockwise. The ring
// draws clockwise from 12: the clock, the volume arc, the comet.
void keryx_panel_set_layout(int top, bool reversed);

// The brightness at most, in % of full (1..100), by day and by night, and when each begins (minutes after midnight,
// local time). Until SNTP has set the clock it is day.
void keryx_panel_set_brightness(int day, int night, int day_minute, int night_minute);

// Whether the ring is at its night brightness now.
bool keryx_panel_night(void);

// One line for the console: which modules answer, I2C errors since the start.
void keryx_panel_report(char *buf, size_t size);
