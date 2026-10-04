# Keryx documentation

Keryx is a voice interface to [Hermes](https://github.com/NousResearch/hermes-agent): a XIAO ESP32S3 on a
reSpeaker Flex hears "Hey Keryx", streams the request over Wi-Fi to a voice bridge next to Hermes and plays the
answer.

## Getting started

- [Flashing](flashing.md) — put the firmware on the board (the XVF3800's I2S firmware, then the XIAO), check it and
  pair it with the bridge. No ESP-IDF needed.
- [setup.sh](../setup.sh) — all of it in one go on the bridge's machine: installs the bridge, flashes, pairs.
- [Bridge](../bridge/README.md) — the service between the board and Hermes: install, pairing, commands, HTTP
  control, settings.

## Development

- [Building the firmware](building.md) — toolchain, building, flashing development builds, console, versions,
  releases.
- [Firmware](../firmware/README.md) — what each ESP-IDF project and component does, the console commands, the
  bridge protocol v1, measurements.
- [Wake word](../wakeword/README.md) — data, training and export of the "Hey Keryx" model.
- [Tools](../tools/README.md) — bench tools on the Mac: XVF3800 control, the mic-array dashboard, recording around
  wake events, the USB conversation loop.

## Reference

- [reSpeaker Flex and the XVF3800](respeaker-flex-xvf3800.md) — the board: pins, the XVF3800's parameters and
  output mux, I2S mode, switching its firmware.
- [Speech providers](speech-providers.md) — the STT and TTS services measured for the bridge, and why xAI.
