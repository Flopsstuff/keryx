#!/usr/bin/env python3
"""Flash the Keryx board over Wi-Fi from this machine: serve a firmware image here and tell the board `ota url`.

    .venv/bin/python ota_push.py --bridge http://<bridge host>:8765      through the bridge's console (no USB)
    .venv/bin/python ota_push.py --usb                                   through the board's serial console

The image is ../firmware/keryx/build/keryx.bin (build it first: idf.py build), or --image. The board downloads it
from this machine over plain http, so this machine's firewall must let it in on --port. The board restarts into the
new image on trial: if it restarts again within 30 s, its bootloader goes back to the previous one.
"""

import argparse
import functools
import glob
import http.server
import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def env(name):
    """From the environment, else from the repository's .env (as the bridge reads it in a checkout)."""
    if os.environ.get(name):
        return os.environ[name]
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("\"'")
    return None


def local_ip(towards):
    """This machine's address on the way to `towards` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((towards, 9))
        return s.getsockname()[0]


def via_bridge(bridge, line):
    token = env("KERYX_BRIDGE_TOKEN")
    if not token:
        sys.exit("no KERYX_BRIDGE_TOKEN in the environment or ../.env")
    request = urllib.request.Request(f"{bridge.rstrip('/')}/keryx/console", data=json.dumps({"line": line}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=80) as response:
        return json.load(response)["output"]


def via_usb(line):
    import serial

    ports = glob.glob("/dev/cu.usbmodemkeryx_*") + glob.glob("/dev/ttyACM*")
    if not ports:
        sys.exit("no Keryx board on USB")
    with serial.Serial(ports[0], 115200, timeout=0.5) as port:
        time.sleep(0.3)
        port.reset_input_buffer()
        port.write((line + "\r\n").encode())
        output, end = [], time.time() + 10
        while time.time() < end:
            text = port.readline().decode(errors="replace").rstrip()
            if text:
                output.append(text)
                if text.startswith(("ok", "error")):
                    break
        return "\n".join(output) + "\n"


class Handler(http.server.SimpleHTTPRequestHandler):
    sent = 0

    def log_message(self, fmt, *args):
        print(f"  board: {fmt % args}", flush=True)

    def copyfile(self, source, outputfile):
        super().copyfile(source, outputfile)
        Handler.sent += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--bridge", help="the bridge's HTTP address, e.g. http://192.168.1.10:8765")
    how.add_argument("--usb", action="store_true", help="tell the board over its serial console")
    parser.add_argument("--image", default=str(ROOT / "firmware/keryx/build/keryx.bin"))
    parser.add_argument("--port", type=int, default=8070, help="where this machine serves the image (8070)")
    args = parser.parse_args()

    image = pathlib.Path(args.image)
    if not image.exists() or image.read_bytes()[:1] != b"\xe9":
        sys.exit(f"{image}: not an ESP32 image (build first, or a Git LFS pointer?)")
    towards = urllib.parse.urlparse(args.bridge).hostname if args.bridge else "192.0.2.1"
    url = f"http://{local_ip(socket.gethostbyname(towards))}:{args.port}/{image.name}"

    handler = functools.partial(Handler, directory=str(image.parent))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"serving {image} ({image.stat().st_size} bytes) at {url}")

    line = f"ota url {url}"
    output = via_bridge(args.bridge, line) if args.bridge else via_usb(line)
    print(output, end="")
    if not output.strip().splitlines()[-1].startswith("ok"):
        sys.exit(1)
    end = time.time() + 180
    while Handler.sent == 0 and time.time() < end:
        time.sleep(0.5)
    if Handler.sent == 0:
        sys.exit("the board did not download the image within 3 minutes (firewall? same network?)")
    print("downloaded; the board checks the image and restarts into it")
    time.sleep(2)
    server.shutdown()


if __name__ == "__main__":
    main()
