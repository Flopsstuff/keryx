#include "kww_decimate.h"

#include <string.h>

// The taps share the data cache with the model's weights, which would keep evicting them; keep them in RAM too.
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define KWW_WEIGHTS DRAM_ATTR
#else
#define KWW_WEIGHTS
#endif
#include "kww_decimate_taps.h"

_Static_assert(KWW_DECIMATE_TAPS <= 64, "history too small for the filter");

void kww_decimate_reset(kww_decimate_t *d)
{
    memset(d, 0, sizeof(*d));
}

size_t kww_decimate(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out)
{
    size_t written = 0;
    for (size_t i = 0; i < count; i++) {
        d->history[d->pos] = in[i];
        d->pos = (d->pos + 1) % KWW_DECIMATE_TAPS;
        if (++d->phase < 3) {
            continue;
        }
        d->phase = 0;
        // newest sample at tap 0
        float acc = 0.0f;
        int slot = d->pos;
        for (int t = 0; t < KWW_DECIMATE_TAPS; t++) {
            slot = slot == 0 ? KWW_DECIMATE_TAPS - 1 : slot - 1;
            acc += kww_decimate_taps[t] * d->history[slot];
        }
        out[written++] = acc > 32767.0f ? 32767 : acc < -32768.0f ? -32768 : (int16_t)acc;
    }
    return written;
}
