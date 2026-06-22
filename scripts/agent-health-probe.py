#!/usr/bin/env python3
"""Synthetic agent health probe with cooldown-gated self-heal.

Runs frequently (e.g. every 30 min). Sends each load-bearing agent a tiny
"reply PROBE_OK" message through the runtime CLI. If the load-bearing agents
stay silent for N consecutive runs, restarts the runtime once (cooldown-gated),
re-probes, and only then escalates to a human. Decision + state logic lives in
lib/agent_selfheal.py so it is unit-tested and reusable.

Config (edit for your deployment):
  AGENTS, RUNTIME_CLI, RESTART_CMD, STATE_FILE, and how alert() notifies you.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agent_selfheal import decide_self_heal, load_state, save_state  # noqa: E402

AGENTS = ["agent-1", "agent-2"]              # your load-bearing agents
RUNTIME_CLI = ["agent-runtime", "invoke"]    # how to send an agent a message
RESTART_CMD = ["agent-runtime", "restart"]   # how to restart the runtime
STATE_FILE = Path.home() / ".cache" / "agent-health-probe.json"
PROBE = "Reply with the literal text PROBE_OK and nothing else."
TIMEOUT = 90


def probe(agent):
    try:
        r = subprocess.run(RUNTIME_CLI + ["--agent", agent, "-m", PROBE],
                           capture_output=True, text=True, timeout=TIMEOUT)
        return r.returncode == 0 and "PROBE_OK" in (r.stdout or "")
    except Exception:
        return False


def alert(msg):
    print(f"ALERT: {msg}", file=sys.stderr)   # wire to Telegram/Slack/email


def main():
    failed = [a for a in AGENTS if not probe(a)]
    st = load_state(STATE_FILE)
    if not failed:
        save_state(0, st.get("last_restart_at"), STATE_FILE)
        return 0

    streak = st.get("streak", 0) + 1
    last = st.get("last_restart_at")
    do_restart, reason = decide_self_heal(streak, last, time.time())
    print(f"silent: {failed} | {reason}")
    if do_restart:
        subprocess.run(RESTART_CMD, timeout=120)
        time.sleep(20)
        still = [a for a in failed if not probe(a)]
        last = time.time()
        if not still:
            alert(f"auto-healed: {failed} recovered after restart")
            streak = 0
        else:
            alert(f"auto-restart did not fix {still} - needs a human")
    else:
        alert(f"agents silent (streak {streak}): {failed}")
    save_state(streak, last, STATE_FILE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
