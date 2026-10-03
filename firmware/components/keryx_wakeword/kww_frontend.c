#include "kww_frontend.h"

#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"

#define FEATURE_SCALE 0.0390625f  // pymicro-features' float scale of the frontend's uint16 output

bool kww_frontend_init(kww_frontend_t *f)
{
    struct FrontendConfig config;
    config.window.size_ms = 30;
    config.window.step_size_ms = 10;
    config.filterbank.num_channels = 40;
    config.filterbank.lower_band_limit = 125.0f;
    config.filterbank.upper_band_limit = 7500.0f;
    config.noise_reduction.smoothing_bits = 10;
    config.noise_reduction.even_smoothing = 0.025f;
    config.noise_reduction.odd_smoothing = 0.06f;
    config.noise_reduction.min_signal_remaining = 0.05f;
    config.pcan_gain_control.enable_pcan = 1;
    config.pcan_gain_control.strength = 0.95f;
    config.pcan_gain_control.offset = 80.0f;
    config.pcan_gain_control.gain_bits = 21;
    config.log_scale.enable_log = 1;
    config.log_scale.scale_shift = 6;
    return FrontendPopulateState(&config, &f->state, KWW_SAMPLE_RATE) == 1;
}

int kww_frontend_process(kww_frontend_t *f, const int16_t *samples, size_t count, float (*frames)[40], int max_frames)
{
    int written = 0;
    while (count > 0 && written < max_frames) {
        size_t read = 0;
        struct FrontendOutput out = FrontendProcessSamples(&f->state, samples, count, &read);
        samples += read;
        count -= read;
        if (out.size == 40) {
            for (int i = 0; i < 40; i++) {
                frames[written][i] = out.values[i] * FEATURE_SCALE;
            }
            written++;
        }
        if (read == 0) {
            break;
        }
    }
    return written;
}
