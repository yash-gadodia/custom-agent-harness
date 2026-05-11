#!/bin/bash
# Weekly OpenClaw backup — Sun 04:00 SGT.
# `openclaw backup create` writes a tarball of config + credentials + sessions
# + workspaces. Belt-and-suspenders alongside the 2h git auto-sync (which
# doesn't include credentials by design).
#
# Failure DMs the operator via the assistant bot.

set -uo pipefail

BACKUP_DIR="$HOME/backups/openclaw"
KEEP_LAST=4
LOG="$HOME/.openclaw/scripts/openclaw-backup-weekly.log"
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

source "$HOME/.openclaw/scripts/lib/dm.sh"

mkdir -p "$BACKUP_DIR"
log "=== weekly backup starting ==="

if openclaw backup create --output "$BACKUP_DIR" --verify >> "$LOG" 2>&1 ; then
  log "backup ok"
  # Rotate — keep last $KEEP_LAST archives (tarballs only).
  cd "$BACKUP_DIR" && ls -1t openclaw-*.tar* 2>/dev/null | tail -n +$((KEEP_LAST+1)) | xargs rm -f --
  log "rotation done; kept $KEEP_LAST"
  exit 0
fi

err="backup failed — see $LOG"
log "$err"
dm_alert "⚠️ openclaw-backup-weekly: $err"
exit 1
