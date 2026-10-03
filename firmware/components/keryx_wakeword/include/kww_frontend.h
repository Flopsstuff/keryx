/*
 * micro_speech features for the wake word model: 16 kHz audio in, a 40-band frame out every 10 ms.
 *
 * The TFLite Micro audio frontend configured exactly as pymicro-features configures it (30 ms window, 10 ms step,
 * 40 bands over 125-7500 Hz, noise reduction, PCAN, log scale), and scaled to the same floats, so the model sees
 * on the device what it saw in training.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"

#define KWW_SAMPLE_RATE 16000
#define KWW_FRAME_SAMPLES 160  // 10 ms

typedef struct {
    struct FrontendState state;
} kww_frontend_t;

bool kww_frontend_init(kww_frontend_t *f);

// Feeds samples; writes a frame of features into frames[] for every 10 ms step completed, up to max_frames,
// and returns how many it wrote. Samples beyond the last completed step stay buffered for the next call.
int kww_frontend_process(kww_frontend_t *f, const int16_t *samples, size_t count, float (*frames)[40], int max_frames);
