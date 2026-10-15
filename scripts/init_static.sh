#!/bin/sh
# Copy the image's built static assets into the shared portal_static volume (#542).
#
# Run by the compose `init` service on every `up`. The app serves app/static/ from that volume, so
# this is the only way a rebuilt image's scripts and CSS reach the browser. The copy is additive:
# files from older images stay, so a blue-green slot still running the previous build keeps the
# assets its templates reference. Afterwards every file of this image must be in the volume,
# byte-for-byte — otherwise exit non-zero, and the services that depend on init don't start.
set -eu

SRC="${1:-/app/app/static}"
DEST="${2:-/static}"

cp -r "$SRC/." "$DEST/"

status=0
cd "$SRC"
for file in $(find . -type f); do
    if ! cmp -s "$file" "$DEST/$file"; then
        echo "init: static asset not copied into the volume: ${file#./}" >&2
        status=1
    fi
done
[ "$status" -eq 0 ] && echo "init: $(find . -type f | wc -l) static assets in place"
exit "$status"
