# Firmware

ESP-IDF projects for the XIAO ESP32S3 on the reSpeaker Flex. The XVF3800 has to run the I2S firmware; see
[I2S mode](../docs/respeaker-flex-xvf3800.md#i2s-mode) in the board notes.

## Toolchain

ESP-IDF v5.5.5, installed once:

```bash
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.5 --depth 1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32s3
brew install ninja ccache
```

Every new shell needs `. ~/esp/esp-idf/export.sh` before `idf.py`.

## Building and flashing

Connect the XIAO's own USB-C port (the Flex port next to RST belongs to the XVF3800), then:

```bash
cd firmware/<project>
idf.py set-target esp32s3        # first build only
idf.py -p /dev/cu.usbmodem* flash monitor
```

or, for any project and even while `usb-soundcard` is running, `./flash.sh <project>` and then reset the XIAO.

Logs go over the XIAO's USB serial/JTAG port: GPIO43/44, the UART0 pins, carry I2S data on this board.

## `xvf-bringup`

Checks that the ESP32, the XVF3800 and the codec work together:

1. scans I2C (expects 0x18 codec, 0x2C XVF3800) and reads the XVF3800 version and output routing;
2. measures BCLK, LRCLK and MCLK with the pins as inputs, and runs I2S as slave when the XVF3800 already drives
   them (it does with the I2S firmware), as master otherwise;
3. logs the level of both capture channels and the XVF3800 DoA twice a second, and plays a quiet 440 Hz beep
   (400 ms every 2 s, −24 dBFS) to the headphone jack. It holds the playback line low from the start and sends
   1.3 s of silence after I2S starts before the first beep; without that the first beep comes with noise.

## `usb-soundcard`

Prototype: the XIAO becomes a USB Audio Class device (`espressif/usb_device_uac` on TinyUSB), bridging the
XVF3800's I2S bus to the host.

- Microphone: 48 kHz, 16-bit stereo; left is the processed beam, right the ASR beam (the XVF3800's L/R outputs).
- Speaker: 48 kHz, 16-bit stereo to the headphone jack, with the host's volume and mute applied in software. The
  XVF3800 reads the same line as its echo-cancellation reference.
- The start-up log is printed over the USB serial console; 4 s after boot TinyUSB takes the USB PHY, the console
  disappears and the sound card appears (macOS lists it as "usb uac").
- To flash again after that, run `./flash.sh usb-soundcard` and press RESET on the XIAO (or replug it): the script
  catches the serial console in its first seconds and flashes then. Holding BOOT on the XIAO while it powers up did
  not bring up the ROM download mode on our board.
