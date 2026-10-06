/*
 * The link to the voice bridge: a WebSocket client that stays connected and speaks protocol v1 (firmware/README.md):
 *
 *   board -> bridge   {"type":"hello",…,"volume":…,"muted":…}, {"type":"wake","score":…,"preroll_ms":…}, {"type":"played"},
 *                     {"type":"volume","value":0..100}, {"type":"mute","value":bool} (after every change,
 *                     whoever made it, and when asked), {"type":"stop"} (end the conversation: the stop button),
 *                     {"type":"console","id":…,"output":…} (what a console line printed),
 *                     binary: PCM16 LE 16 kHz mono (ASR beam) in 20 ms frames, from a wake until listen_stop
 *   bridge -> board   {"type":"ready"}, {"type":"listen_stop"}, {"type":"sound","name":…},
 *                     {"type":"play_start","rate":24000|16000}, binary PCM16 LE mono, {"type":"play_end"},
 *                     {"type":"play_stop"}, {"type":"volume","value":0..100} or {"type":"volume","delta":±n},
 *                     {"type":"mute","value":bool}; either without value asks for the current one;
 *                     {"type":"console","id":…,"line":…} (a console command, run if the application allows it)
 *
 * The bridge's URL and token come from keryx_net. Wi-Fi power save is off from a wake or a play_start (the bridge may
 * start an answer on its own) until 5 s after the stream and the playback have ended.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    void (*sound)(const char *name);  // {"type":"sound"}: "wake", "thinking" or "stop"
    void (*volume)(bool relative, int amount);  // {"type":"volume"}: set to amount, or change by it
    int (*get_volume)(void);  // the current volume, for hello
    void (*mute)(bool on);    // {"type":"mute","value":…}
    bool (*get_muted)(void);  // for hello
    void (*console)(int id, const char *line);  // {"type":"console"}: answer with keryx_link_console_reply
} keryx_link_callbacks_t;

// Connects to the bridge, if keryx_net has one; call after keryx_net_start().
esp_err_t keryx_link_start(const keryx_link_callbacks_t *callbacks);

// Reconnects with the current settings, after `set bridge`, `set token` or `erase`.
void keryx_link_restart(void);

// Capture task, every block: the 16 kHz ASR audio, kept for the preroll and streamed during a conversation.
void keryx_link_audio_up(const int16_t *samples, size_t count);

// Capture task: the wake word fired. Never blocks.
void keryx_link_wake(float score);

// The stop button: asks the bridge to end the conversation (it stops the answer and the stream). Never blocks.
void keryx_link_stop(void);

// What a console line from the bridge printed: sent back as {"type":"console","id":…,"output":…}. Never blocks.
void keryx_link_console_reply(int id, const char *output);

// Connected to the bridge, past hello.
bool keryx_link_ready(void);

// The microphone streams to the bridge: from a wake until listen_stop, i.e. the whole conversation.
bool keryx_link_streaming(void);

// An answer from the bridge is playing (from play_start until it has been played or stopped).
bool keryx_link_playing(void);

// Playback task, every block: up to `frames` of the bridge's audio at 48 kHz mono into out; returns how many were
// written (the rest of out is untouched), 0 when the bridge is not playing.
size_t keryx_link_play(int16_t *out, size_t frames);

// The volume changed (from the bridge, the console or a knob): tells the bridge. Never blocks.
void keryx_link_volume_changed(int volume);

// The microphone was muted or unmuted: stops a running stream at once when muted, and tells the bridge.
void keryx_link_mute_changed(bool muted);

// One line for the console: connection state, stream and playback counters since the last call.
void keryx_link_report(char *buf, size_t size);
