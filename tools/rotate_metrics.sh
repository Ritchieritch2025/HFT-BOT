#!/usr/bin/env bash
# Rider (a), W-A5: rotate a metrics ndjson at METRICS_ROTATE_BYTES (default
# 512 MB), keeping 3 generations (.1 newest → .3 oldest, oldest dropped by
# the mv overwrite). Called by pipeline_supervisor.sh between hourly
# ws_shadow runs (the writer is not running at that instant; readers open
# per-request, so the rename is safe). Portable: works on macOS + Linux
# (wc -c, no stat flags). Usage: rotate_metrics.sh <path-to-metrics.ndjson>
set -u
MF="${1:?usage: rotate_metrics.sh <metrics.ndjson>}"
LIMIT="${METRICS_ROTATE_BYTES:-536870912}"
[ -f "$MF" ] || exit 0
SIZE="$(wc -c < "$MF" | tr -d ' ')"
[ "$SIZE" -gt "$LIMIT" ] || exit 0
[ -f "$MF.2" ] && mv "$MF.2" "$MF.3"   # overwrites .3 = oldest dropped (keep 3)
[ -f "$MF.1" ] && mv "$MF.1" "$MF.2"
mv "$MF" "$MF.1"
echo "[rotate_metrics] $MF rotated at $SIZE bytes (limit $LIMIT, keep 3)"
exit 0
