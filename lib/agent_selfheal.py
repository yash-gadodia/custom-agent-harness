"""Self-heal decision core for an agent fleet — generic, no deployment coupling.

A frequent health probe pings each agent; when the load-bearing ones stay
unresponsive for several consecutive runs, the supervisor restarts the runtime
once (cooldown-gated so a problem a restart can't fix can't cause a restart
storm), re-probes, and only then escalates to a human. This module holds the
pure decision + state logic so it can be unit-tested and reused; the I/O (how
you probe, how you restart, how you alert) is the caller's.
"""
from __future__ import annotations

import json
from pathlib import Path

# Defaults; override per deployment.
MIN_FAIL_STREAK = 2
RESTART_COOLDOWN_SEC = 3 * 3600


def decide_self_heal(streak, last_restart_at, now,
                     min_streak=MIN_FAIL_STREAK,
                     cooldown_sec=RESTART_COOLDOWN_SEC):
    """Pure decision: should the supervisor restart the runtime this run?

    streak           consecutive failed probe runs, including the current one
    last_restart_at  epoch seconds of the previous auto-restart, or None
    now              current epoch seconds
    Returns (should_restart: bool, reason: str).
    """
    if streak < min_streak:
        return False, f"streak {streak} < {min_streak} — alert-only, no restart yet"
    if last_restart_at is not None and (now - last_restart_at) < cooldown_sec:
        left_min = int((cooldown_sec - (now - last_restart_at)) // 60)
        return False, f"restart cooldown active (~{left_min}min left) — alert-only"
    return True, f"streak {streak} >= {min_streak} and cooldown clear — restarting runtime"


def load_state(path):
    """Read {streak, last_restart_at}; defaults on missing/corrupt file."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {"streak": 0, "last_restart_at": None}


def save_state(streak, last_restart_at, path):
    """Atomically persist {streak, last_restart_at}."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"streak": streak, "last_restart_at": last_restart_at}))
    tmp.replace(p)
