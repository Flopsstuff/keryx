#!/usr/bin/env bash
# Runs a command on the Keryx board's console, over Wi-Fi through the bridge on this machine, and prints its answer:
#   ./console.sh ring brightness
#   ./console.sh ring brightness night 5 22:00
#   ./console.sh status
# Only a safe set of commands runs this way (not set, erase, reboot, …): the rest need the board on USB.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
[ $# -gt 0 ] || { echo "usage: $0 <command>  (e.g. ring brightness)" >&2; exit 1; }
"$BRIDGE" console "$@"
