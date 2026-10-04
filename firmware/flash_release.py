#!/usr/bin/env python3
"""Flash the Keryx firmware from firmware/release/ onto the XIAO ESP32S3, without ESP-IDF.

    python firmware/flash_release.py [--port DEV] [--release DIR]

Run it with a Python that has esptool (it brings pyserial), e.g. bridge/.venv/bin/python. The board is found by USB
ID: Keryx firmware (303a:8000) is asked over its console to restart into the ROM bootloader; a XIAO whose firmware
keeps the USB serial/JTAG console, or one already in the bootloader (303a:1001), is flashed straight away; with
neither, you are asked to put the XIAO into the bootloader by hand. The parts are written at their own addresses,
so the pairing in NVS survives. Exits 0 once the board has started the new firmware and reports its version.
"""

import argparse
import pathlib
import subprocess
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing: run this with a Python that has esptool installed (pip install esptool)")

KERYX = (0x303A, 0x8000)      # Keryx firmware: TinyUSB sound card + console
BOOTLOADER = (0x303A, 0x1001)  # USB serial/JTAG: ROM bootloader, or firmware that keeps that console
HERE = pathlib.Path(__file__).resolve().parent


def find(usb_id):
    for port in list_ports.comports():
        if (port.vid, port.pid) == usb_id:
            return port.device
    return None


def wait_for(usb_id, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        port = find(usb_id)
        if port:
            return port
        time.sleep(0.1)
    return None


def fail(message):
    print(f"\nflash_release: {message}", file=sys.stderr)
    sys.exit(1)


def check_release(release):
    version_file = release / "VERSION"
    flash_args = release / "flash_args"
    if not version_file.exists() or not flash_args.exists():
        fail(f"no release in {release}: VERSION or flash_args is missing (build one with firmware/release.sh)")
    for part in release.glob("*.bin"):
        with open(part, "rb") as f:
            if f.read(40).startswith(b"version https://git-lfs"):
                fail(f"{part.name} is a Git LFS pointer, not the image: run `git lfs install && git lfs pull`")
    return version_file.read_text().strip(), flash_args.read_text().split()


def open_console(port):
    for _ in range(50):  # the port shows up a moment before it can be opened
        try:
            return serial.Serial(port, timeout=0.3)
        except serial.SerialException as e:
            last = e
            time.sleep(0.1)
    if "ermission" in str(last):
        fail(f"cannot open {port}: {last}. On Linux add yourself to the dialout group (sudo usermod -aG dialout $USER, "
             "then log in again)")
    fail(f"cannot open {port}: {last}")


def to_bootloader(port_arg):
    if port_arg:
        console = port_arg if find(KERYX) == port_arg else None
        if console is None:
            return port_arg
    else:
        console = find(KERYX)
    if console:
        print(f"Keryx firmware on {console}: asking it to restart into the bootloader")
        with open_console(console) as c:
            c.write(b"bootloader\n")
            c.flush()
        port = wait_for(BOOTLOADER, 20)
        if not port:
            fail("the board did not come back in the bootloader within 20 s. Hold BOOT on the XIAO, tap RESET, "
                 "release BOOT, and run this again")
        return port
    port = find(BOOTLOADER)
    if port:
        return port
    print("No Keryx board found. Put the XIAO ESP32S3 into its bootloader: hold BOOT, tap RESET, release BOOT "
          "(the XIAO's own USB-C port, not the reSpeaker's). Waiting up to 60 s...")
    port = wait_for(BOOTLOADER, 60)
    if not port:
        fail("no board in the bootloader within 60 s")
    return port


def write_flash(port, release, flash_args):
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "-p", port, "-b", "460800",
           "--before", "default_reset", "--after", "hard_reset", "write_flash", *flash_args]
    for attempt in range(1, 6):
        result = subprocess.run(cmd, cwd=release, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"written via {port}")
            return
        if "No module named esptool" in result.stderr:
            fail("esptool is missing: pip install esptool into the Python you run this with")
        time.sleep(0.5)
    print(result.stdout[-1500:], result.stderr[-1500:], sep="\n", file=sys.stderr)
    fail(f"esptool failed {attempt} times on {port}")


def check_running(version):
    print("waiting for the new firmware to start...")
    console = wait_for(KERYX, 30)  # it keeps the serial/JTAG console ~4 s after boot, then TinyUSB takes the port
    if not console:
        fail("the board did not come up as Keryx within 30 s after flashing (is the XVF3800 board attached?)")
    with open_console(console) as c:
        time.sleep(0.5)
        c.reset_input_buffer()
        c.write(b"status\n")
        lines, end = [], time.monotonic() + 5
        while time.monotonic() < end:
            line = c.readline().decode(errors="replace").strip()
            if not line or line.startswith("I (") or line.startswith("Keryx "):
                continue
            lines.append(line)
            if line == "ok" or line.startswith("error"):
                break
    for line in lines:
        print("  " + line)
    running = next((w.split("=", 1)[1] for l in lines for w in l.split() if w.startswith("firmware=")), None)
    if running != version:
        fail(f"the board runs {running or 'something unknown'}, not {version}")
    print(f"Keryx {version} is running on {console}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port of the board (default: found by USB ID)")
    parser.add_argument("--release", type=pathlib.Path, default=HERE / "release", help="release directory")
    args = parser.parse_args()
    version, flash_args = check_release(args.release)
    print(f"flashing Keryx {version} from {args.release}")
    port = to_bootloader(args.port)
    write_flash(port, args.release, flash_args)
    check_running(version)


if __name__ == "__main__":
    main()
