#!/bin/bash
# install-net-selfheal.sh — apply the SSH-resilience hardening (2026-06-01 incident).
# Idempotent. Run with sudo:
#   sudo bash /home/user/.openclaw/scripts/install-net-selfheal.sh
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "ERROR: run with sudo"; exit 1; }

SCRIPTS=/home/user/.openclaw/scripts
PLIST_SRC="$SCRIPTS/com.example.net-selfheal.plist"
PLIST_DST=/Library/LaunchDaemons/com.example.net-selfheal.plist

echo "== 1/4 DNS — pin to public resolvers (off the flaky router 192.168.88.1) =="
networksetup -setdnsservers "Wi-Fi" 1.1.1.1 8.8.8.8 1.0.0.1
networksetup -setdnsservers "Ethernet" 1.1.1.1 8.8.8.8 1.0.0.1 2>/dev/null || true
dscacheutil -flushcache; killall -HUP mDNSResponder 2>/dev/null || true
echo "   Wi-Fi DNS now: $(networksetup -getdnsservers Wi-Fi | tr '\n' ' ')"

echo "== 2/4 net-selfheal LaunchDaemon =="
chown root:wheel "$SCRIPTS/net-selfheal.sh"; chmod 755 "$SCRIPTS/net-selfheal.sh"
cp "$PLIST_SRC" "$PLIST_DST"; chown root:wheel "$PLIST_DST"; chmod 644 "$PLIST_DST"
launchctl bootout system "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap system "$PLIST_DST"
launchctl enable system/com.example.net-selfheal
launchctl print system/com.example.net-selfheal >/dev/null 2>&1 \
  && echo "   loaded: yes" || echo "   loaded: NO (check syntax)"

echo "== 3/4 power — auto-restart on power loss / freeze =="
pmset -a autorestart 1 || true

echo "== 4/4 first run + health =="
NSH_LOG=/var/log/net-selfheal.log "$SCRIPTS/net-selfheal.sh" || true
echo "   $(tail -1 /var/log/net-selfheal.log 2>/dev/null || echo '(healthy — no log line is normal)')"
echo "DONE. Verify DNS survives router outage:  nslookup cloudflare.com   (should resolve via 1.1.1.1)"
