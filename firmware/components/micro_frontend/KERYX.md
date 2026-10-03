# Vendored TFLite Micro audio frontend

The micro_speech feature frontend (window, FFT, mel filterbank, noise reduction, PCAN gain control, log scale)
that microWakeWord and TFLite Micro use, copied from [rhasspy/pymicro-features](https://github.com/rhasspy/pymicro-features)
at commit `96bd69cfad79aa67697e176570d3dd87052c3def` — the same code the wake word was trained with through the `pymicro-features` Python package,
so the features on the ESP32 match the training ones.

- `tensorflow/lite/experimental/microfrontend/lib/`: from TensorFlow, Apache-2.0 (see `LICENSE`). Only the
  library sources; the `_io`, `_test`, `_main` and memmap files were left out.
- `kissfft/`: KISS FFT by Mark Borgerding, BSD-3-Clause (the notice heads each file). It is not compiled on its
  own: `kiss_fft_int16.cc` includes `kiss_fft.cc` and `tools/kiss_fftr.cc` as a 16-bit fixed-point build.

No local changes. To update, copy the same files from a newer pymicro-features commit.
