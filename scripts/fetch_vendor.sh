#!/usr/bin/env bash
# Vendors the LiveKit browser SDK so the ops console has no CDN dependency at
# demo time -- a flaky network should never break a recording.
set -euo pipefail
DEST="$(dirname "$0")/../ops/vendor"
mkdir -p "$DEST"
curl -sfL --max-time 60 \
  "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js" \
  -o "$DEST/livekit-client.umd.min.js"
echo "vendored $(wc -c < "$DEST/livekit-client.umd.min.js") bytes -> $DEST"
