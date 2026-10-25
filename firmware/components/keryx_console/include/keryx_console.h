/*
 * Text console on the CDC serial port of the Keryx USB device (usb_device_uac with CONFIG_UAC_CDC).
 *
 * Once TinyUSB holds the USB port, the USB serial/JTAG console is gone; this brings the log back on the CDC port and
 * takes commands from the host, one per line:
 *   bootloader  restart into the ROM download mode, on the USB serial/JTAG port, ready for esptool
 *   reboot      restart the firmware
 *   top [seconds]  how busy each core was and which tasks took the time (default 5 s)
 *   log <tag|*> <none|error|warn|info|debug|verbose>   change a log level until the next restart
 *   log recent  the end of the journal
 *   log saved [clear]  the journal saved in NVS (keryx_console_journal_save), or forget it
 * Output is dropped while no host has the port open (DTR low), so writing never stalls the audio.
 *
 * Everything written but the periodic report also goes into a journal of the recent lines in PSRAM, each with the time
 * it began (the time of day once SNTP has set it, until then `+<seconds since boot>s`). Its end can be saved in NVS,
 * to be read after a restart or a power cut: a board that lost the bridge has nobody to tell why until it is back.
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

// Handles a command line the console does not know; returns false if the application does not know it either.
typedef bool (*keryx_console_command_fn)(const char *cmd);

// Call after uac_device_init(). The banner is printed whenever a host opens the port; `command` may be NULL.
esp_err_t keryx_console_start(const char *banner, keryx_console_command_fn command);

// What a submitted line printed, at most 3 KB; valid only during the call.
typedef void (*keryx_console_done_fn)(int id, const char *output);

// Runs a line from elsewhere (the console over Wi-Fi) in the console task, as if typed, and hands what it printed
// to `done`, called from the console task. Which lines may run is the caller's business. False if the line is too
// long (127 characters at most) or two are already waiting.
bool keryx_console_submit(int id, const char *line, keryx_console_done_fn done);

// Writes a line (or part of one) to the host, as printf.
void keryx_console_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
// The same, but not into the journal: for the periodic report, which would push everything else out of it.
void keryx_console_report(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
// Saves the journal's end (2.7 KB) in NVS, over a saved one of this run; ESP_ERR_INVALID_STATE if one of an earlier
// run is there and not yet cleared, as that one saw the trouble begin. Writes flash: the stack must be internal.
esp_err_t keryx_console_journal_save(void);
// A saved journal is waiting to be read (`log saved`) and cleared (`log saved clear`).
bool keryx_console_journal_saved(void);
// A USB host has the board: it is on the bench, not in the speaker.
bool keryx_console_host(void);
