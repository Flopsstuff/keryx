#!/usr/bin/env bash
# The bridge's wake log: every wake word the board heard, kept for training it. Through the bridge on this machine:
#   ./wake_label.sh                    the latest 10 wakes: id, time, score, label
#   ./wake_label.sh false              the latest wake was false (Keryx woke up though nobody called it)
#   ./wake_label.sh false <id>         that one was; `true` for a real one, `none` takes the label off
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
BRIDGE=${KERYX_VENV:-$ROOT/bridge/.venv}/bin/keryx-bridge
[ -x "$BRIDGE" ] || { echo "the bridge is not installed here (bridge/install.sh)" >&2; exit 1; }
if [ $# -eq 0 ]; then
    "$BRIDGE" wakes
else
    "$BRIDGE" wake-label "$@"
fi
