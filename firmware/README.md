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

## `keryx`

The wake word and the USB sound card in one firmware; `wakeword` and `usb-soundcard` stay as their simpler
predecessors.

- One task reads I2S in 5 ms blocks and feeds both the wake word (ASR channel, as in `wakeword`) and the USB
  microphone (48 kHz, 16-bit stereo: processed beam, ASR beam) through a stream buffer. The XVF3800 clocks I2S and
  the host clocks USB, so audio older than 20 ms is dropped when the host reads.
- Playback: the host's packets go into a second buffer; a playback task writes I2S without a break, starting once
  30 ms are queued and dropping the excess beyond 80 ms. The sound card is asynchronous: its feedback endpoint asks
  the host for slightly more or fewer samples to keep ~40 ms queued, so the host follows the XVF3800's clock (the
  UAC component's own FIFO-count feedback lost ~0.4 % of the audio, see `components/usb_device_uac/KERYX.md`). A
  30 s sine plays without a single gap; the report shows the host's real rate.
- AEC: with clean playback the echo of speech from a speaker on the headphone jack reaches the ASR beam at the noise
  floor. The XVF3800's `AUDIO_MGR_SYS_DELAY` of −30 samples in this I2S build is right: 0, 12, 30 or 60 let the
  echo through.
- A detection prints `wake score=0.973` on the serial port (`components/keryx_console`, as in `usb-soundcard`) and
  plays a chime on the headphone jack, mixed into the host's playback. `sound thinking` loops a quiet "thinking"
  sound until `sound stop`, until the host's audio starts (above −54 dBFS) or for 60 s at most; `sound wake`
  plays the chime. The sounds are synthesised by `sounds/make_sounds.py` (`--export` writes the chosen ones to
  `keryx/main/keryx_sounds.h`; without it, candidates to listen to go to `sounds/candidates/`). The log goes to the same port, with a report every
  5 s: peak score, ASR level, processing time per 5 ms block (about 0.5 ms), USB microphone and speaker buffers.
- Serial port commands besides `bootloader` and `reboot`: `loop on` puts the XVF3800's echo reference — what was
  played — on the left capture channel instead of the processed beam, to measure the playback path; `loop mic`
  puts microphone 0 there, before AEC; `loop off` restores the beam. `xvf get|set <resid> <cmd> <int32|float|uint8>
  [count | values…]` reads or writes any XVF3800 parameter (e.g. `xvf get 33 3 int32`, AEC converged). While
  `loop on` is set, the ASR beam lets echo through: it is for measuring only.
- Pairing, through the same serial port (`components/keryx_net`): `set ssid|password|bridge|token <value>`,
  `config`, `erase`, `wifi scan`, `status`, `net check <host> <port>` (can the board open a TCP connection there:
  is the bridge reachable from this network?); answers end with an `ok` or `error` line. The settings live in NVS and
  survive flashing; the board joins Wi-Fi at start-up and after `set password`, retries with back-off (1 to 30 s)
  and reports `wifi connected ip=… rssi=…` / `wifi disconnected reason=…`. `status` also shows the board's id
  (`keryx-` and the end of its MAC, also its USB serial number, so each board has its own port name), the firmware
  version and free memory. The XIAO needs its U.FL antenna: without it networks show at about −92 dBm.
- Memory: the wake word's weights and state take ~160 KB of internal RAM, and Wi-Fi needs its RX buffers there, so
  the 8 MB PSRAM is on for everything large, Wi-Fi, FreeRTOS and heap code stays in flash instead of IRAM, and Wi-Fi
  keeps 8 static RX buffers. About 33 KB of internal RAM stays free with Wi-Fi connected. The app builds for size
  except the wake word and its frontend (`-O2`); `partitions.csv` has two 3 MB app slots, for updates over Wi-Fi
  later, and 1.9 MB for a filesystem.
- The voice bridge (`components/keryx_link`): a WebSocket client to the `bridge` URL, connected all the time
  (ping every 10 s, reconnects every 2 s). Protocol v1 below. Wi-Fi power save (modem sleep, DTIM 3: pings take
  ~260 ms) goes off at a wake and back on 5 s after the conversation. The report adds a line for the link: state,
  frames up, bytes down, playback underruns.
- Volume: the board's own, 0–100 over everything it plays (the bridge's answers, its sounds, USB audio): 0 is
  silence, then 0.5 dB a step up to 0 dB at 100, kept in NVS (saved 2 s after the last change). `volume`,
  `volume <0-100>`, `volume up|down` (±10) on the console; `{"type":"volume",…}` from the bridge, see below. The
  macOS volume of the Keryx sound card still applies to USB audio on top.
- `top [seconds]` on the console: how busy each core was and which tasks took the time. Idle, connected to Wi-Fi
  at 240 MHz: core 0 ~2 % (Wi-Fi, lwIP), core 1 ~12 % (the wake word 10 %, playback 2 %).
- More console commands: `wake` acts as if the wake word fired (to test the bridge without saying it), and
  `log <tag|*> <none|error|warn|info|debug>` changes a log level until the next restart (the WebSocket library's
  own log is off: it reports every failed attempt while the bridge is down).
- Tasks: core 1 has capture (wake word) and playback; core 0 USB, Wi-Fi, lwIP, the WebSocket client and the console.
  USB audio runs at priority 20, above lwIP (18): at 5 it starved during Wi-Fi traffic and dropped audio.

### Bridge protocol v1

The board connects to `ws://…` (the `bridge` setting) and keeps the connection. Text frames are JSON; binary frames
are PCM16 little-endian mono.

| Direction | Message | Meaning |
|---|---|---|
| board → bridge | `{"type":"hello","id":…,"token":…,"firmware":…,"volume":…}` | first message; a wrong token: close with 4001 |
| bridge → board | `{"type":"ready"}` | accepted |
| board → bridge | `{"type":"wake","score":0.97,"preroll_ms":500}` | the wake word fired; audio follows |
| board → bridge | binary, 16 kHz, 20 ms (640 bytes) | the ASR beam: `preroll_ms` from before the wake, then live, until `listen_stop`. A wake during a stream sends `preroll_ms: 0` and the stream goes on unbroken |
| bridge → board | `{"type":"listen_stop"}` | stop streaming (the board stops by itself after 2 min) |
| bridge → board | `{"type":"sound","name":"wake"\|"thinking"\|"stop"}` | the board's own sounds |
| bridge → board | `{"type":"play_start","rate":24000}`, binary, `{"type":"play_end"}` | an answer, 24 or 16 kHz, any frame size, faster than real time is fine: the board buffers 10 s (holding back TCP beyond that), starts once 150 ms are queued and upsamples to 48 kHz |
| bridge → board | `{"type":"play_stop"}` | cut the answer now (10 ms fade) |
| board → bridge | `{"type":"played"}` | the answer has really finished playing, or was cut |
| bridge → board | `{"type":"volume","value":60}`, `{"type":"volume","delta":-10}`, `{"type":"volume"}` | set the volume, change it, or ask for it |
| board → bridge | `{"type":"volume","value":60}` | the volume, after every change whoever made it (bridge, console, later a knob) |

Measured against a test server, through the echo reference (`loop on`): 24 and 16 kHz answers clean (residual
−40 dB), `played` 3.06 s after the first byte of a 3 s answer, 90–170 ms after `play_stop`; uplink frames 18 ms
apart on average, at most ~40 ms.
- The host should send 48 kHz: macOS converting 16 kHz on the fly (PortAudio with its default small blocks) breaks
  up the sound on this device; 24 and 44.1 kHz were fine.
