#!/usr/bin/env bash
# Installs the Keryx voice bridge on this machine: run it from a checkout of the repository.
#
#   bridge/install.sh               venv + package, ~/.config/keryx/bridge.env, the systemd user service
#   bridge/install.sh --no-service  everything but the service (macOS, or to run it by hand)
#   bridge/install.sh --uninstall   stop and remove the service (the config and the venv stay)
#
# Running it again after `git pull` updates the package and restarts the service.
set -euo pipefail

BRIDGE=$(cd "$(dirname "$0")" && pwd)
VENV=${KERYX_VENV:-$BRIDGE/.venv}
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/keryx
CONFIG=$CONFIG_DIR/bridge.env
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT=$UNIT_DIR/keryx-bridge.service
SERVICE=yes
case "${1:-}" in
    --no-service) SERVICE=no ;;
    --uninstall)
        systemctl --user disable --now keryx-bridge.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "keryx-bridge service removed; $CONFIG and $VENV are left in place"
        exit 0 ;;
    "") ;;
    *) sed -n '2,8p' "$0"; exit 2 ;;
esac

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Python"
python3 - <<'PY' || { echo "Python 3.10 or newer is needed"; exit 1; }
import sys
print(sys.version.split()[0])
sys.exit(sys.version_info < (3, 10))
PY

say "Package into $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$BRIDGE"
"$VENV/bin/keryx-bridge" --version

say "Config $CONFIG"
if [ -f "$CONFIG" ]; then
    echo "kept as it is"
else
    mkdir -p "$CONFIG_DIR"
    umask 077
    token=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
    sed "s|^KERYX_BRIDGE_TOKEN=.*|KERYX_BRIDGE_TOKEN=$token|" "$BRIDGE/bridge.env.example" > "$CONFIG"
    echo "created from bridge.env.example, with a new board token"
fi
missing=$(grep -E '^(XAI_API_KEY|HERMES_API_KEY|KERYX_BRIDGE_TOKEN)=[[:space:]]*$' "$CONFIG" | cut -d= -f1 | tr '\n' ' ' || true)

if [ "$SERVICE" = yes ]; then
    if ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
        echo "no systemd user session here: run the bridge with $VENV/bin/keryx-bridge run"
        SERVICE=no
    fi
fi
if [ "$SERVICE" = yes ]; then
    say "Service $UNIT"
    mkdir -p "$UNIT_DIR"
    sed -e "s|@BRIDGE@|$BRIDGE|g" -e "s|@VENV@|$VENV|g" "$BRIDGE/keryx-bridge.service.in" > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable keryx-bridge.service >/dev/null 2>&1
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
        echo "note: without lingering the service stops when you log out; to keep it running:"
        echo "      sudo loginctl enable-linger $USER"
    fi
fi

if [ -n "$missing" ]; then
    say "Next: fill in $missing in $CONFIG"
    echo "then run:  $VENV/bin/keryx-bridge check"
    [ "$SERVICE" = yes ] && echo "and:       systemctl --user restart keryx-bridge"
    exit 0
fi

say "Check"
if ! "$VENV/bin/keryx-bridge" check; then
    echo; echo "fix the settings above in $CONFIG, then run this script again"
    exit 1
fi
if [ "$SERVICE" = yes ]; then
    systemctl --user restart keryx-bridge.service
    sleep 2
    systemctl --user --no-pager status keryx-bridge.service | head -3
    echo "logs:  journalctl --user -u keryx-bridge -f"
fi
say "Next: connect the board by USB to this machine and run  $VENV/bin/keryx-bridge pair"
