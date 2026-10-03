/*
 * 48 kHz -> 16 kHz with the same low-pass filter scipy.signal.resample_poly(x, 1, 3) uses, which made the
 * 16 kHz audio the wake word model was trained on from the board's 48 kHz recordings.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    float history[64];  // the last KWW_DECIMATE_TAPS input samples, ring
    int pos;
    int phase;  // input samples since the last output, 0..2
} kww_decimate_t;

void kww_decimate_reset(kww_decimate_t *d);

// Filters `count` 48 kHz samples and writes every third filtered one to out (16-bit, clipped); returns how many.
size_t kww_decimate(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out);
