#!/bin/bash
# net-selfheal.sh — keep the mini reachable from outside (SSH via cloudflared).
#
# Guards against the 2026-06-01 incident: the box stayed powered ON but its
# WiFi/LAN + router-DNS path died for ~3 days. cloudflared (the ONLY exposed SSH
# path) and OpenClaw both need the internet, so both went dark — and there was
# no way back in until a physical reboot. The existing cloudflared-watchdog only
# restarts cloudflared, which is useless when the network itself is dead.
#
# Every 180s this checks REAL reachability and escalates only after a SUSTAINED
# outage. Cheap/safe fixes first; reboot is the last resort and only after the
# box has been fully unreachable for ~21 min (by which point a clean reboot is
# strictly better than staying dark). Runs as root via com.example.net-selfheal.
#
# DISABLE auto-reboot: set REBOOT_AT=0 (env or edit). Tune thresholds below.

set -uo pipefail

GW="${NSH_GW:-192.168.88.1}"            # LAN gateway
WIFI_IF="${NSH_WIFI_IF:-en1}"           # active WiFi interface
PROBE_HOST="${NSH_PROBE_HOST:-cloudflare.com}"  # resolved via SYSTEM resolver (the path that failed)
PROBE_IP="${NSH_PROBE_IP:-1.1.1.1}"     # internet egress probe (no DNS needed)

# Escalation ladder — consecutive 180s cycles of total unreachability:
FLUSH_AT="${NSH_FLUSH_AT:-2}"           #  ~6 min  -> flush DNS cache
BOUNCE_AT="${NSH_BOUNCE_AT:-3}"         #  ~9 min  -> re-associate WiFi
DHCP_AT="${NSH_DHCP_AT:-5}"             # ~15 min  -> renew DHCP lease
REBOOT_AT="${NSH_REBOOT_AT:-7}"         # ~21 min  -> reboot (last resort; 0 disables)

STATE="${NSH_STATE:-/var/run/net-selfheal.fails}"        # fail counter (cleared on boot — intended)
BREADCRUMB="${NSH_BREADCRUMB:-/var/log/net-selfheal.breadcrumb}"  # survives reboot, for recovery DM
LOG="${NSH_LOG:-/var/log/net-selfheal.log}"
OPENCLAW_CFG="${NSH_OPENCLAW_CFG:-/home/user/.openclaw/openclaw.json}"
ALERT_CHAT_ID="000000000"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $1" >> "$LOG" 2>/dev/null; }

# Reuse the proven token-resolver + DM helper from laser-chase.sh (handles the
# secrets.json dict-ref form of the Telegram bot token).
resolve_tg_token() {
  OPENCLAW_CFG="$OPENCLAW_CFG" ACCT="$1" python3 <<'PYEOF'
import json, os, sys
try:
    cfg = json.load(open(os.environ['OPENCLAW_CFG']))
    v = cfg['channels']['telegram']['accounts'][os.environ['ACCT']]['botToken']
    if isinstance(v, dict):
        sp = cfg['secrets']['providers'][v['provider']]['path']
        v = json.load(open(sp))[v['id'].lstrip('/')]
    print(v, end='')
except Exception as e:
    sys.stderr.write(f'token-resolve failed: {e}\n')
    sys.exit(1)
PYEOF
}

dm_alert() {
  local text="$1" token
  token=$(resolve_tg_token the worker agent) || return 1
  [ -n "$token" ] || return 1
  curl -sS --max-time 10 \
    "https://api.telegram.org/bot${token}/sendMessage" \
    -d "chat_id=${ALERT_CHAT_ID}" \
    --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

lan_ok()      { ping -c1 -t2 "$GW" >/dev/null 2>&1; }
internet_ok() { ping -c1 -t2 "$PROBE_IP" >/dev/null 2>&1; }
dns_ok()      {
  # Pass if the SYSTEM resolver works (the path cloudflared uses) OR a direct
  # public query works — so a flaky local DNS proxy alone never triggers a
  # reboot while egress is fine. Only DNS-dead-on-both-paths counts as failure.
  dscacheutil -q host -a name "$PROBE_HOST" 2>/dev/null | grep -q "ip_address" && return 0
  nslookup -timeout=2 "$PROBE_HOST" "$PROBE_IP" >/dev/null 2>&1
}
# Healthy = the two things cloudflared + OpenClaw actually need to be reachable.
healthy()     { internet_ok && dns_ok; }

flush_breadcrumb() {
  [ -s "$BREADCRUMB" ] || return 0
  local msg; msg=$(cat "$BREADCRUMB" 2>/dev/null)
  dm_alert "🛜 net-selfheal @ $(hostname -s): ${msg}" && : > "$BREADCRUMB"
}

uptime_secs() {
  local b; b=$(sysctl -n kern.boottime 2>/dev/null | grep -oE 'sec = [0-9]+' | head -1 | grep -oE '[0-9]+')
  [ -n "$b" ] && echo $(( $(date +%s) - b )) || echo 999999
}

fails=$(cat "$STATE" 2>/dev/null || echo 0)
case "$fails" in (*[!0-9]*) fails=0 ;; esac

if healthy; then
  if [ "$fails" -ge "$FLUSH_AT" ]; then
    log "RECOVERED after $fails unhealthy cycles (~$((fails*3))m)"
    echo "network recovered $(ts) after ~$((fails*3))m down" >> "$BREADCRUMB"
  fi
  echo 0 > "$STATE" 2>/dev/null
  flush_breadcrumb
  exit 0
fi

fails=$((fails+1)); echo "$fails" > "$STATE" 2>/dev/null
log "UNHEALTHY consecutive=$fails (lan=$(lan_ok && echo 1 || echo 0) inet=$(internet_ok && echo 1 || echo 0) dns=$(dns_ok && echo 1 || echo 0))"

if [ "$fails" -eq "$FLUSH_AT" ]; then
  log "escalate#1: flush DNS cache"
  dscacheutil -flushcache 2>/dev/null; killall -HUP mDNSResponder 2>/dev/null || true
elif [ "$fails" -eq "$BOUNCE_AT" ]; then
  log "escalate#2: bounce WiFi $WIFI_IF"
  networksetup -setairportpower "$WIFI_IF" off 2>/dev/null || true; sleep 5
  networksetup -setairportpower "$WIFI_IF" on 2>/dev/null || true
elif [ "$fails" -eq "$DHCP_AT" ]; then
  log "escalate#3: renew DHCP on $WIFI_IF"
  ipconfig set "$WIFI_IF" DHCP 2>/dev/null || true
  dscacheutil -flushcache 2>/dev/null; killall -HUP mDNSResponder 2>/dev/null || true
elif [ "$REBOOT_AT" -gt 0 ] && [ "$fails" -ge "$REBOOT_AT" ]; then
  up=$(uptime_secs)
  if [ "$up" -lt 1500 ]; then
    log "escalate#4: reboot SUPPRESSED (uptime ${up}s < 1500s — avoid boot-race/loop)"
  else
    log "escalate#4: REBOOT — unreachable ~$((fails*3))m, escalations exhausted"
    echo "auto-rebooted $(ts) — network unreachable ~$((fails*3))m, escalations exhausted" >> "$BREADCRUMB"
    sync
    /sbin/shutdown -r now "net-selfheal: network unreachable" 2>/dev/null
  fi
fi
exit 0
