#!/usr/bin/env bash
# Flash an ESP-IDF project in this directory onto the XIAO ESP32S3.
#
# Firmware that hands the USB port to TinyUSB (usb-soundcard) keeps the serial console only for the first few
# seconds after boot, so this waits for the port to show up and flashes straight away. Run it, then press RESET on
# the XIAO or replug its USB cable.
set -euo pipefail

project=${1:?usage: flash.sh <project>, e.g. flash.sh usb-soundcard}
cd "$(dirname "$0")/$project"
. "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" > /dev/null
idf.py build > /dev/null
cd build

echo "waiting for the XIAO's serial port: press RESET on the XIAO or replug it"
for _ in $(seq 1 1200); do
    port=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)
    if [ -n "$port" ]; then
        # the port shows up a moment before it can be opened
        for attempt in $(seq 1 25); do
            if python -m esptool --chip esp32s3 -p "$port" -b 460800 --before default_reset --after hard_reset \
                write_flash @flash_args > flash.log 2>&1; then
                echo "flashed $project via $port (attempt $attempt)"
                exit 0
            fi
            sleep 0.15
        done
        tail -3 flash.log
        exit 1
    fi
    sleep 0.05
done
echo "no serial port within 60 s" >&2
exit 1
