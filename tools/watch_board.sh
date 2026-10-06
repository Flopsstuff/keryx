#!/usr/bin/env bash
# Watch the Keryx board once a second: its USB serial port on this Mac, ping over Wi-Fi, and whether the bridge
# sees it. Prints a line every second; changes are marked with "<<<". Ctrl-C to stop.
#   tools/watch_board.sh [board IP] [bridge http address]
# Without arguments: KERYX_BOARD_IP and KERYX_BRIDGE_HTTP (e.g. http://192.168.1.10:8765), from the environment or
# the repository's .env, as is the bridge's KERYX_BRIDGE_TOKEN.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
setting() {
    local value=${!1:-}
    [ -n "$value" ] || value=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'")
    echo "$value"
}
BOARD_IP=${1:-$(setting KERYX_BOARD_IP)}
BRIDGE=${2:-$(setting KERYX_BRIDGE_HTTP)}
TOKEN=$(setting KERYX_BRIDGE_TOKEN)
if [ -z "$BOARD_IP" ] || [ -z "$BRIDGE" ]; then
    echo "usage: $0 <board IP> <bridge http address>, or KERYX_BOARD_IP and KERYX_BRIDGE_HTTP in .env" >&2
    exit 1
fi

prev=""
while true; do
    if ls /dev/cu.usbmodemkeryx_* > /dev/null 2>&1; then usb="USB ok  "; else usb="USB --  "; fi
    if ms=$(ping -c 1 -t 1 "$BOARD_IP" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p') && [ -n "$ms" ]; then
        wifi=$(printf "ping %4.0f ms" "$ms")
    else
        wifi="ping --     "
    fi
    if [ -n "$TOKEN" ] && curl -s -m 1 -H "Authorization: Bearer $TOKEN" "$BRIDGE/keryx/status" 2>/dev/null |
        grep -q '"connected": true'; then
        bridge="bridge ok"
    else
        bridge="bridge --"
    fi
    state="$usb  $wifi  $bridge"
    key="${usb}${wifi:0:6}${bridge}"  # the ping time changes every second; only up/down counts as a change
    mark=""
    [ -n "$prev" ] && [ "$key" != "$prev" ] && mark="  <<<"
    echo "$(date +%H:%M:%S)  $state$mark"
    prev=$key
    sleep 1
done
