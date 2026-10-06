#!/usr/bin/env bash
# Publish firmware/release/ as the latest GitHub release, where the boards' `ota update` finds it:
#   firmware/release.sh && git add firmware/release && git commit -m "📦 Release <version>" && git push
#   firmware/publish.sh
# The release is tagged firmware-<version> on the commit that holds firmware/release/, so that commit must be pushed.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
version=$(cat release/VERSION)
case "$version" in
*-dirty) echo "release/ holds a dirty build ($version): commit firmware/ and run release.sh again" >&2; exit 1 ;;
esac
if ! git diff --quiet HEAD -- release; then
    echo "release/ has uncommitted changes: commit them first" >&2
    exit 1
fi
head=$(git rev-parse HEAD)
if ! git branch -r --contains "$head" | grep -q .; then
    echo "$(git rev-parse --short HEAD) is not pushed yet: push first" >&2
    exit 1
fi
if [ "$(head -c1 release/keryx.bin | od -An -tx1 | tr -d ' ')" != "e9" ]; then
    echo "release/keryx.bin is not an ESP32 image (a Git LFS pointer? git lfs pull)" >&2
    exit 1
fi
tag="firmware-$version"
if gh release view "$tag" > /dev/null 2>&1; then
    echo "$tag is published already" >&2
    exit 1
fi

# notes: the firmware commits since the previous firmware release
previous=$(git tag --list 'firmware-*' --sort=-creatordate | head -1)
range=${previous:+$previous..}$head
notes=$(git log --format='- %s' "$range" -- . ':(exclude)release' | head -40)
gh release create "$tag" --target "$head" --latest --title "Firmware $version" \
    --notes "${notes:-firmware $version}" \
    release/keryx.bin release/VERSION release/manifest.json release/bootloader.bin release/partition-table.bin \
    release/ota_data_initial.bin release/flash_args
echo "published $tag: boards get it with \`ota update\`"
