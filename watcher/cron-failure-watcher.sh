#!/usr/bin/env bash
# cron-failure-watcher: DMs you on Telegram when any watched LaunchAgent
# exits non-zero, and auto-heals allowlisted idempotent jobs (kickstart once,
# verify next pass, escalate if it did not stick).
#
# Config: $HARNESS_CONFIG or ~/.config/openclaw-harness/config.json
# (see harness.config.example.json). Schedule every 15 min via the plist
# template in this directory. State + log live in config.state_dir, so a
# single failure fires exactly one DM (no spam, no missed events on retry).
set -euo pipefail

CONFIG="${HARNESS_CONFIG:-$HOME/.config/openclaw-harness/config.json}"
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 1; }

HARNESS_LIB="$(cd "$(dirname "$0")/../lib" && pwd)"
LAUNCHD_LIST=$(launchctl list 2>/dev/null || true)
export CONFIG HARNESS_LIB LAUNCHD_LIST

python3 <<'PY'
import json, os, subprocess, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.environ["HARNESS_LIB"])
from launchagent_failures import parse_launchctl_list
from launchd_selfheal import decide

cfg = json.load(open(os.environ["CONFIG"]))
state_dir = os.path.expanduser(cfg.get("state_dir", "~/.config/openclaw-harness"))
os.makedirs(state_dir, exist_ok=True)
state_file = os.path.join(state_dir, "watcher-state.json")
selfheal_file = os.path.join(state_dir, "selfheal-state.json")
log_file = os.path.join(state_dir, "watcher.log")

tg = cfg["telegram"]
token = os.environ.get(tg.get("bot_token_env", ""), "")
if not token and tg.get("bot_token_file"):
    try:
        token = open(os.path.expanduser(tg["bot_token_file"])).read().strip()
    except FileNotFoundError:
        pass
if not token:
    sys.exit("no telegram bot token: set telegram.bot_token_env or telegram.bot_token_file")
chat_id = str(tg["chat_id"])

prefixes = tuple(cfg.get("watch_prefixes", []))
allowlist = frozenset(cfg.get("selfheal_allowlist", []))

def logmsg(s):
    with open(log_file, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {s}\n")

def send_dm(text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

def load(path):
    try:
        return json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

failures = dict(parse_launchctl_list(os.environ.get("LAUNCHD_LIST", ""), prefixes=prefixes))
state = load(state_file)
for label in [l for l in state if l not in failures]:
    state.pop(label)
new = {l: s for l, s in failures.items() if state.get(l) != s}

sh_state = load(selfheal_file)
kicks, healed, escalations, sh_state = decide(failures, sh_state, time.time(), allowlist=allowlist)
kick_failed = set()
for label in kicks:
    r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        kick_failed.add(label)
        logmsg(f"selfheal kickstart FAILED for {label}: {(r.stderr or r.stdout).strip()}")
json.dump(sh_state, open(selfheal_file, "w"), indent=2)

lines = [f"• <code>{l}</code> exit {s}" for l, s in sorted(new.items())]
for l in kicks:
    lines.append(f"🩹 selfheal: kickstarted <code>{l}</code>"
                 + (", kickstart FAILED" if l in kick_failed else ", verifying next pass"))
for l, s in escalations:
    lines.append(f"❌ selfheal didn't stick: <code>{l}</code> still exit {s}, needs you")
for l in healed:
    lines.append(f"✅ selfheal verified: <code>{l}</code> recovered")
if lines:
    title = "⚠️ <b>LaunchAgent failure</b>" if (new or escalations) else "🩹 <b>LaunchAgent selfheal</b>"
    try:
        resp = send_dm(title + "\n" + "\n".join(lines))
        if resp.get("ok"):
            state.update(new)
            logmsg(f"alert sent: new={sorted(new)} kicks={kicks} healed={healed} escalated={[l for l, _ in escalations]}")
        else:
            logmsg(f"alert send failed: {resp}")
    except Exception as e:
        logmsg(f"alert exception: {e}")
json.dump(state, open(state_file, "w"), indent=2)
logmsg(f"scan done: failing={len(failures)} new={len(new)} kicks={len(kicks)} healed={len(healed)}")
PY
