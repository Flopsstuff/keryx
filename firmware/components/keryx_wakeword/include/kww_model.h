/*
 * Streaming inference of the "Hey Keryx" wake word model (wakeword/train.py's MixedNet), in plain float C.
 *
 * Feed it one 40-band micro_speech feature frame every 10 ms; every KWW_STRIDE frames it produces the
 * probability that the wake phrase has just ended. Each layer keeps only the history its kernel needs, so a step
 * costs a few tens of thousands of multiply-adds instead of rerunning the whole 1.9 s window.
 *
 * The weights come from wakeword/export_model.py, which writes kww_config.h (shapes, threshold) and the private
 * kww_weights.h (float weights, batch norm folded into the pointwise convolutions).
 */
#pragma once

#include <stdbool.h>

#include "kww_config.h"

typedef struct {
    float input[KWW_FIRST_KERNEL][KWW_BANDS];         // last frames, ring
    int input_pos;                                    // next slot to write in input
    long frames;                                      // frames pushed so far
    float history[KWW_BLOCKS][KWW_MAX_KERNEL][KWW_FILTERS];  // inputs of each block's depthwise conv, ring
    int history_pos[KWW_BLOCKS];
} kww_state_t;

void kww_reset(kww_state_t *s);

// Pushes one feature frame (KWW_BANDS values, the float scale pymicro-features gives). Returns true and sets
// *probability every KWW_STRIDE frames; the first ones after a reset are meaningless until KWW_RECEPTIVE frames
// have been pushed.
bool kww_push(kww_state_t *s, const float *frame, float *probability);
