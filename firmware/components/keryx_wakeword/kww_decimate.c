#include "kww_decimate.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

// The taps share the data cache with the model's weights, which would keep evicting them; keep them in RAM too.
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define KWW_WEIGHTS DRAM_ATTR
#else
#define KWW_WEIGHTS
#endif
#include "kww_decimate_taps.h"

_Static_assert(KWW_DECIMATE_TAPS <= KWW_DECIMATE_LEN, "delay line too short for the filter");

static int16_t clip16(float v)
{
    return v > 32767.0f ? 32767 : v < -32768.0f ? -32768 : (int16_t)v;
}

void kww_decimate_reset(kww_decimate_t *d)
{
    memset(d, 0, sizeof(*d));
#ifdef ESP_PLATFORM
    // esp-dsp applies coeffs[0] to the oldest sample of the delay line. The filter is symmetric, so the taps keep
    // their order; the 3 padding zeros go first, onto the 3 oldest samples, which the 61 taps would not reach.
    static float coeffs[KWW_DECIMATE_LEN] __attribute__((aligned(16)));
    for (int i = 0; i < KWW_DECIMATE_LEN; i++) {
        int t = i - (KWW_DECIMATE_LEN - KWW_DECIMATE_TAPS);
        coeffs[i] = t < 0 ? 0.0f : kww_decimate_taps[t];
    }
    d->use_dsp = dsps_fird_init_f32(&d->fir, coeffs, d->history, KWW_DECIMATE_LEN, 3) == ESP_OK;
    memset(d->history, 0, sizeof(d->history));
    d->pos = 0;
#endif
}

size_t kww_decimate_reference(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out)
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
        out[written++] = clip16(acc);
    }
    return written;
}

size_t kww_decimate(kww_decimate_t *d, const int16_t *in, size_t count, int16_t *out)
{
#ifdef ESP_PLATFORM
    if (!d->use_dsp) {
        return kww_decimate_reference(d, in, count, out);
    }
    size_t written = 0;
    while (count >= 3) {
        size_t n = count < KWW_DECIMATE_CHUNK ? count - count % 3 : KWW_DECIMATE_CHUNK;
        for (size_t i = 0; i < n; i++) {
            d->in[i] = in[i];
        }
        int got = dsps_fird_f32(&d->fir, d->in, d->out, (int)(n / 3));
        for (int i = 0; i < got; i++) {
            out[written++] = clip16(d->out[i]);
        }
        in += n;
        count -= n;
    }
    return written;
#else
    return kww_decimate_reference(d, in, count, out);
#endif
}

int kww_decimate_self_check(void)
{
    enum { N = 4800 };  // 100 ms at 48 kHz: a chirp through the pass and stop bands, plus noise
    int16_t *in = malloc(N * sizeof(int16_t));
    int16_t *a = malloc(N / 3 * sizeof(int16_t)), *b = malloc(N / 3 * sizeof(int16_t));
    size_t size = (sizeof(kww_decimate_t) + 15) & ~(size_t)15;
    kww_decimate_t *fast = aligned_alloc(16, size), *slow = aligned_alloc(16, size);
    int worst = -1;
    if (in && a && b && fast && slow) {
        unsigned seed = 1;
        for (int i = 0; i < N; i++) {
            float t = (float)i / 48000.0f;
            seed = seed * 1103515245u + 12345u;
            float noise = ((seed >> 16) & 0x7fff) / 32768.0f - 0.5f;
            in[i] = clip16(12000.0f * sinf(2.0f * (float)M_PI * (200.0f + 100000.0f * t) * t) + 4000.0f * noise);
        }
        kww_decimate_reset(fast);
        kww_decimate_reset(slow);
#ifdef ESP_PLATFORM
        if (!fast->use_dsp) {
            worst = -2;  // esp-dsp refused the buffers: nothing fast to compare
            goto done;
        }
#endif
        size_t na = kww_decimate(fast, in, N, a);
        size_t nb = kww_decimate_reference(slow, in, N, b);
        worst = na == nb ? 0 : 32767;
        for (size_t i = 0; i < na && i < nb; i++) {
            int diff = abs(a[i] - b[i]);
            worst = diff > worst ? diff : worst;
        }
    }
#ifdef ESP_PLATFORM
done:
#endif
    free(in);
    free(a);
    free(b);
    free(fast);
    free(slow);
    return worst;
}
