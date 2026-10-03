/*
 * 48 kHz -> 16 kHz with the same low-pass filter scipy.signal.resample_poly(x, 1, 3) uses, which made the
 * 16 kHz audio the wake word model was trained on from the board's 48 kHz recordings.
 *
 * On the ESP32 the filtering runs in esp-dsp's decimating FIR (the ESP32-S3 SIMD build); elsewhere, and as the
 * reference it is checked against, in plain C.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef ESP_PLATFORM
#include "dsps_fir.h"
#endif

#define KWW_DECIMATE_LEN 64     // the 61 taps padded with zeros: esp-dsp on the S3 wants a multiple of 4
#define KWW_DECIMATE_CHUNK 480  // input samples converted per esp-dsp call

// Must be 16-byte aligned for esp-dsp: static, or from an aligned allocation (plain malloc is not enough).
typedef struct {
    float history[KWW_DECIMATE_LEN] __attribute__((aligned(16)));  // delay line
    int pos;
    int phase;  // input samples since the last output, 0..2 (plain C path)
#ifdef ESP_PLATFORM
    int use_dsp;  // esp-dsp accepted the buffers (it needs them 16-byte aligned); otherwise the plain C path runs
    fir_f32_t fir;
    float in[KWW_DECIMATE_CHUNK] __attribute__((aligned(16)));
    float out[KWW_DECIMATE_CHUNK / 3] __attribute__((aligned(16)));
#endif
} kww_decimate_t;

void kww_decimate_reset(kww_decimate_t *d);

// Filters `count` 48 kHz samples and writes every third filtered one to out (16-bit, clipped); returns how many.
// On the ESP32 count must be a multiple of 3.
size_t kww_decimate(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out);

// The plain C filter, for checking the fast one against.
size_t kww_decimate_reference(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out);

// Runs a test signal through both and returns the largest difference, in 16-bit steps (0 or 1 is a match).
int kww_decimate_self_check(void);
