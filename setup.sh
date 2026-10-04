#!/usr/bin/env bash
# Keryx in one go, on the machine that will run the bridge (usually the Hermes host), with the board on USB:
#
#   ./setup.sh                flash the board, install the bridge and its service, pair the board
#   ./setup.sh --no-flash     the board already runs the keryx firmware
#   ./setup.sh --no-service   no systemd service (macOS, or a computer that only flashes and pairs)
#   ./setup.sh --port DEV     the board's serial port, when there is more than one candidate
#
# Each step can also run on its own: bridge/install.sh, firmware/flash_release.py, keryx-bridge pair
# (see bridge/README.md, also for flashing and pairing from another computer).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
VENV=${KERYX_VENV:-$ROOT/bridge/.venv}
FLASH=yes
INSTALL_ARGS=()
PORT=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-flash) FLASH=no ;;
        --no-service) INSTALL_ARGS+=(--no-service) ;;
        --port) PORT=(--port "$2"); shift ;;
        *) sed -n '2,9p' "$0"; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

CONFIG=${KERYX_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/keryx/bridge.env}

step "1/3 Bridge"
"$ROOT/bridge/install.sh" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"} || true
missing=$(grep -E '^(XAI_API_KEY|HERMES_API_KEY)=[[:space:]]*$' "$CONFIG" | cut -d= -f1 || true)
if [ -n "$missing" ] && [ -t 0 ]; then
    step "Keys for $CONFIG"
    echo "XAI_API_KEY: https://console.x.ai — HERMES_API_KEY: API_SERVER_KEY in ~/.hermes/.env on the Hermes host"
    for name in $missing; do
        read -r -s -p "$name: " value; echo
        KEY="$name" VALUE="$value" "$VENV/bin/python" - "$CONFIG" <<'PY'
import os, re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(rf"^{os.environ['KEY']}=.*$", lambda m: f"{os.environ['KEY']}={os.environ['VALUE']}", text, flags=re.M)
open(path, "w").write(text)
PY
    done
    if ! grep -q '^HERMES_URL=' "$CONFIG"; then
        read -r -p "Hermes API URL [http://127.0.0.1:8642/v1]: " url
        [ -n "$url" ] && printf 'HERMES_URL=%s\n' "$url" >> "$CONFIG"
    fi
    "$ROOT/bridge/install.sh" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"} || true
fi

if [ "$FLASH" = yes ]; then
    step "2/3 Firmware"
    if [ ! -f "$ROOT/firmware/flash_release.py" ]; then
        echo "firmware/flash_release.py is not in this checkout yet: flash the board as firmware/README.md says,"
        echo "then run ./setup.sh --no-flash"
        exit 1
    fi
    # the release in firmware/release/ (Git LFS); the bridge's settings in the board's NVS survive it
    if grep -lqs '^version https://git-lfs' "$ROOT"/firmware/release/*.bin; then
        echo "firmware/release/*.bin are Git LFS pointers, not the firmware: install git-lfs, then"
        echo "    git lfs install && git lfs pull"
        exit 1
    fi
    "$VENV/bin/pip" install -q esptool
    "$VENV/bin/python" "$ROOT/firmware/flash_release.py" ${PORT[@]+"${PORT[@]}"}
else
    step "2/3 Firmware: skipped (--no-flash)"
fi

step "3/3 Pairing"
DEVICE=()
[ ${#PORT[@]} -gt 0 ] && DEVICE=(--device "${PORT[1]}")
"$VENV/bin/keryx-bridge" pair ${DEVICE[@]+"${DEVICE[@]}"}

step "Done"
sleep 3
"$VENV/bin/keryx-bridge" status || true
cat <<EOF

Say "Hey Keryx". The board can now move to any USB power supply.
Logs:      journalctl --user -u keryx-bridge -f
Settings:  ${XDG_CONFIG_HOME:-$HOME/.config}/keryx/bridge.env  (then: systemctl --user restart keryx-bridge)
EOF
