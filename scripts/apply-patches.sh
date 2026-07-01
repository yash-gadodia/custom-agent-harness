#!/bin/bash
# apply-patches.sh — re-apply every ~/.openclaw/scripts/patch-*.sh against the
# currently-installed OpenClaw dist, then restart the gateway if anything
# changed. Run it from launchd/systemd at boot, on a WatchPaths trigger against
# the installed package.json (fires on every `npm i -g openclaw@...`), and on
# a several-hour interval as a safety net.
#
# Patch-script contract (enforced by the banner classifier below):
#   - exit 0 + output containing "already applied"            → noop
#   - exit 0 + output containing "applied to"/"PATCH: applied" → applied
#   - exit non-zero                                            → failed (alert)
#   - exit 0 with any other output                             → unclear (alert;
#     this is how a patch silently no-ops when an upstream refactor breaks its
#     anchor match — treat it as a page, not a log line)
#
# Ordering matters: the gateway restart runs BEFORE the failure exit. A failed
# patch must not leave a sibling patch that DID apply sitting
# patched-on-disk-but-never-loaded (that exact failure shipped a 24h window
# with a customer-safety patch unloaded on the deployment this is from).
#
# Optional runtime gate assertion: if your deployment patches a deterministic
# outbound gate into the dist (the "LLM can hallucinate, the gate decides what
# actually sends" pattern), set:
#   OPENCLAW_GATE_ASSERT=1
#   OPENCLAW_GATE_SEARCH_ROOTS="/path/to/dist ..."   # dirs to scan
#   OPENCLAW_GATE_MATCH="const sendTrackedMessage = async"  # file locator
#   OPENCLAW_GATE_SENTINEL="MY_GATE_BEGIN"           # marker your patch inserts
# Every run then asserts (1) every matching dist file carries the sentinel and
# (2) no matching file is newer than the running gateway/node-host processes
# (newer = patched-but-not-loaded → restart needed). Fails loud via dm_alert.

set -uo pipefail

SCRIPTS_DIR="$HOME/.openclaw/scripts"
LOG_FILE="$SCRIPTS_DIR/apply-patches.log"
LOCK_FILE="${OPENCLAW_APPLY_PATCHES_LOCK:-$SCRIPTS_DIR/.apply-patches.lock}"
GATEWAY_LABEL="${OPENCLAW_GATEWAY_LABEL:-ai.openclaw.gateway}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"

# dm_alert comes from your deployment's lib/dm.sh (Telegram/Slack/whatever).
# Missing file → alerts become log lines, the run still works.
# shellcheck source=/dev/null
source "$SCRIPTS_DIR/lib/dm.sh" 2>/dev/null || dm_alert() { echo "ALERT: $*" >> "$LOG_FILE"; }

# Single-instance lock via atomic mkdir (overlapping triggers otherwise stack
# and fight over the gateway restart). NOTE: keep ONE EXIT trap — a second
# `trap ... EXIT` REPLACES the first and leaks the lockdir.
LOCK_DIR="$LOCK_FILE.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ] && ! kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null; then
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
echo $$ > "$LOCK_DIR/pid"
PATCH_LIST_FILE=""
cleanup() { rm -rf "$LOCK_DIR"; [ -n "$PATCH_LIST_FILE" ] && rm -f "$PATCH_LIST_FILE"; }
trap cleanup EXIT

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log_json() { printf '{"ts":"%s",%s}\n' "$(ts)" "$1" >> "$LOG_FILE"; }

PATCH_LIST_FILE=$(mktemp -t patches.XXXXXX)
ls -1 "$SCRIPTS_DIR"/patch-*.sh 2>/dev/null | sort > "$PATCH_LIST_FILE"

if [ ! -s "$PATCH_LIST_FILE" ]; then
  log_json "\"event\":\"no_patches_found\""
  exit 0
fi

applied_any=0
failed_count=0
failed_list=""
unclear_count=0
unclear_list=""

while IFS= read -r p; do
  name=$(basename "$p")
  if out=$(bash "$p" 2>&1); then
    case "$out" in
      *"already applied"*)
        log_json "\"event\":\"noop\",\"patch\":\"$name\""
        ;;
      *"applied to"*|*"PATCH: applied"*)
        applied_any=1
        log_json "\"event\":\"patched\",\"patch\":\"$name\""
        ;;
      *)
        unclear_count=$((unclear_count + 1))
        unclear_list="${unclear_list}• ${name}
"
        log_json "\"event\":\"unclear_success\",\"patch\":\"$name\",\"out\":\"$(printf '%s' "$out" | head -c 120 | sed 's/"/\\"/g')\""
        ;;
    esac
  else
    rc=$?
    failed_count=$((failed_count + 1))
    failed_list="${failed_list}• ${name}
