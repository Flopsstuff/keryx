#!/usr/bin/env bash
# Puts voice ID where the bridge looks for it (~/.local/share/keryx/voiceid): profiles/voiceid.npz, which
# calibrate.py --save writes, and the speaker embedding models it names, from models/.
#
#   voiceid/install.sh              on this machine
#   voiceid/install.sh rpi5.local   on another one, over ssh; then restart its bridge
#
# The profiles are biometric: they go only where the bridge runs, never into git.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BUNDLE="$HERE/profiles/voiceid.npz"
DEST='.local/share/keryx/voiceid'
[ -f "$BUNDLE" ] || { echo "no $BUNDLE: run voiceid/.venv/bin/python voiceid/calibrate.py --channel l --save" >&2; exit 1; }

models=$("$HERE/.venv/bin/python" -c "import numpy as np, sys; print(' '.join(np.load(sys.argv[1])['models']))" "$BUNDLE")
files=("$BUNDLE")
for m in $models; do
    [ -f "$HERE/models/$m.onnx" ] || { echo "no $HERE/models/$m.onnx (see voiceid/README.md)" >&2; exit 1; }
    files+=("$HERE/models/$m.onnx")
done

if [ $# -eq 0 ]; then
    mkdir -p "$HOME/$DEST"
    cp "${files[@]}" "$HOME/$DEST/"
    echo "voice ID in $HOME/$DEST"
else
    ssh "$1" "mkdir -p ~/$DEST"
    scp -q "${files[@]}" "$1:$DEST/"
    echo "voice ID in $1:~/$DEST; restart the bridge there: systemctl --user restart keryx-bridge"
fi
