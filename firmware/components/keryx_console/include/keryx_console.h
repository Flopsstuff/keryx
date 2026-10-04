/*
 * Text console on the CDC serial port of the Keryx USB device (usb_device_uac with CONFIG_UAC_CDC).
 *
 * Once TinyUSB holds the USB port, the USB serial/JTAG console is gone; this brings the log back on the CDC port and
 * takes commands from the host, one per line:
 *   bootloader  restart into the ROM download mode, on the USB serial/JTAG port, ready for esptool
 *   reboot      restart the firmware
 *   log <tag|*> <none|error|warn|info|debug|verbose>   change a log level until the next restart
 * Output is dropped while no host has the port open (DTR low), so writing never stalls the audio.
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"

// Handles a command line the console does not know; returns false if the application does not know it either.
typedef bool (*keryx_console_command_fn)(const char *cmd);

// Call after uac_device_init(). The banner is printed whenever a host opens the port; `command` may be NULL.
esp_err_t keryx_console_start(const char *banner, keryx_console_command_fn command);

// Writes a line (or part of one) to the host, as printf.
void keryx_console_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
