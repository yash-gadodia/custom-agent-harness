#!/bin/bash
# OpenClaw channel self-heal — every 5min.
# Closes the gap the gateway watchdog leaves: when the gateway restarts, the
# long-lived node host (Telegram + WhatsApp connections) can fail to reconnect
# and crash-loop ECONNREFUSED, taking the worker agent + the customer agent silent with no alert.
# Detects it via `openclaw health --json` channels.*.connected and kickstarts
# `ai.openclaw.node` (idempotent). Also alerts on claude-cli token states that
# take every agent down (missing / expired-no-refresh / no-refresh-token).
# Decision logic lives in lib/channel_selfheal.py (unit-tested); this wrapper
# owns launchctl + Telegram + state.
set -uo pipefail

LIB="$HOME/.openclaw/scripts/lib"
LOG="$HOME/.openclaw/scripts/openclaw-channel-selfheal.log"
STATE="$HOME/.openclaw/scripts/openclaw-channel-selfheal.state.json"
NODE_LABEL="gui/$(id -u)/ai.openclaw.node"
KEYCHAIN_SVC="Claude Code-credentials-a1ec9f0a"
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

source "$HOME/.openclaw/scripts/lib/dm.sh"

health=$(openclaw health --json 2>/dev/null)
token=$(security find-generic-password -s "$KEYCHAIN_SVC" -a "$USER" -w 2>/dev/null)
now_ms=$(python3 -c "import time;print(int(time.time()*1000))")

# Single python pass: load state, run both decisions, emit an action line.
action=$(HEALTH="$health" TOKEN="$token" NOW_MS="$now_ms" STATE="$STATE" LIB="$LIB" python3 <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LIB"])
import channel_selfheal as cs

def loads(s):
    try: return json.loads(s) if s and s.strip() else None
    except Exception: return None

health = loads(os.environ.get("HEALTH", ""))
now_ms = int(os.environ["NOW_MS"]); now = now_ms / 1000.0

state = {}
try:
    with open(os.environ["STATE"]) as f: state = json.load(f)
except Exception: pass
streak = state.get("streak", 0)
last_kick = state.get("last_kick_at")           # epoch seconds or None
last_auth = state.get("last_auth_level")        # dedup auth alerts

# --- claude-cli token state ---
tok = loads(os.environ.get("TOKEN", ""))
tinfo = {"present": False}
if tok:
    o = tok.get("claudeAiOauth", {}) or {}
    tinfo = {"present": True, "expires_at_ms": o.get("expiresAt"),
             "has_refresh": bool(o.get("refreshToken"))}
auth_level, auth_detail = cs.auth_decision(tinfo, now_ms)

# --- channel / node-host state ---
if health is None:
    # gateway unreachable: don't kick the node host (gateway watchdog owns that),
    # just carry streak so we react the moment health returns.
    degraded, kick, down, new_streak, new_last_kick = True, False, ["gateway-unreachable"], streak + 1, last_kick
else:
    degraded, kick, down, new_streak, new_last_kick = cs.channel_decision(health, streak, last_kick, now)

new_state = {"ts": now_ms, "streak": new_streak, "last_kick_at": new_last_kick,
             "last_auth_level": auth_level}
d = os.path.dirname(os.environ["STATE"])
import tempfile
f = tempfile.NamedTemporaryFile("w", dir=d, prefix=".chsh-", suffix=".tmp", delete=False)
json.dump(new_state, f); f.close(); os.replace(f.name, os.environ["STATE"]); os.chmod(os.environ["STATE"], 0o600)

print(json.dumps({
    "degraded": degraded, "kick": kick, "down": down, "streak": new_streak,
    "auth_level": auth_level, "auth_detail": auth_detail,
    "auth_changed": auth_level != last_auth,
}))
PY
)

[ -z "$action" ] && { log "no action (python failed)"; exit 0; }

degraded=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('degraded'))" "$action" 2>/dev/null)
kick=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('kick'))" "$action" 2>/dev/null)
down=$(python3 -c "import json,sys;print(', '.join(json.loads(sys.argv[1]).get('down') or []))" "$action" 2>/dev/null)
streak=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('streak'))" "$action" 2>/dev/null)
auth_level=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('auth_level') or '')" "$action" 2>/dev/null)
auth_detail=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('auth_detail') or '')" "$action" 2>/dev/null)
auth_changed=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('auth_changed'))" "$action" 2>/dev/null)

if [ "$degraded" = "True" ]; then log "channels degraded (streak=$streak): $down"; fi

if [ "$kick" = "True" ]; then
  log "kickstarting node host ($NODE_LABEL) — channels down: $down"
  launchctl kickstart -k "$NODE_LABEL" >/dev/null 2>&1
  dm_alert "🔧 OpenClaw node host wedged — channels down: ${down}. Auto-kicked \`ai.openclaw.node\` to reconnect the worker agent + the customer agent. Watching for recovery."
fi

# Auth alerts: only when the level changes (avoid 5-min spam), and only for real outage states.
if [ -n "$auth_level" ] && [ "$auth_changed" = "True" ]; then
  log "auth alert ($auth_level): $auth_detail"
  dm_alert "🔑 claude-cli auth: ${auth_detail}. Re-auth: \`CLAUDE_CONFIG_DIR=\$HOME/.claude-openclaw claude /login\` then \`openclaw daemon restart\`."
fi

exit 0
