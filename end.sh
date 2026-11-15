#!/usr/bin/env bash
# Ends the conversation on the Keryx board, through the bridge on this machine, without waiting for silence:
#   ./end.sh          once the answer in progress has been played (Keryx stops listening then)
#   ./end.sh --now    at once, cutting the answer, as the stop button does
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
"$BRIDGE" end "$@"
