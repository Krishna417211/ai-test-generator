#!/usr/bin/env bash
# Render the technical reference to PDF.
#
# The PDF is a build artifact — edit Testra-Complete-Reference.html and re-run
# this. The original PDF was produced by headless Chrome and had no committed
# source, which meant the only way to update it was to reconstruct the markup
# from the rendered pages. Keeping the HTML in git is what stops that recurring.
set -euo pipefail

cd "$(dirname "$0")"

CHROME="${CHROME:-}"
if [ -z "$CHROME" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then CHROME="$c"; break; fi
  done
fi
[ -n "$CHROME" ] || { echo "No Chrome/Chromium found. Set CHROME=/path/to/chrome" >&2; exit 1; }

# --no-pdf-header-footer keeps Chrome's default URL/date furniture off the page;
# the document carries its own cover and section numbering.
"$CHROME" \
  --headless \
  --disable-gpu \
  --no-sandbox \
  --no-pdf-header-footer \
  --print-to-pdf=Testra-Complete-Reference.pdf \
  Testra-Complete-Reference.html

echo "Wrote docs/Testra-Complete-Reference.pdf"
