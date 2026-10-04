"""keryx-bridge: run the voice bridge, check its settings, or talk to a running one.

  keryx-bridge run [options]     the bridge itself (what the systemd service runs)
  keryx-bridge check             settings, the system prompt, Hermes and xAI, without a board
  keryx-bridge pair [options]    give the board on USB the Wi-Fi, this bridge's address and its token
  keryx-bridge status            what a running bridge on this machine knows about the board
  keryx-bridge volume 60|+10|-10 set the board's volume through a running bridge
"""

import argparse
import asyncio
import getpass
import json
import socket
import sys
import time

import aiohttp

from . import __version__
from .server import add_server_arguments, serve
from .voice import CONFIG, add_arguments, env, finish_arguments, system_prompt


def run(argv):
    parser = argparse.ArgumentParser(prog="keryx-bridge run", description="Run the voice bridge.")
    add_arguments(parser, "recordings/bridge")
    add_server_arguments(parser)
    args = finish_arguments(parser.parse_args(argv))
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


async def check_services():
    ok = True

    def report(good, what, detail=""):
        nonlocal ok
        ok &= good
        print(f"  {'ok ' if good else 'BAD'}  {what}{': ' + detail if detail else ''}")

    print(f"config: {CONFIG or 'none (the environment only)'}")
    for name in ("XAI_API_KEY", "HERMES_API_KEY", "KERYX_BRIDGE_TOKEN"):
        try:
            report(bool(env(name)), name, "set")
        except SystemExit:
            report(False, name, "missing")
    try:
        prompt, source = system_prompt(None)
        report(True, "system prompt", f"{source}, {len(prompt)} chars")
    except SystemExit as e:
        report(False, "system prompt", str(e))
    hermes = env("HERMES_URL", "http://127.0.0.1:8642/v1")
    async with aiohttp.ClientSession() as session:
        try:
            t0 = time.monotonic()
            headers = {"Authorization": f"Bearer {env('HERMES_API_KEY', '')}"}
            async with session.get(f"{hermes}/models", headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                body = await resp.text()
                detail = f"{hermes}, HTTP {resp.status}, {(time.monotonic() - t0) * 1000:.0f} ms"
                if resp.status == 200:
                    detail += f", model {json.loads(body)['data'][0]['id']!r}"
                report(resp.status == 200, "Hermes", detail)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            report(False, "Hermes", f"{hermes}: {e!r}")
        try:
            t0 = time.monotonic()
            url = f"wss://api.x.ai/v1/tts?language=ru&voice={env('XAI_VOICE', 'eve')}&codec=pcm&sample_rate=24000"
            async with session.ws_connect(url, headers={"Authorization": f"Bearer {env('XAI_API_KEY', '')}"}) as ws:
                await ws.send_str(json.dumps({"type": "text.delta", "delta": "Проверка."}))
                await ws.send_str(json.dumps({"type": "text.done"}))
                event = json.loads((await ws.receive(timeout=10)).data)
                good = event.get("type") == "audio.delta"
                report(good, "xAI text-to-speech",
                       f"voice {env('XAI_VOICE', 'eve')}, first audio in {(time.monotonic() - t0) * 1000:.0f} ms"
                       if good else str(event)[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            report(False, "xAI text-to-speech", repr(e))
    return ok


async def control(method, path, body=None):
    port = int(env("KERYX_BRIDGE_PORT", "8765"))
    url = f"http://127.0.0.1:{port}/keryx/{path}"
    headers = {"Authorization": f"Bearer {env('KERYX_BRIDGE_TOKEN')}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, json=body, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status, await resp.json()
        except aiohttp.ClientError as e:
            raise SystemExit(f"no bridge at {url}: {e!r}")


BOARD_USB = (0x303A, 0x8000)  # the Keryx firmware's USB VID:PID


def local_ip():
    """This machine's address on the network the default route goes to (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]


class BoardConsole:
    """The board's USB serial console: commands answered by a final `ok …` or `error …` line."""

    def __init__(self, device):
        import serial

        self.port = serial.Serial(device, timeout=0.2)
        self.port.reset_input_buffer()

    def command(self, line, timeout=5.0, show=True):
        self.port.write((line + "\n").encode())
        deadline, lines = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            text = self.port.readline().decode(errors="replace").strip()
            if not text or text.startswith("wake "):
                continue
            if text == "ok" or text.startswith("ok "):
                return True, lines
            if text.startswith("error"):
                return False, lines + [text]
            if show:
                lines.append(text)
        return False, lines + ["(no answer)"]

    def wait_for(self, prefix, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.port.readline().decode(errors="replace").strip()
            if text.startswith(prefix):
                return text
        return None


def pair(argv):
    parser = argparse.ArgumentParser(prog="keryx-bridge pair",
                                     description="Configure the board connected by USB to use this bridge.")
    parser.add_argument("--device", help="the board's serial port (default: found by its USB id)")
    parser.add_argument("--ssid", help="Wi-Fi network (asked when not given)")
    parser.add_argument("--keep-wifi", action="store_true", help="leave the board's Wi-Fi settings as they are")
    parser.add_argument("--url", help="bridge URL for the board (default ws://<this machine>:<port>/keryx)")
    args = parser.parse_args(argv)
    from serial.tools import list_ports

    device = args.device or next((p.device for p in list_ports.comports()
                                  if (p.vid, p.pid) == BOARD_USB), None)
    if device is None:
        sys.exit("no Keryx board on USB (connect it, or name the port with --device)")
    url = args.url or f"ws://{local_ip()}:{int(env('KERYX_BRIDGE_PORT', '8765'))}/keryx"
    token = env("KERYX_BRIDGE_TOKEN")
    board = BoardConsole(device)
    print(f"board on {device}")
    good, lines = board.command("status")
    for line in lines:
        print(f"  {line}")
    if not good:
        sys.exit("the board does not answer `status`: is it running the keryx firmware?")
    if any("xvf=no_answer" in line or "i2s=no_clock" in line for line in lines):
        print("warning: the XVF3800 does not run its I2S firmware, so the board hears nothing and plays nothing;\n"
              "         flash respeaker_flex_i2s_c48k2ch onto the reSpeaker Flex first (see docs/flashing.md, step 1)")

    def setting(name, value, shown):
        good, lines = board.command(f"set {name} {value}", show=False)
        print(f"  set {name} {shown}: {'ok' if good else ' '.join(lines)}")
        if not good:
            sys.exit(1)

    if not args.keep_wifi:
        ssid = args.ssid or input("Wi-Fi network (SSID): ").strip()
        password = getpass.getpass(f"password for {ssid}: ")
        setting("ssid", ssid, ssid)
    setting("bridge", url, url)
    setting("token", token, "(the bridge's KERYX_BRIDGE_TOKEN)")
    if not args.keep_wifi:
        setting("password", password, "(hidden)")
        print("waiting for the board to join Wi-Fi…")
        joined = board.wait_for("wifi connected", 30)
        print(f"  {joined or 'not joined within 30 s: check the network name and password, then pair again'}")
        if not joined:
            sys.exit(1)
    host, port = url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)
    good, lines = board.command(f"net check {host} {port}", timeout=10)
    for line in lines:
        print(f"  {line}")
    print(f"  the board {'reaches' if good else 'cannot reach'} {host}:{port}")
    if good:
        print("paired: the board connects to the bridge within a few seconds (keryx-bridge status shows it)")
    else:
        print("is the bridge running here, and does a firewall let the board in on that port?")
        sys.exit(1)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    command, rest = argv[0], argv[1:]
    if command == "--version":
        print(__version__)
    elif command == "run":
        run(rest)
    elif command == "pair":
        pair(rest)
    elif command == "check":
        sys.exit(0 if asyncio.run(check_services()) else 1)
    elif command == "status":
        status, body = asyncio.run(control("GET", "status"))
        print(json.dumps(body, ensure_ascii=False, indent=1))
        sys.exit(0 if status == 200 else 1)
    elif command == "volume" and len(rest) == 1:
        value = rest[0]
        body = {"delta": int(value)} if value[0] in "+-" else {"value": int(value)}
        status, answer = asyncio.run(control("POST", "volume", body))
        print(json.dumps(answer, ensure_ascii=False))
        sys.exit(0 if status == 200 else 1)
    else:
        sys.exit(f"unknown command {' '.join(argv)!r}\n\n{__doc__.strip()}")


if __name__ == "__main__":
    main()
