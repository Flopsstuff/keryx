# TFLite Micro audio frontend, as built by pymicro-features; see KERYX.md
set(lib tensorflow/lite/experimental/microfrontend/lib)
idf_component_register(SRCS ${lib}/kiss_fft_int16.cc ${lib}/fft.cc ${lib}/fft_util.cc ${lib}/filterbank.cc
                            ${lib}/filterbank_util.cc ${lib}/frontend.cc ${lib}/frontend_util.cc ${lib}/log_lut.cc
                            ${lib}/log_scale.cc ${lib}/log_scale_util.cc ${lib}/noise_reduction.cc
                            ${lib}/noise_reduction_util.cc ${lib}/pcan_gain_control.cc
                            ${lib}/pcan_gain_control_util.cc ${lib}/window.cc ${lib}/window_util.cc
                       INCLUDE_DIRS "."
                       PRIV_INCLUDE_DIRS "kissfft")
