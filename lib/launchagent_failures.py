"""Parse `launchctl list` output for failed LaunchAgents worth alerting on.

A job is alert-worthy when it is not currently running (PID column "-"),
its last exit status is non-zero, and its label matches a watched prefix.
The header row and malformed lines are ignored. Pure logic — the
cron-failure-watcher wrapper owns state, dedup, and Telegram delivery.
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
