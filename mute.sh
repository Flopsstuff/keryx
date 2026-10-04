#!/usr/bin/env bash
# Stops the Keryx board listening (only the microphone: it still speaks), through the bridge on this machine:
#   ./mute.sh          until ./unmute.sh
#   ./mute.sh 30m      for a while (90s, 30m, 2h; a bare number is minutes), then the bridge unmutes it
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
"$BRIDGE" mute "$@"
