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

or, for any project, `./flash.sh <project>`. It finds the board by USB ID: if `usb-soundcard` runs, it sends the
`bootloader` command to its serial port; with firmware that keeps the serial/JTAG console it flashes straight away;
otherwise it asks for RESET on the XIAO.

Logs go over the XIAO's USB serial/JTAG port: GPIO43/44, the UART0 pins, carry I2S data on this board.

## `xvf-bringup`

Checks that the ESP32, the XVF3800 and the codec work together:

1. scans I2C (expects 0x18 codec, 0x2C XVF3800) and reads the XVF3800 version and output routing;
2. measures BCLK, LRCLK and MCLK with the pins as inputs, and runs I2S as slave when the XVF3800 already drives
   them (it does with the I2S firmware), as master otherwise;
3. logs the level of both capture channels and the XVF3800 DoA twice a second, and plays a quiet 440 Hz beep
   (400 ms every 2 s, −24 dBFS) to the headphone jack. It holds the playback line low from the start and sends
   1.3 s of silence after I2S starts before the first beep; without that the first beep comes with noise.

## `mic-loopback`

Plays the XVF3800 capture straight back to the headphone jack from the ESP32, no host involved: processed beam in
the left ear, ASR beam in the right. 2 ms DMA blocks keep the ESP32's share of the delay at about 10 ms. Logs both
channel levels once a second.

## `usb-soundcard`

Prototype: the XIAO becomes a USB Audio Class device (`espressif/usb_device_uac` on TinyUSB), bridging the
XVF3800's I2S bus to the host.

- Microphone: 48 kHz, 16-bit stereo; left is the processed beam, right the ASR beam (the XVF3800's L/R outputs).
- At start-up it sets the XVF3800's ASR output gain (`AEC_ASROUTGAIN`, reset to 1.0 by the chip) to 4.0, +12 dB,
  over I2C with `components/xvf_control`. The ASR path has no AGC or limiter, so this fixed gain is its whole level
  control: speech at a distance now peaks around −15 dBFS instead of −27.
- Speaker: 48 kHz, 16-bit stereo to the headphone jack, with the host's volume and mute applied in software. The
  XVF3800 reads the same line as its echo-cancellation reference.
- The start-up log is printed over the USB serial console; 4 s after boot TinyUSB takes the USB PHY, the console
  disappears and a composite device appears: the sound card (macOS lists it as "Keryx") and a CDC serial port
  (`/dev/cu.usbmodemkeryx_proto*` on macOS, `/dev/ttyACM*` on Linux, USB ID 303A:8000).
- That port is `components/keryx_console`: from then on the log goes there (while a host has the port open), and it
  takes commands, one per line: `bootloader` restarts into the ROM download mode on the serial/JTAG port, `reboot`
  restarts the firmware. `./flash.sh` uses `bootloader`, so no buttons are needed. Without it: press RESET while
  `./flash.sh` waits; holding BOOT on the XIAO while it powers up did not bring up the ROM download mode on our board.

## `wakeword`

The "Hey Keryx" wake word on the XIAO itself, from `components/keryx_wakeword`: the XVF3800's ASR channel goes
from 48 to 16 kHz with the filter the training audio went through (`scipy.signal.resample_poly`, run in esp-dsp's
decimating FIR, checked against plain C at start-up), into the
micro_speech frontend (`components/micro_frontend`, the TFLite Micro code pymicro-features wraps) and the model
from `wakeword/`, run streaming in float C: each 30 ms only the newest outputs of every layer are computed.

- A detection logs `>>> Hey Keryx! (score …)` and beeps on the headphone jack; the XVF3800 takes that line as its
  echo reference, so the beep does not reach the ASR channel.
- Once a second the log shows the highest score, the ASR peak level and the processing time per 10 ms of audio:
  about 0.86 ms (decimation 0.15, features 0.43, model 0.28), the model's weights kept in internal RAM.
- At start-up it sets `AEC_ASROUTGAIN` to 4.0, as `usb-soundcard` does: the model learned the channel at that gain.
- The model and threshold are `components/keryx_wakeword/kww_weights.h` and `include/kww_config.h`, written by
  `wakeword/export_model.py`.

The console stays on the USB serial port, so `idf.py monitor` (or `./flash.sh wakeword` and then any serial
terminal) shows the log. To record through the board again, flash `usb-soundcard`.
