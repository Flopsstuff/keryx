#!/usr/bin/env bash
# Says something on the Keryx board, through the bridge running on this machine:
#   ./say.sh "Таймер на десять минут вышел."
#   ./say.sh --volume 30 "Спокойной ночи."     at volume 30 (0..100, the board's scale) instead of the board's
#   ./say.sh --volume -20 "Спокойной ночи."    20 steps (10 dB) below the board's volume; +10 above it
#   echo "text" | ./say.sh
# Waits for any answer Keryx is giving, then plays the text and returns once the board has played it.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
volume=()
if [ "${1:-}" = "--volume" ] || [ "${1:-}" = "-v" ]; then
    [ $# -ge 2 ] || { echo "usage: $0 [--volume 0..100|+n|-n] \"text\"" >&2; exit 1; }
    volume=(--volume "$2")
    shift 2
fi
if [ $# -gt 0 ]; then
    "$BRIDGE" say ${volume[@]+"${volume[@]}"} "$*"
else
    "$BRIDGE" say ${volume[@]+"${volume[@]}"}
fi
