# reSpeaker Flex XVF3800 Circular-4

The microphone array Keryx listens through: a Seeed Studio carrier board built around the XMOS XVF3800 voice
processor, with a 4-mic circular daughter board attached over FPC.

## Official documentation

Seeed Studio:

- [reSpeaker Flex wiki](https://wiki.seeedstudio.com/respeaker_flex_introduction/) — hardware, firmware
  variants, flashing, safe mode.
- [reSpeaker Flex repository](https://github.com/respeaker/reSpeaker_Flex) — firmware images
  (`xmos_firmwares/usb`, `xmos_firmwares/i2s`) and the Python control tool (`python_control/xvf_host.py`).
- [Product page, Circular-4](https://www.seeedstudio.com/reSpeaker-Flex-XVF3800-Circular-4-p-6737.html).
- [reSpeaker XVF3800 USB Mic Array wiki](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/) — the
  non-Flex sibling on the same chip; useful for overlapping topics.
- [reSpeaker XVF3800 USB 4-mic array repository](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY)
  — native `xvf_host` binaries (including `mac_arm64`) and its
  [host control README](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md).

XMOS (the chip itself, firmware v3.2.1):

- [XVF3800 product page](https://www.xmos.com/xvf3800).
- [User Guide (HTML)](https://www.xmos.com/documentation/XM-014888-PC/html/doc/user_guide/index.html) /
  [PDF](https://www.xmos.com/documentation/XM-014888-PC/pdf/xvf3800_user_guide_v3.2.1.pdf) — control commands,
  output selection (section 3.6.1), tuning.
- [Using the host application](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/03_using_the_host_application.html).
- [Tuning the application](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/04_tuning_the_application.html).
- [Programming Guide (PDF)](https://www.xmos.com/documentation/XM-014888-PC/pdf/xvf3800_programming_guide_v3.2.1.pdf).

## Hardware

- 4 PDM MEMS microphones in a circle, 44 mm between neighbours, 360° pickup.
- XMOS XVF3800 does all DSP on-board: AEC, beamforming, DoA, AGC, VAD, noise suppression, de-reverberation.
- TLV320AIC3104 codec; 3.5 mm AUX out and a JST speaker connector driven by an amplifier (up to 10 W into 4 Ω,
  needs the 12 V terminal).
- USB-C (UAC 2.0 and DFU), 2 GPIO lines on the FPC, RST and Boot (safe mode) buttons.
- Footprint for an optional XIAO ESP32S3 connected over I2S + I2C; that setup needs the I2S firmware instead of
  the USB one.
- The green PWR LED sits on the power rail; no firmware can switch it off.

![Core board, back and front](images/flex-board-overview.jpg)

Images in this document come from the [Seeed wiki](https://wiki.seeedstudio.com/respeaker_flex_introduction/).

## Pins

![Pin headers](images/flex-headers-pinout.jpg)

### XVF3800 header (2×10)

Pin 1 is the square pad; 1–10 run down the outer column, 20–11 down the inner one.

![XVF3800 header schematic](images/flex-xvf-header-schematic.png)

| Pin | Signal | Pin | Signal |
|---|---|---|---|
| 1 | X1D11 — I2S MCLK | 20 | X1D10 — I2S BCLK |
| 2 | X1D01 — I2S LRCK | 19 | X1D22 — I2S DATA1 (processed audio to the host) |
| 3 | X1D00 — I2S DATA0 (reference in from the host; out in USB builds) | 18 | X1D34 — I2S DATA2 |
| 4 | GND | 17 | GND |
| 5 | X0D38 — I2C SDA, 4.7 kΩ pull-up to VDDIO | 16 | X0D00 — SPI CS_N |
| 6 | X0D37 — I2C SCL, 4.7 kΩ pull-up to VDDIO | 15 | X0D10 — SPI CLK |
| 7 | X1D13 — button input (active low) | 14 | X0D11 — SPI MOSI |
| 8 | GND | 13 | X0D39 — SPI MISO |
| 9 | 5V_IN | 12 | GND |
| 10 | 5V_IN | 11 | VDDIO (3.3 V) |

The front silkscreen photo says X0D13 on pin 7; the schematic and the back silkscreen say X1D13.

### XIAO ESP32S3

![XIAO schematic](images/flex-xiao-schematic.png)

The GPIO numbers printed inside the XIAO symbol above belong to a different XIAO; the ESP32-S3 numbers below come
from the XIAO ESP32S3 pinout.

| XIAO | ESP32-S3 | Used for |
|---|---|---|
| D0 | GPIO1 (ADC1, touch) | free, on the 4-pin header |
| D1 | GPIO2 | not on the header; most likely the XVF3800 reset line |
| D2 | GPIO3 (ADC1, touch, strapping pin) | free, on the 4-pin header |
| D3 | GPIO4 (ADC1, touch) | free, on the 4-pin header |
| D4 | GPIO5 | I2C SDA |
| D5 | GPIO6 | I2C SCL |
| D6 | GPIO43 | I2S DATA1, audio from the XVF3800 |
| D7 | GPIO44 | I2S DATA0, audio to the XVF3800 |
| D8 | GPIO7 | I2S LRCK |
| D9 | GPIO8 | I2S BCLK |
| D10 | GPIO9 | I2S MCLK |

The 4-pin header next to the XIAO carries GND, D3, D2 and D0; it has no 3.3 V pin.

### What is free for our own peripherals

- **ESP32 D0, D2, D3** — any function through the GPIO matrix: buttons, an encoder (PCNT), WS2812 data (RMT).
  All three are ADC1 channels (GPIO1–10 are), so a resistor ladder can put several buttons on one pin; ADC1 keeps
  working while Wi-Fi is on. D2 (GPIO3) is a strapping pin: fine as a GPIO, but nothing should hold it at a fixed
  level during boot.
- **XVF3800 X0D11 and X0D39** — outputs driven with `GPO_WRITE_VALUE`, e.g. plain LEDs through a ~1 kΩ
  resistor. SPI control is not used by the USB or I2C builds. X0D00 and X0D10 cannot be driven from the Seeed
  firmware.
- **XVF3800 X1D13** — a button input; what the Seeed firmware does with it is unknown, and the Seeed
  `xvf_host.py` has no command to read it.
- **The shared I2C bus** (header pins 5 and 6, the same lines as XIAO D4/D5) — room for I2C expanders, encoders
  or LED drivers. Taken addresses: 0x2C (XVF3800); the TLV320AIC3104 codec is usually at 0x18 — confirm with a
  bus scan. The XMOS pinout also lists this bus as the way the XVF3800 controls the DAC, so in the USB builds it
  probably acts as a master here; adding a second master needs care.
- **Power**: 5V_IN (pins 9, 10), VDDIO 3.3 V (pin 11), GND.

## Our unit

Read with `xvf_host.py` from the Seeed repository on 2026-09-28:

| Property | Value |
|---|---|
| USB name | `reSpeaker Flex XVF3800 C16K6Ch` |
| VID / PID | `0x2886` / `0x001e` |
| Firmware | `VERSION` 1.0.3, `BLD_MSG` `ua-io16-6ch-sqr` (image `respeaker_flex_usb_c16k6ch_v1.0.3.bin`) |
| Audio | 6 channels in, 2 channels out, 16 kHz, 16-bit |
| `AEC_MIC_ARRAY_TYPE` | 2 (squarecular) |
| `AUDIO_MGR_MIC_GAIN` | 10.0 |
| `AUDIO_MGR_SYS_DELAY` | 12 samples |

Microphone positions from `AEC_MIC_ARRAY_GEO`, in metres:

| Mic | X | Y |
|---|---|---|
| 0 | 0.022 | -0.022 |
| 1 | 0.022 | 0.022 |
| 2 | -0.022 | 0.022 |
| 3 | -0.022 | -0.022 |

## Input channels of the 6-channel firmware

Each USB input channel is fed by an output mux configured as a `(category, source)` pair. The values below were
read from the device; the meaning comes from Table 3.2 of the XMOS User Guide.

| USB channel (0-based) | Command | Value | Signal |
|---|---|---|---|
| 0 | `AUDIO_MGR_OP_L` | 8, 0 | User chosen channel — currently a copy of the auto-select processed beam |
| 1 | `AUDIO_MGR_OP_R` | 7, 3 | ASR output of the auto-select beam |
| 2 | `AUDIO_MGR_OP_CH3` | 1, 0 | Raw mic 0, before amplification |
| 3 | `AUDIO_MGR_OP_CH4` | 1, 1 | Raw mic 1, before amplification |
| 4 | `AUDIO_MGR_OP_CH5` | 1, 2 | Raw mic 2, before amplification |
| 5 | `AUDIO_MGR_OP_CH6` | 1, 3 | Raw mic 3, before amplification |

Mux categories (XMOS User Guide, Table 3.2):

| Category | Signal | Sources |
|---|---|---|
| 0 | Silence | 0 |
| 1 | Raw microphone data, before amplification | 0–3: mic index |
| 2 | Unpacked microphone data | 0–3 (packed input only) |
| 3 | Amplified microphone data with system delay | 0–3: what the SHF cores receive |
| 4 | Far end (reference) | 0 |
| 5 | Far end (reference) with system delay | 0 |
| 6 | Processed data | 0, 1: slow-moving beams; 2: fast-moving beam; 3: auto-select beam |
| 7 | AEC residual / ASR data | 0–3: per mic residual or per beam ASR output |
| 8 | User chosen channels | 0, 1: copy of the auto-select beam by default |
| 9 | Post SHF DSP channels | 0–3 |
| 10 | Far end at native rate | 0–5 |
| 11 | Amplified microphone data before system delay | 0–3 |
| 12 | Amplified far end with system delay | 0 |

## Control from macOS

`xvf_host.py` talks to the chip over USB control transfers and needs `pyusb` plus `libusb`
(`brew install libusb`). Without `--pid` it finds the device by the Seeed VID alone.

```bash
python xvf_host.py VERSION
python xvf_host.py --list              # every command with its description
python xvf_host.py DOA_VALUE           # angle 0..359 and a speech flag
python xvf_host.py AEC_AZIMUTH_VALUES  # radians: beam 1, beam 2, free-running, auto-select
python xvf_host.py AUDIO_MGR_OP_L 1 0  # route raw mic 0 to channel 0
python xvf_host.py SAVE_CONFIGURATION 1
```

Changes are lost on reboot unless saved with `SAVE_CONFIGURATION`; `CLEAR_CONFIGURATION` returns to defaults.
