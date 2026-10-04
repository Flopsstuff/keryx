# Keryx

**A "Hey Keryx" voice speaker for the [Hermes agent](https://github.com/NousResearch/hermes-agent).** A
[XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) on a
[Seeed reSpeaker Flex XVF3800](https://wiki.seeedstudio.com/respeaker_flex_introduction/) listens for its wake word
on the device, then lets you talk to Hermes out loud: your request goes over Wi-Fi to a small voice bridge next to
Hermes, and the answer comes back as speech. A sibling of [RaspiDR](https://github.com/Flopsstuff/raspidr) on
different hardware.

*Keryx* (κῆρυξ) is Greek for a herald — the one who speaks aloud for Hermes.

![The prototype: the reSpeaker Flex core board with the XIAO ESP32S3 underneath, its circular four-microphone array and the Wi-Fi antenna](docs/images/keryx-prototype.jpg)

## What it does

- **Wake word on the board.** "Hey Keryx" (or «Хей / Эй, Керикс») is recognized by a small streaming model trained
  for this project and running on the ESP32-S3; nothing leaves the room until it is said.
- **Talk, not commands.** After the wake word the conversation goes on without it: ask a follow-up, or talk over an
  answer to stop it. A few seconds of silence end the conversation.
- **Hermes does the thinking.** Requests go to Hermes through its OpenAI-compatible API, with all its tools, memory
  and skills; the answers are short and written for the ear.
- **Hermes can speak first.** The bridge gives Hermes `say.sh` for reminders, timers and anything you asked to be
  told later, and `set_volume.sh` for the speaker's volume.
- **The room stays clean.** The XVF3800 cancels Keryx's own voice from the microphones (its playback is the echo
  reference), beam-forms towards the speaker and suppresses noise; the bridge filters what echo is left.

## How it works

![Keryx architecture: the board's microphones go through the XVF3800 and the wake word on the ESP32-S3, then over a Wi-Fi WebSocket to the voice bridge on a Raspberry Pi 5, which runs xAI speech-to-text, Hermes and xAI text-to-speech and streams the answer back to the board's speaker](docs/images/keryx-architecture.jpg)

From the end of a sentence to the first sound takes about a second plus Hermes' own thinking time. Speech costs
roughly $0.004 per exchange with xAI ([why xAI](docs/speech-providers.md)).

## What you need

- A [reSpeaker Flex XVF3800](https://wiki.seeedstudio.com/respeaker_flex_introduction/) with a XIAO ESP32S3 and its
  U.FL Wi-Fi antenna, and a speaker on its 3.5 mm output (or a 4 Ω speaker on its JST amplifier output, which needs
  the 12 V input).
- [Hermes](https://github.com/NousResearch/hermes-agent) with its API server enabled.
- An [xAI API key](https://console.x.ai) for speech-to-text and text-to-speech.
- A Linux machine on the same network for the bridge — usually the one running Hermes. Python 3.10+, git-lfs.

## Quick start

On the machine that will run the bridge, with the board connected by USB:

```bash
git clone https://github.com/Flopsstuff/keryx.git && cd keryx
./setup.sh
```

`setup.sh` installs the bridge as a service, asks for the keys, flashes the board and pairs it with the bridge over
Wi-Fi; after that the board runs on any USB power supply. The XVF3800 itself needs Seeed's I2S firmware once,
before anything else — see [flashing](docs/flashing.md).

## Documentation

**[docs/index.md](docs/index.md)** is the map: flashing and pairing, the bridge, building the firmware, training
the wake word, the board's hardware notes and the measurements behind the design choices.

## License

[Apache License 2.0](LICENSE). Vendored components keep their own licenses: `firmware/components/micro_frontend`
(TensorFlow Lite Micro's frontend, Apache-2.0, with KISS FFT, BSD-3-Clause) and
`firmware/components/usb_device_uac` (Espressif, Apache-2.0). The board images in `docs/images/` other than the
photo of the prototype come from the [Seeed wiki](https://wiki.seeedstudio.com/respeaker_flex_introduction/).

Work in progress.
