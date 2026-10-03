#include "kww_model.h"

#include <math.h>
#include <string.h>

// Constant tables would otherwise stay in flash and be read through the 32 KB data cache, which the ~120 KB of
// weights keep evicting: every inference would stream them from flash again. Internal RAM has room for them.
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define KWW_WEIGHTS DRAM_ATTR
#else
#define KWW_WEIGHTS
#endif
#include "kww_weights.h"

void kww_reset(kww_state_t *s)
{
    memset(s, 0, sizeof(*s));
}

// Depthwise convolution of one block at the newest time step: each channel convolves its own last k inputs,
// k being the kernel of the channel's group, so all groups line up at the end as in MixConv.
static void depthwise(int block, const kww_state_t *s, float *out)
{
    const float *w = kww_dw_weight[block];
    const float *b = kww_dw_bias[block];
    const int pos = s->history_pos[block];
    int channel = 0;
    for (int g = 0; g < kww_block_groups[block]; g++) {
        const int k = kww_group_kernel[block][g];
        for (int c = 0; c < kww_group_channels[block][g]; c++, channel++) {
            float acc = b[channel];
            for (int j = 0; j < k; j++) {
                const int slot = (pos - k + j + 2 * KWW_MAX_KERNEL) % KWW_MAX_KERNEL;
                acc += w[j] * s->history[block][slot][channel];
            }
            w += k;
            out[channel] = acc;
        }
    }
}

static void push_history(kww_state_t *s, int block, const float *values, int count)
{
    memcpy(s->history[block][s->history_pos[block]], values, count * sizeof(float));
    s->history_pos[block] = (s->history_pos[block] + 1) % KWW_MAX_KERNEL;
}

bool kww_push(kww_state_t *s, const float *frame, float *probability)
{
    memcpy(s->input[s->input_pos], frame, sizeof(s->input[0]));
    s->input_pos = (s->input_pos + 1) % KWW_FIRST_KERNEL;
    s->frames++;
    if (s->frames < KWW_FIRST_KERNEL || (s->frames - KWW_FIRST_KERNEL) % KWW_STRIDE != 0) {
        return false;
    }

    // first convolution over the last KWW_FIRST_KERNEL frames, then ReLU
    float x[KWW_FILTERS];
    for (int o = 0; o < KWW_FIRST_FILTERS; o++) {
        float acc = kww_first_bias[o];
        const float *w = kww_first_weight + o * KWW_BANDS * KWW_FIRST_KERNEL;  // [out][band][tap]
        for (int t = 0; t < KWW_FIRST_KERNEL; t++) {
            const float *f = s->input[(s->input_pos + t) % KWW_FIRST_KERNEL];  // oldest first
            for (int band = 0; band < KWW_BANDS; band++) {
                acc += w[band * KWW_FIRST_KERNEL + t] * f[band];
            }
        }
        x[o] = acc > 0.0f ? acc : 0.0f;
    }

    // blocks: depthwise mixed convolution, pointwise convolution with batch norm folded in, ReLU
    float d[KWW_FILTERS];
    for (int block = 0; block < KWW_BLOCKS; block++) {
        const int in = kww_block_inputs[block];
        push_history(s, block, x, in);
        depthwise(block, s, d);
        const float *w = kww_pw_weight[block];
        const float *b = kww_pw_bias[block];
        for (int o = 0; o < KWW_FILTERS; o++) {
            float acc = b[o];
            for (int c = 0; c < in; c++) {
                acc += w[o * in + c] * d[c];
            }
            x[o] = acc > 0.0f ? acc : 0.0f;
        }
    }

    float logit = kww_head_bias;
    for (int c = 0; c < KWW_FILTERS; c++) {
        logit += kww_head_weight[c] * x[c];
    }
    *probability = 1.0f / (1.0f + expf(-logit));
    return true;
}
