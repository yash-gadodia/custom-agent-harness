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

2026-09-07 — `running` is load-bearing, do not drop it. `failures` omits any
job with a live PID, because its exit status is not knowable mid-run. Treating
that absence as recovery ended the episode, and the next pass saw the same
failure as brand new: kickstart -> escalate -> "didn't stick, needs you", on a
loop, roughly every 30 minutes, for a failure already triaged. MAX_ATTEMPTS and
COOLDOWN_S could not stop it because both are per-episode and the episode kept
being reborn. The watcher polls on the same :00/:15/:30/:45 cadence the jobs run
on, so sampling mid-run is systematic, not a rare race. Labels in `running` are
therefore held: neither healed nor escalated until an exit status is observed.
"""
from __future__ import annotations

ALLOWLIST = frozenset()  # per deployment: "selfheal_allowlist" in harness config
NEVER_ALLOWLIST_SUBSTRINGS = ("gateway",)  # labels that must never be auto-healed

VERIFY_GRACE_S = 10 * 60
COOLDOWN_S = 24 * 60 * 60
MAX_ATTEMPTS = 2


def decide(failures: dict[str, str], state: dict, now: float,
           allowlist=None, never_substrings=None, running=None):
    """Return (kickstarts, recovered, escalations, new_state).

    `running` is the set of labels with a live PID this pass. Their status is
    indeterminate, so they are held in state rather than counted as recovered.
    Defaults to empty, which reproduces the pre-2026-09-07 behaviour.
    """
    allowlist = ALLOWLIST if allowlist is None else frozenset(allowlist)
    never_substrings = (NEVER_ALLOWLIST_SUBSTRINGS if never_substrings is None
                        else tuple(never_substrings))
    running = frozenset() if running is None else frozenset(running)
    kickstarts: list[str] = []
    recovered: list[str] = []
    escalations: list[tuple[str, str]] = []
    new_state: dict = {}

    for label, entry in state.items():
        # `label in running` must be checked as a hold, never as a recovery:
        # a job observed mid-run has told us nothing about how it will exit.
        if (label in failures or label in running
                or now - entry["attempted_at"] < VERIFY_GRACE_S):
            new_state[label] = dict(entry)
        else:
            recovered.append(label)

    for label, status in sorted(failures.items()):
        if label not in allowlist:
            continue
        # Defense in depth: even if a protected-surface label is added to
        # ALLOWLIST by mistake, the denylist must still refuse to touch it.
        low = label.lower()
        if any(sub in low for sub in never_substrings):
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
