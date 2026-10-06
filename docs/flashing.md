# Flashing Keryx

How to put the Keryx firmware onto the hardware and check that it runs. Building the firmware yourself is in
[building.md](building.md); nothing here needs ESP-IDF.

## What you need

- A **Seeed reSpeaker Flex** (XMOS XVF3800) with a **XIAO ESP32S3** on its header, and the XIAO's **U.FL Wi-Fi
  antenna** attached (without it networks show at about −92 dBm).
- A speaker or headphones on the Flex's 3.5 mm jack.
- A computer with this repository, **Git LFS** (`git lfs install && git lfs pull`: the firmware images are LFS
  files) and Python 3.10+. For the XVF3800 step also `dfu-util` (`brew install dfu-util`, `apt install dfu-util`).
- Two USB-C ports on the board: the **Flex's** (next to its RST button) talks to the XVF3800, the **XIAO's** to the
  ESP32-S3. They are used in different steps.

## 1. The XVF3800 on its I2S firmware

Once per reSpeaker Flex. It ships with USB firmware, which does not clock the I2S bus the XIAO listens to.

1. Download `respeaker_flex_i2s_c48k2ch_v1.0.4.bin` from the
   [reSpeaker Flex repository](https://github.com/respeaker/reSpeaker_Flex) (`xmos_firmwares/i2s`).
2. Connect the **Flex's** USB-C port and run:

   ```bash
   dfu-util -R -e -a 1 -D respeaker_flex_i2s_c48k2ch_v1.0.4.bin
   ```

   Afterwards the board disappears from USB as a sound card: from now on the XIAO is its only way out.

To go back to the USB firmware: power the board off, hold its Boot button while powering it on (safe mode), then
flash a USB image the same way; see [Switching firmware](respeaker-flex-xvf3800.md#switching-firmware).

## 2. The XIAO

### All in one

On the machine that will run the voice bridge (usually the Raspberry Pi with Hermes), with the board on the XIAO's
USB-C port:

```bash
./setup.sh
```

It installs the bridge, flashes the board and pairs it; see [bridge/README.md](../bridge/README.md) and the
comment at the top of `setup.sh` for its options (`--no-flash`, `--no-service`, `--port`).

### Only the firmware

```bash
python3 -m venv .venv-flash && .venv-flash/bin/pip install esptool   # or any Python that has esptool
.venv-flash/bin/python firmware/flash_release.py                       # --port DEV if there are several boards
```

`flash_release.py` flashes the release in [`firmware/release/`](../firmware/release/) (its version is in
`firmware/release/VERSION`):

- It finds the board by USB ID. A board already running Keryx (`303a:8000`) is told over its console to restart
  into the ROM bootloader; a XIAO in the bootloader, or with firmware that keeps the USB serial/JTAG console
  (`303a:1001`), is flashed straight away. Otherwise it asks you to enter the bootloader by hand: hold **BOOT** on
  the XIAO, tap **RESET**, release BOOT.
- It writes the parts at their own addresses and leaves the NVS partition alone, so the pairing (Wi-Fi, bridge,
  token, volume) survives an update.
- It waits until the board has started the new firmware and checks the version it reports. Exit status 0 means
  done; otherwise it says what went wrong.

It takes about 20 s. By hand, the same thing is (from `firmware/release/`, the board in the bootloader):

```bash
python -m esptool --chip esp32s3 -p PORT -b 460800 write_flash @flash_args
```

The release's `manifest.json` also suits [ESP Web Tools](https://esphome.github.io/esp-web-tools/), for flashing
from a browser page.

## 3. Check

Open the board's console — the serial port the board shows on the XIAO's USB-C a few seconds after boot
(`/dev/cu.usbmodemkeryx_*` on macOS, `/dev/ttyACM*` on Linux):

```bash
python -m serial.tools.miniterm /dev/ttyACM0 115200   # pyserial; or screen, picocom...
```

and type `status`:

```
id=keryx-a1b2c3 firmware=0d29ff5 internal_free=27655 ...
wifi=connected ssid=… ip=192.168.1.42 rssi=-55 ch=1
bridge=ws://192.168.1.10:8765/keryx token=(set)
xvf=ok version=1.0.4 i2s=running
volume=100 muted=0
ok
```

- `xvf=ok … i2s=running`: the XVF3800 answers and runs its I2S firmware.
- `xvf=no_answer`: the XIAO does not reach the XVF3800 over I2C: is the XIAO seated on the Flex?
- `i2s=no_clock`: the XVF3800 does not clock I2S: it still runs USB firmware (step 1).
- `wifi=not_configured`, `bridge=(not set)`: not paired yet (step 4).

Then say "Hey Keryx": the board chimes and the console prints `wake score=0.97`.

## 4. Pairing

`keryx-bridge pair` (part of `setup.sh`) gives the board the Wi-Fi, the bridge's address and its token over the
same console; see [bridge/README.md](../bridge/README.md#pair-the-board). By hand, on the console:

```
wifi scan
set ssid MyNetwork
set password ********
set bridge ws://192.168.1.10:8765/keryx
set token <the bridge's KERYX_BRIDGE_TOKEN>
net check 192.168.1.10 8765
status
```

Every command answers with a line starting with `ok` or `error`. `config` shows the settings (password and token
only as `(set)`), `erase` forgets them all. The full list of console commands is in
[firmware/README.md](../firmware/README.md#keryx).

## Later: updates over Wi-Fi

Once a board runs a release with updates over Wi-Fi, newer releases need no cable: `./console.sh ota update` on the
bridge host (or `ota update` on the console) installs the latest GitHub release. See
[building.md](building.md#updates-over-wi-fi).

## Troubleshooting

| Symptom | What to do |
|---|---|
| `flash_release.py`: no board found | Use the XIAO's own USB-C port, not the Flex's. Enter the bootloader by hand: hold BOOT, tap RESET, release BOOT |
| `Git LFS pointer, not the image` | `git lfs install && git lfs pull` |
| `cannot open /dev/ttyACM0: Permission denied` (Linux) | `sudo usermod -aG dialout $USER`, then log in again |
| Flashed, but no console or sound card appears | They show up about 5 s after boot (the first seconds belong to the USB serial/JTAG console); otherwise press RESET on the XIAO |
| `wifi disconnected reason=wrong_password` / `no_ap_found` | `set password …` again; the XIAO only sees 2.4 GHz networks; check the antenna |
| `net check` to the bridge fails | The bridge is down, or the network isolates clients (guest/IoT networks): allow the board to reach the bridge's host and port |
| Start over completely | `python -m esptool --chip esp32s3 erase_flash`, then flash again: this also erases the pairing |
