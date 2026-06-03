#!/bin/bash
# OpenClaw gateway watchdog.
# Runs from a system LaunchDaemon (UserName=user) so it fires at boot even when
# nobody has logged into the GUI. The normal gateway LaunchAgent lives in the
# GUI/Aqua domain and only loads after a GUI login; when a reboot does NOT
# auto-login (e.g. 2026-06-02: /etc/kcpassword missing) the LaunchAgent never
# starts and the whole gateway is down. This watchdog closes that gap: if the
# gateway port is not listening, it starts the gateway directly.
#
# Auth note: with no GUI login the login keychain is locked, so the claude CLI
# cannot read its keychain-stored OAuth creds. OpenClaw is pointed at a
# file-based creds store at ~/.claude-openclaw/.credentials.json (has a refresh
# token, so it self-renews on use) which works headlessly.

export HOME=/home/user
PORT=18789
LOG="$HOME/.openclaw/logs/gateway-watchdog.log"
WRAP="$HOME/.openclaw/service-env/ai.openclaw.gateway-env-wrapper.sh"
ENVF="$HOME/.openclaw/service-env/ai.openclaw.gateway.env"
NODE="/opt/homebrew/opt/node/bin/node"
ENTRY="/opt/homebrew/lib/node_modules/openclaw/dist/index.js"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# Already listening? Nothing to do (LaunchAgent or a prior tick already started it).
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  exit 0
fi

echo "[$(ts)] gateway not listening on :$PORT — starting" >> "$LOG"
cd "$HOME/.openclaw" || exit 1

nohup "$WRAP" "$ENVF" "$NODE" "$ENTRY" gateway --port "$PORT" \
  >> "$HOME/.openclaw/logs/gateway.log" \
  2>> "$HOME/.openclaw/logs/gateway.err.log" < /dev/null &
disown 2>/dev/null || true
echo "[$(ts)] start dispatched (pid $!)" >> "$LOG"
