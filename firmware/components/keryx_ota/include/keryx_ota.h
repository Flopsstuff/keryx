/*
 * Updates over Wi-Fi: the app image from the latest GitHub release of the repository, or from any http(s) URL.
 *
 * Console commands (also over Wi-Fi through the bridge):
 *   ota                 the running version, its slot and whether it is still on trial
 *   ota check           the latest release's version against the running one
 *   ota update [force]  download the latest release and restart into it (force: even if it is the same version)
 *   ota url <url>       download that image and restart into it (http is fine on the local network)
 * The download runs in its own task; the console answers at once and the log shows how it goes.
 *
 * A new image boots on trial: the bootloader (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE) goes back to the previous one if
 * it restarts before keryx_ota_start_trial has kept it for 30 s.
 */
#pragma once

#include <stdbool.h>

// Call early in app_main: a new image on trial is kept once it has run for 30 s.
void keryx_ota_start_trial(void);

// Handles the `ota` console commands; false if the line is not one of them.
bool keryx_ota_command(const char *cmd);

// 0..100 while an image downloads, -1 otherwise (for the LED ring).
int keryx_ota_progress(void);
