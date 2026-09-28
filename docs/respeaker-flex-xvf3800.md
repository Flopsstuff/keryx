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
