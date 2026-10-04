# "Hey Keryx" wake word: 48 -> 16 kHz decimation, micro_speech features, streaming model (wakeword/export_model.py)
# kww_decimate.h exposes esp-dsp's FIR state, so esp-dsp is a public requirement
idf_component_register(SRCS "kww_decimate.c" "kww_frontend.c" "kww_model.c"
                       INCLUDE_DIRS "include"
                       REQUIRES micro_frontend espressif__esp-dsp)

# where the time goes: fast even when the project builds for size
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)
