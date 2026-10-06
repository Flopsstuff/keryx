# Building the firmware

How to build the ESP32-S3 firmware in [`firmware/`](../firmware/), flash development builds and cut a release.
Flashing a finished release needs none of this: see [flashing.md](flashing.md). What each project and component
does is in [firmware/README.md](../firmware/README.md).

## Toolchain

ESP-IDF **v5.5.5**, installed once:

```bash
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.5 --depth 1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32s3
brew install ninja ccache    # macOS
```

Every new shell needs `. ~/esp/esp-idf/export.sh` before `idf.py`. Managed components (esp-dsp, TinyUSB, the
WebSocket client) are downloaded on the first build into each project's `managed_components/`.

## Layout

| Path | What |
|---|---|
| `firmware/keryx/` | **the firmware**: wake word, USB sound card and console, Wi-Fi, the bridge link |
| `firmware/wakeword/`, `firmware/usb-soundcard/` | simpler predecessors, kept for experiments and recording |
| `firmware/xvf-bringup/`, `firmware/mic-loopback/` | first hardware checks |
| `firmware/components/` | shared components; `usb_device_uac/` and `micro_frontend/` are vendored, with local changes listed in their `KERYX.md` |
| `firmware/sounds/` | `make_sounds.py`: the chime and "thinking" sounds (`--export` writes `keryx/main/keryx_sounds.h`) |
| `firmware/release/` | the latest release images (Git LFS) |
| `wakeword/` | training of the wake word model; `export_model.py` writes `components/keryx_wakeword/kww_weights.h` and `include/kww_config.h` |

Each project's settings live in its `sdkconfig.defaults` (commented); `sdkconfig` is generated and not committed.
After changing `sdkconfig.defaults`, delete `sdkconfig` so the new defaults apply.

## Build

```bash
cd firmware/keryx
idf.py build
```

The first build sets the target from `sdkconfig.defaults` (esp32s3) and takes a few minutes; later ones are
incremental. `idf.py size` and `idf.py size-components` show memory use: internal RAM is the scarce resource
(the wake word's weights and state take ~160 KB of it, Wi-Fi needs its RX buffers there); PSRAM is on for
everything large.

## Flash a development build

```bash
firmware/flash.sh keryx      # any project name under firmware/
```

`flash.sh` builds, then finds the board by USB ID: a board running Keryx is told over its console to restart into
the ROM bootloader (`bootloader` command), one in the bootloader or with a USB serial/JTAG console is flashed at
once; otherwise it waits for you to press RESET on the XIAO. `idf.py -p PORT flash` works too while the board is in
the bootloader.

Only one program can hold the board's console at a time: stop terminals and scripts that have it open before
flashing.

## Console and logs

All projects log over the XIAO's USB-C, never UART0: on this board its pins, GPIO43/44, carry I2S data.
`firmware/keryx` shows the USB serial/JTAG console for the first ~4 s after boot, then TinyUSB takes the port and
the console moves to the CDC serial port next to the sound card (`/dev/cu.usbmodemkeryx_*`, `/dev/ttyACM*`). The
log goes there while a program has the port open. Useful commands for development:

| Command | |
|---|---|
| `status` | firmware version, memory, Wi-Fi, bridge, XVF3800, volume |
| `top [s]` | how busy each core was and which tasks took the time |
| `log <tag\|*> <level>` | change a log level until the next restart |
| `wake` | act as if the wake word fired (tests the bridge without speaking) |
| `volume [0-100\|up\|down]`, `mute [on\|off]` | the board's volume and microphone mute (both kept in NVS) |
| `loop on\|mic\|off` | put the echo reference, or microphone 0 before AEC, on the left capture channel, to measure playback and echo through the USB sound card |
| `xvf get\|set <resid> <cmd> <type> …` | any XVF3800 parameter over I2C |
| `bootloader`, `reboot` | restart into the ROM bootloader, or the firmware |

Every 5 s the log reports the wake word's peak score, the ASR level, processing time, the USB buffers and the
bridge link. The full list is in [firmware/README.md](../firmware/README.md#keryx).

## Version

The version a build carries (and `status` and the bridge's `hello` show) is the short hash of the last commit that
touched `firmware/`, outside `firmware/release/`, with `-dirty` if `firmware/` has uncommitted changes. Changes
elsewhere in the repository do not affect it. It is fixed when CMake configures, which happens again when the git
index or HEAD changes.

## Release

```bash
firmware/release.sh                 # refuses with uncommitted changes under firmware/
git add firmware/release && git commit -m "📦 Release $(cat firmware/release/VERSION)"
git push
firmware/publish.sh                 # the GitHub release that boards update from
```

`release.sh` reconfigures and builds `firmware/keryx`, then copies into `firmware/release/` the four parts
(`bootloader.bin` at 0x0, `partition-table.bin` at 0x8000, `ota_data_initial.bin` at 0xf000, `keryx.bin` at
0x20000), `flash_args`, `VERSION` and an ESP Web Tools `manifest.json`. They stay separate parts rather than one
merged image on purpose: a merged image fills the gaps with 0xFF, and NVS — the pairing — lies in such a gap at
0x9000. Only the latest release is kept; the `*.bin` files go to Git LFS.

`publish.sh` makes the same files a GitHub release, `firmware-<version>`, marked latest, with the firmware commits
since the previous one as notes; it refuses a dirty build, uncommitted `release/` or an unpushed commit. Boards
take it with `ota update` (below).

## Updates over Wi-Fi

The app lives in two 3 MB slots; an update writes the other one and restarts into it (`components/keryx_ota`). On
the console, over USB or over Wi-Fi (`./console.sh` on the bridge host):

```
ota                    the running version, its slot (ota_0 / ota_1) and whether it is on trial
ota check              the latest GitHub release against the running version
ota update [force]     install the latest release (force: even the same version)
ota url <http(s)://…>  install any image, e.g. a build served from this machine
```

From a development machine, `tools/ota_push.py --bridge http://<bridge host>:8765` (or `--usb`) serves
`firmware/keryx/build/keryx.bin` over http and tells the board to fetch it — flashing without a cable; the
machine's firewall must let the board in on port 8070.

A new image boots on trial: if it restarts within 30 s, the bootloader goes back to the previous one
(`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`); after 30 s the app keeps itself. The bootloader and the partition table
are not updated over Wi-Fi — a board needs one USB flash with a rollback bootloader (from 2026-10-06) before
updates over Wi-Fi are safe. The download runs in its own task with an 8 KB stack in internal RAM (a TLS handshake
needs it), TLS buffers in PSRAM; audio stutters while flash is written, and the ring shows the progress.

The partition table ([`firmware/keryx/partitions.csv`](../firmware/keryx/partitions.csv)) has NVS at 0x9000, two
3 MB app slots for updates over Wi-Fi and 1.9 MB of storage. Keep NVS where it is when changing it, or every
board loses its pairing.

## Changing…

- **The wake word model**: train and export in `wakeword/` ([wakeword/README.md](../wakeword/README.md));
  `export_model.py --check` compares the device's processing chain with the training one.
- **The sounds**: edit and listen to the candidates with `firmware/sounds/make_sounds.py`, pick them in `CHOSEN`,
  run it with `--export`.
- **Vendored components**: re-apply the changes listed in their `KERYX.md` when updating them from upstream.
- **The bridge protocol**: it is shared with `bridge/`; change both sides together
  ([firmware/README.md](../firmware/README.md#bridge-protocol-v1)).
