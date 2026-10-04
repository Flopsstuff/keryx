#!/usr/bin/env bash
# Says something on the Keryx board, through the bridge running on this machine:
#   ./say.sh "Таймер на десять минут вышел."
#   echo "text" | ./say.sh
# Waits for any answer Keryx is giving, then plays the text and returns once the board has played it.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
if [ $# -gt 0 ]; then
    "$BRIDGE" say "$*"
else
    "$BRIDGE" say
fi
