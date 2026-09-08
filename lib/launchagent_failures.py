"""Parse `launchctl list` output for failed LaunchAgents worth alerting on.

A job is alert-worthy when it is not currently running (PID column "-"),
its last exit status is non-zero, and its label matches a watched prefix.
The header row and malformed lines are ignored. Pure logic — the
cron-failure-watcher wrapper owns state, dedup, and Telegram delivery.

`parse_launchctl_running` reports the complement that matters: labels with a
live PID, whose exit status is therefore not yet knowable. Absence from
`parse_launchctl_list` means "not observed failing", which is NOT the same as
"succeeded" — see the 2026-09-07 note in launchd_selfheal.decide().
"""
from __future__ import annotations

WATCH_PREFIXES = ()  # per deployment: "watch_prefixes" in harness config


def parse_launchctl_list(text: str,
                         prefixes: tuple[str, ...] = WATCH_PREFIXES) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = (p.strip() for p in parts)
        if pid != "-" or status in ("0", "-", ""):
            continue
        if not label.startswith(prefixes):
            continue
        try:
            int(status)
        except ValueError:
            continue
        failures.append((label, status))
    return failures


def parse_launchctl_running(text: str,
                            prefixes: tuple[str, ...] = WATCH_PREFIXES) -> set[str]:
    """Labels with a live PID — their exit status is indeterminate this pass.

    The watcher polls on a fixed cadence that collides with the jobs' own
    schedules, so sampling a job mid-run is routine, not rare.
    """
    running: set[str] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, _status, label = (p.strip() for p in parts)
        if not label.startswith(prefixes):
            continue
        try:
            int(pid)
        except ValueError:
            continue
        running.add(label)
    return running
