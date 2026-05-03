#!/bin/bash
# Snapshots ~/.claude-openclaw/.credentials.json (the OpenClaw-isolated
# Claude Code OAuth token) so a corrupted/rotated token can be rolled back
# without forcing another browser login. Keeps the most recent 12 snapshots.
#
# Triggered every 5 minutes by LaunchAgent com.example.claude-creds-backup.
# Skips if the live file is unchanged from the most recent backup (no churn
# when nothing rotated).

set -euo pipefail

SRC="$HOME/.claude-openclaw/.credentials.json"
DST_DIR="$HOME/.claude-openclaw/.credentials-backups"
KEEP=12

[ -f "$SRC" ] || { echo "no live credentials file at $SRC; skipping"; exit 0; }

mkdir -p "$DST_DIR"
chmod 700 "$DST_DIR"

LATEST=$(ls -1t "$DST_DIR"/credentials-*.json 2>/dev/null | head -1 || true)
if [ -n "$LATEST" ] && cmp -s "$SRC" "$LATEST"; then
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)
DST="$DST_DIR/credentials-$TS.json"
cp -p "$SRC" "$DST"
chmod 600 "$DST"

ls -1t "$DST_DIR"/credentials-*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
