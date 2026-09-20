"""Liveness supervision for always-on LaunchAgents.

Distinct from launchd_selfheal, and deliberately so. That module remediates
*crons that exited non-zero*, where re-running can repeat a side effect — hence
its conservative ALLOWLIST. This module supervises *daemons that are supposed
to be resident*, where the failure is simply "no process". Restarting one does
not replay anything; it resumes the service.

Why it exists: on some macOS builds gui/<uid> honours neither StartInterval
nor KeepAlive. Two KeepAlive=true LaunchAgents were therefore found dead on
2026-09-20 with `state = not running` and `runs = 3` — an internal queue
worker had been down for a month, a message-relay bridge for six days. Nothing
noticed, because both still look "installed" to launchctl, and a daemon that
exits cleanly leaves exit status 0, so an exit-status watcher never flags it.
Supervision has to come from the system domain, so this is driven by the
gateway watchdog, which a system LaunchDaemon already runs every 120s.

Design notes:
  - Two consecutive misses before acting, so sampling a daemon during its own
    restart does not cause a restart storm (same reasoning as the gateway's
    FAIL_STREAK_TO_RESTART).
  - A cooldown and an attempt cap, so a crash-looping daemon escalates to a
    human once instead of being hammered every tick.
  - Observing a live PID ends the episode: counters reset, so a later failure
    starts fresh rather than inheriting a spent attempt budget.

Pure decision logic only — no launchctl, no network, no clock. The caller
supplies `now` and performs the side effects, which is what makes this
testable.
"""
from __future__ import annotations

ALWAYS_ON = frozenset()  # per deployment: "always_on_labels" in harness config

MISSES_BEFORE_RESTART = 2
RESTART_COOLDOWN_S = 300
MAX_RESTARTS = 3


def parse_pids(listing: str) -> dict[str, str]:
    """{label: pid} for every line of `launchctl list` output.

    pid is the raw field: a digit string when running, "-" when not.
    """
    pids: dict[str, str] = {}
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, _status, label = (p.strip() for p in parts)
        if label:
            pids[label] = pid
    return pids


def down_labels(listing: str, always_on=ALWAYS_ON) -> set[str]:
    """Supervised labels with no live process.

    A label missing from the listing entirely counts as down: that is what an
    unloaded job looks like, and an unloaded always-on job is exactly the
    silent failure this module exists to catch.
    """
    pids = parse_pids(listing)
    down = set()
    for label in always_on:
        pid = pids.get(label)
        if pid is None or pid == "-" or not pid.lstrip("-").isdigit():
            down.add(label)
        elif int(pid) <= 0:
            down.add(label)
    return down


def decide(listing: str, state: dict, now: float,
           always_on=ALWAYS_ON) -> tuple[list[str], list[str], dict]:
    """(to_kickstart, to_escalate, new_state).

    `to_escalate` is emitted at most once per episode, when the attempt budget
    is spent — a daemon that will not stay up is a human problem, and repeating
    the alert every 120s would be the noise this whole exercise is removing.
    """
    state = {k: dict(v) for k, v in state.items() if isinstance(v, dict)}
    down = down_labels(listing, always_on)
    kick: list[str] = []
    escalate: list[str] = []

    for label in sorted(always_on):
        entry = state.get(label, {})
        if label not in down:
            # Live PID observed: episode over.
            if entry:
                state.pop(label, None)
            continue

        misses = int(entry.get("misses", 0)) + 1
        restarts = int(entry.get("restarts", 0))
        last = float(entry.get("last_restart_at", 0) or 0)
        entry["misses"] = misses
        entry["restarts"] = restarts
        entry["last_restart_at"] = last
        entry.setdefault("down_since", now)

        if misses < MISSES_BEFORE_RESTART:
            state[label] = entry
            continue
        if restarts >= MAX_RESTARTS:
            if not entry.get("alerted"):
                entry["alerted"] = True
                escalate.append(label)
            state[label] = entry
            continue
        if now - last < RESTART_COOLDOWN_S:
            state[label] = entry
            continue

        kick.append(label)
        entry["restarts"] = restarts + 1
        entry["last_restart_at"] = now
        state[label] = entry

    return kick, escalate, state