"
    log_json "\"event\":\"failed\",\"patch\":\"$name\",\"rc\":$rc,\"out\":\"$(printf '%s' "$out" | head -c 200 | sed 's/"/\\"/g')\""
  fi
done < "$PATCH_LIST_FILE"

# Restart BEFORE the failure exit — see header.
if [ "$applied_any" -eq 1 ]; then
  log_json "\"event\":\"restarting_gateway\""
  # Prefer graceful (drains active work) over kickstart (hard SIGTERM drops
  # in-flight replies). Give the drain enough headroom for your slowest
  # agent turn — 120s here.
  restart_ok=0
  if [ -n "$OPENCLAW_BIN" ] && [ -x "$OPENCLAW_BIN" ]; then
    if "$OPENCLAW_BIN" daemon restart --wait 120s >/dev/null 2>&1; then
      restart_ok=1
    fi
  fi
  if [ "$restart_ok" -eq 0 ]; then
    if launchctl kickstart -k "gui/$(id -u)/${GATEWAY_LABEL}" >/dev/null 2>&1; then
      restart_ok=1
    fi
  fi
  if [ "$restart_ok" -eq 1 ]; then
    log_json "\"event\":\"gateway_restarted\""
  else
    log_json "\"event\":\"gateway_restart_failed\""
    dm_alert "⚠️ patch applied but gateway restart failed — restart manually"
    exit 1
  fi
fi

# --- Optional runtime gate assertion ---------------------------------------
GATE_ASSERT="${OPENCLAW_GATE_ASSERT:-0}"
GATE_SENTINEL="${OPENCLAW_GATE_SENTINEL:-}"
GATE_MATCH="${OPENCLAW_GATE_MATCH:-const sendTrackedMessage = async}"
if [ "$GATE_ASSERT" = "1" ] && [ -n "$GATE_SENTINEL" ]; then
gate_issue=""
gate_files=""
# shellcheck disable=SC2086
set -- ${OPENCLAW_GATE_SEARCH_ROOTS:-}
for d in "$@"; do
  [ -d "$d" ] || continue
  while IFS= read -r f; do
    gate_files="${gate_files}${f}
"
    if ! grep -q "$GATE_SENTINEL" "$f" 2>/dev/null; then
      gate_issue="${gate_issue}• sentinel MISSING: ${f}
"
    fi
  done < <(grep -rl "$GATE_MATCH" "$d" 2>/dev/null)
done
if [ -z "$gate_files" ]; then
  gate_issue="${gate_issue}• no dist file matching '${GATE_MATCH}' found at all
"
fi

proc_start_epoch() {
  local pid="$1"
  [ -n "$pid" ] && [ "$pid" != "-" ] || return 1
  ps -p "$pid" -o lstart= 2>/dev/null | xargs -I{} date -j -f '%a %b %d %T %Y' {} +%s 2>/dev/null
}
gw_pid=$(launchctl list 2>/dev/null | awk -v l="$GATEWAY_LABEL" '$3==l{print $1}')
node_pid=$(launchctl list 2>/dev/null | awk '$3=="ai.openclaw.node"{print $1}')
oldest_proc=""
for p in "$gw_pid" "$node_pid"; do
  s=$(proc_start_epoch "$p") || continue
  if [ -z "$oldest_proc" ] || [ "$s" -lt "$oldest_proc" ]; then oldest_proc="$s"; fi
done
if [ -n "$oldest_proc" ] && [ -n "$gate_files" ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    fm=$(stat -f %m "$f" 2>/dev/null || echo 0)
    if [ "$fm" -gt "$oldest_proc" ]; then
      gate_issue="${gate_issue}• patched-but-not-loaded: ${f} is newer than the running gateway/node host — restart needed
"
    fi
  done <<EOF_GATE
$gate_files
EOF_GATE
fi

if [ -n "$gate_issue" ]; then
  log_json "\"event\":\"gate_assert_fail\",\"detail\":\"$(printf '%s' "$gate_issue" | head -c 300 | tr '\n' ';' | sed 's/"/\\"/g')\""
  dm_alert "🚨 outbound-gate runtime assertion FAILED: $gate_issue"
else
  log_json "\"event\":\"gate_assert_ok\""
fi
fi
# ---------------------------------------------------------------------------

if [ "$unclear_count" -gt 0 ]; then
  dm_alert "🟡 patch run: unrecognised output (possible silent no-op after an upstream refactor): $unclear_list"
fi

if [ "$failed_count" -gt 0 ]; then
  dm_alert "⚠️ patch run failed: $failed_list"
  exit 1
fi

checked=$(wc -l < "$PATCH_LIST_FILE" | tr -d ' ')
log_json "\"event\":\"finished\",\"applied\":$applied_any,\"checked\":$checked"
exit 0
