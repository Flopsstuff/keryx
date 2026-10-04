#!/usr/bin/env bash
# The Keryx board's volume, through the bridge running on this machine:
#   ./set_volume.sh          print the current volume
#   ./set_volume.sh 60       set it, 0..100 (100 is the loudest, 0.5 dB a step)
#   ./set_volume.sh +10      louder by 10 steps (-10 quieter)
# Prints the volume the board reports back; fails when the bridge or the board does not answer.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
case "${1:-}" in
    "") "$BRIDGE" status | sed -n 's/.*"volume": *\([0-9]*\).*/\1/p' ;;
    [0-9]*|[+-][0-9]*) "$BRIDGE" volume "$1" | sed -n 's/.*"volume": *\([0-9]*\).*/\1/p' ;;
    *) sed -n '2,6p' "$0" >&2; exit 2 ;;
esac
