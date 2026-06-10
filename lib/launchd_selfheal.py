"""Tier-1 selfheal for failed LaunchAgents: kickstart once, verify, escalate.

decide() is pure logic — the cron-failure-watcher wrapper owns launchctl
execution, state persistence, and Telegram delivery. Only ALLOWLIST jobs are
ever auto-remediated: idempotent, non-customer-facing scripts that gate their
own side effects (blog publishers are cadence-gated; backup and apply-patches
re-run safely). Gateway restarts and anything that can message customers or
book real-world resources stay manual by design.

Lifecycle per failure episode:
  fail -> kickstart (attempt 1) -> verdict next pass after VERIFY_GRACE_S
       -> recovered (state cleared) | escalate once, then at most one retry
          after COOLDOWN_S; MAX_ATTEMPTS total, after which it waits for a
          human. Recovery ends the episode, so a later failure starts fresh.
"""
from __future__ import annotations

ALLOWLIST = frozenset()  # per deployment: "selfheal_allowlist" in harness config
NEVER_ALLOWLIST_SUBSTRINGS = ("gateway",)  # labels that must never be auto-healed

VERIFY_GRACE_S = 10 * 60
COOLDOWN_S = 24 * 60 * 60
MAX_ATTEMPTS = 2


def decide(failures: dict[str, str], state: dict, now: float, allowlist=None):
    """Return (kickstarts, recovered, escalations, new_state)."""
    allowlist = ALLOWLIST if allowlist is None else frozenset(allowlist)
    kickstarts: list[str] = []
    recovered: list[str] = []
    escalations: list[tuple[str, str]] = []
    new_state: dict = {}

    for label, entry in state.items():
        if label in failures or now - entry["attempted_at"] < VERIFY_GRACE_S:
            new_state[label] = dict(entry)
        else:
            recovered.append(label)

    for label, status in sorted(failures.items()):
        if label not in allowlist:
            continue
        entry = new_state.get(label)
        if entry is None:
            kickstarts.append(label)
            new_state[label] = {"attempted_at": now, "status": status,
                                "attempts": 1, "escalated": False}
        elif not entry["escalated"]:
            if now - entry["attempted_at"] >= VERIFY_GRACE_S:
                escalations.append((label, status))
                entry["escalated"] = True
        elif entry["attempts"] < MAX_ATTEMPTS and now - entry["attempted_at"] >= COOLDOWN_S:
            kickstarts.append(label)
            new_state[label] = {"attempted_at": now, "status": status,
                                "attempts": entry["attempts"] + 1, "escalated": False}

    return kickstarts, recovered, escalations, new_state
