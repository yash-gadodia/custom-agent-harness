"""Route LaunchAgent watcher events to either an immediate the assistant DM or a
batched queue that the 08:00 self-improve digest flushes once a day.

Why: a single transient mid-edit failure used to fan out into 3-4 real-time
DMs (failure -> kickstart -> verify -> recovered) for a non-event that healed
itself. Customer-facing failures and selfheal escalations must still interrupt
immediately; everything that auto-recovers or is a known-idempotent job can
wait for the morning read.

route_launchd_events() is pure (no IO) so it is unit-testable in isolation.
The selfheal ALLOWLIST is the discriminator: allowlisted labels are, by
definition, idempotent and non-customer-facing, so their raw failures /
kickstarts / recoveries are batchable. Anything not on the allowlist may be
customer-facing, so it stays immediate. Escalations (selfheal gave up) and
failed kickstarts are always immediate regardless.
"""
from __future__ import annotations

import json
import os


def route_launchd_events(new, kicks, kick_failed, escalations, healed, allowlist):
    """Pure router.

    Args:
      new:         dict {label: status} of newly-detected raw failures.
      kicks:       list of labels selfheal kickstarted this pass.
      kick_failed: set of labels whose kickstart command itself failed.
      escalations: list of (label, status) selfheal gave up on.
      healed:      list of labels selfheal verified recovered.
      allowlist:   frozenset of idempotent, non-customer-facing labels.

    Returns dict with:
      immediate_lines / batched_lines : HTML-formatted strings to render.
      immediate_labels / batched_labels : {label: status} subsets of `new`,
        for the watcher's dedup bookkeeping (only mark a raw failure handled
        once it has actually been emitted somewhere).
    """
    immediate_lines: list[str] = []
    batched_lines: list[str] = []
    immediate_labels: dict[str, str] = {}
    batched_labels: dict[str, str] = {}

    for label, status in sorted(new.items()):
        line = f"• <code>{label}</code> exit {status}"
        if label in allowlist:
            batched_lines.append(line)
            batched_labels[label] = status
        else:
            immediate_lines.append(line)
            immediate_labels[label] = status

    for label in kicks:
        if label in kick_failed:
            immediate_lines.append(
                f"🩹 selfheal: kickstart FAILED for <code>{label}</code>, needs you")
        else:
            batched_lines.append(
                f"🩹 selfheal: kickstarted <code>{label}</code>, verifying next pass")

    for label, status in escalations:
        immediate_lines.append(
            f"❌ selfheal didn't stick: <code>{label}</code> still exit {status}, needs you")

    for label in healed:
        batched_lines.append(f"✅ selfheal verified: <code>{label}</code> recovered")

    return {
        "immediate_lines": immediate_lines,
        "batched_lines": batched_lines,
        "immediate_labels": immediate_labels,
        "batched_labels": batched_labels,
    }


def enqueue(path, lines, ts):
    """Append lines to the batch queue as JSONL. Best-effort, append-only."""
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps({"ts": ts, "text": line}) + "\n")


def drain(path):
    """Atomically take everything queued so far and return the texts.

    Renames the file first so any enqueue() racing the drain lands in a fresh
    file and is not lost. Missing/empty queue returns []. Malformed lines are
    skipped, not fatal.
    """
    if not os.path.exists(path):
        return []
    tmp = path + ".draining"
    os.rename(path, tmp)
    out = []
    try:
        with open(tmp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln)["text"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
    finally:
        os.remove(tmp)
    return out
