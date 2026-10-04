#!/usr/bin/env bash
# Build firmware/keryx and put its images into firmware/release/, for flash_release.py (no ESP-IDF needed there).
#
# The release is the parts and their addresses, not one merged image: a merged image fills the gaps with 0xFF, and
# NVS (pairing: Wi-Fi, bridge, token, volume) lies in such a gap, at 0x9000. Only the latest release is kept; *.bin
# goes to Git LFS. Refuses to run with uncommitted changes under firmware/ (outside release/), so that the version
# a release carries is a commit; --force builds anyway, as "<commit>-dirty".
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" != "--force" ] && [ -n "$(git -C "$here" status --porcelain -- . ':(exclude)release')" ]; then
    echo "firmware/ has uncommitted changes; commit them first, or pass --force" >&2
    exit 1
fi

. "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" > /dev/null 2>&1
cd "$here/keryx"
idf.py reconfigure > /dev/null  # the version comes from git at configure time
idf.py build > /dev/null
version=$(python3 -c 'import json; print(json.load(open("build/project_description.json"))["project_version"])')

out="$here/release"
mkdir -p "$out"
cp build/bootloader/bootloader.bin build/partition_table/partition-table.bin build/ota_data_initial.bin \
   build/keryx.bin "$out/"
# the build's flash_args, with the files next to it
sed -e 's#bootloader/##; s#partition_table/##' build/flash_args > "$out/flash_args"
echo "$version" > "$out/VERSION"
cat > "$out/manifest.json" <<EOF
{
  "name": "Keryx",
  "version": "$version",
  "new_install_prompt_erase": false,
  "builds": [
    {
      "chipFamily": "ESP32-S3",
      "parts": [
        {"path": "bootloader.bin", "offset": 0},
        {"path": "partition-table.bin", "offset": 32768},
        {"path": "ota_data_initial.bin", "offset": 61440},
        {"path": "keryx.bin", "offset": 131072}
      ]
    }
  ]
}
EOF
echo "release $version in $out:"
ls -l "$out"
