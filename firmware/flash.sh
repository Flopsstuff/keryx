#!/usr/bin/env bash
# Flash an ESP-IDF project in this directory onto the XIAO ESP32S3.
#
# Ports are told apart by USB ID: the ROM bootloader and firmware that keeps the USB serial/JTAG console show up as
# 303A:1001; the Keryx USB device (usb-soundcard) as 303A:8000, whose serial port (keryx_console) takes a
# `bootloader` command. With neither, e.g. older firmware that gives the USB port to TinyUSB with no serial port,
# this waits for the console port: press RESET on the XIAO or replug its USB cable.
set -euo pipefail

project=${1:?usage: flash.sh <project>, e.g. flash.sh usb-soundcard}
cd "$(dirname "$0")/$project"
. "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" > /dev/null 2>&1
idf.py build > /dev/null
cd build

find_port() {
    python -m serial.tools.list_ports -q "$1" 2>/dev/null | head -1 | awk '{print $1}'
}

console=$(find_port 303A:8000)
if [ -n "$console" ]; then
    echo "asking $console to restart into the bootloader"
    python -c 'import serial, sys; serial.Serial(sys.argv[1]).write(b"bootloader\n")' "$console"
elif [ -z "$(find_port 303A:1001)" ]; then
    echo "waiting for the XIAO's serial port: press RESET on the XIAO or replug it"
fi

for _ in $(seq 1 1200); do
    port=$(find_port 303A:1001)
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
