/*
 * Pairing and Wi-Fi: the settings the voice bridge hands over through the USB serial console, kept in NVS, and the
 * Wi-Fi station that uses them.
 *
 * Console commands (keryx_console passes them on); the value is the rest of the line, spaces included:
 *   set ssid <name>        set password <password>     Wi-Fi; reconnects with the new values
 *   set bridge <ws://…>    set token <token>           the voice bridge, for streaming
 *   config                 the settings; the password and token show only as (set) / (not set)
 *   erase                  forget all settings and leave Wi-Fi
 *   wifi scan              networks in range: rssi, security, ssid
 *   status                 board id, firmware version, Wi-Fi state, bridge
 *   net check <host> <port>  open a TCP connection there and close it: is the bridge reachable from this network?
 * Every answer ends with a line starting with `ok` or `error`. Wi-Fi changes come as their own lines:
 * `wifi connected ip=… rssi=…`, `wifi disconnected reason=… retry_in=…s`.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

// Initialises NVS and Wi-Fi and connects if the settings are complete. Call after keryx_console_start().
esp_err_t keryx_net_start(void);

// Handles a console command; false if it is not one of the above.
bool keryx_net_command(const char *cmd);

// The board's id, from its Wi-Fi MAC: "keryx-a1b2c3". Valid before keryx_net_start().
const char *keryx_net_id(void);

// The voice bridge's settings, copied into the buffers; false if unset.
bool keryx_net_bridge(char *url, size_t url_size, char *token, size_t token_size);

bool keryx_net_connected(void);
