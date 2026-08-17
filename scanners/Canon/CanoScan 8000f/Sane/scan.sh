#!/bin/sh
# Scan via the canon8000f SANE backend straight to a PNG on the Desktop.
#
#   ./scan.sh                 300 dpi colour
#   ./scan.sh 150             150 dpi colour
#   ./scan.sh 600 Gray        600 dpi grayscale
#   ./scan.sh 150 Color test  -> ~/Desktop/test.png
#
# Two things this exists to get right, both of which fail confusingly by hand:
#   - the arm64 scanimage must be used; an Intel one on PATH cannot load an
#     arm64 backend and reports "open of device ... failed: Invalid argument"
#   - the output format comes from the file extension, so the .png matters
set -e

DPI=${1:-300}
MODE=${2:-Color}
NAME=${3:-}

SCANIMAGE=/opt/homebrew/bin/scanimage
[ -x "$SCANIMAGE" ] || SCANIMAGE=$(command -v scanimage) || {
    echo "scanimage not found" >&2; exit 1; }

case "$(file -b "$SCANIMAGE" 2>/dev/null)" in
    *x86_64*) [ "$(uname -m)" = arm64 ] && echo \
        "warning: $SCANIMAGE is x86_64 on an arm64 host; it cannot load an arm64 backend" >&2 ;;
esac

if [ -z "$NAME" ]; then
    NAME="scan_${DPI}dpi_$(date +%Y%m%d_%H%M%S)"
fi
OUT="$HOME/Desktop/${NAME}.png"

echo "scanning ${DPI} dpi ${MODE} -> ${OUT}"
"$SCANIMAGE" --device canon8000f:0 \
             --resolution "$DPI" \
             --mode "$MODE" \
             --format=png \
             -o "$OUT"

echo "wrote $OUT"
