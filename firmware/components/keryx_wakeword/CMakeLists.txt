# "Hey Keryx" wake word: 48 -> 16 kHz decimation, micro_speech features, streaming model (wakeword/export_model.py)
idf_component_register(SRCS "kww_decimate.c" "kww_frontend.c" "kww_model.c"
                       INCLUDE_DIRS "include"
                       REQUIRES micro_frontend)
