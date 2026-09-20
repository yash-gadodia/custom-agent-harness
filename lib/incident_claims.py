"""Incident claims: stop two reports of one failure becoming two alerts.

A job that detects its own failure sends a specific, useful message ("failed
after 5 attempts: OAuth session expired"). A generic launchd watcher then
notices the same non-zero exit and sends a content-free one ("<label> exit 1").
Same incident, two pings, and the second one says strictly less than the first.

Fingerprint dedup cannot catch that pairing: different job keys, different
text, one event. So the detailed reporter *claims* the incident by launchd
label, and the generic watcher drops any line whose label is already claimed.
Claims expire after CLAIM_TTL_S, so a stale claim can never mute a later,
genuinely new failure.

Everything here is pure: state goes in as a dict, a new dict comes out, and the
caller owns persistence. That keeps the atomic-write behaviour with whichever
module owns the state file, and makes the policy testable without touching
disk.

Fail-open is the rule throughout. An unreadable or malformed claim store yields
"not claimed", so the worst case is a duplicate alert, never a silent one.
"""
from __future__ import annotations

import re

CLAIM_TTL_S = 2 * 3600

# Launchd label prefixes that can appear in an alert line. Reverse-DNS, so this
# stays deployment-agnostic.
_LABEL_PREFIXES = ("com.", "ai.", "org.", "net.", "io.", "dev.")

_TAG_RE = re.compile(r"<[^>]+>")


def as_dict(v):
    """Coerce anything that is not a usable mapping to {}.

    State on disk can be truncated or hand-edited; a malformed blob must
    degrade to "no claims" rather than raise.
    """
    return v if isinstance(v, dict) else {}


def label_from_line(line: str, prefixes: tuple[str, ...] = _LABEL_PREFIXES) -> str | None:
    """Extract the launchd label from a formatted alert line.

    Strips markup FIRST. Alert lines are HTML for Telegram, so the label
    arrives wrapped: "• <code>com.example.job</code> exit 1". Tokenising the
    raw line leaves every token starting with "<code>" rather than "com.", so a
    naive prefix test matches nothing and silently keeps every line. That is
    not hypothetical: it made this dedup a no-op for 206 consecutive alerts
    before it was caught on 2026-09-20. The bug is invisible because the
    failure mode is "keeps the line" — it over-reports, so nothing ever errors.
    """
    if not line:
        return None
    plain = _TAG_RE.sub(" ", line).replace("•", " ")
    for word in plain.split():
        if word.startswith(prefixes):
            return word
    return None


def claim(state, label, detail="", now=None, ttl=CLAIM_TTL_S):
    """Record that `label` has already reported its own failure.

    Returns a NEW state dict; expired entries are dropped on the way through so
    the store cannot grow without bound.
    """
    if now is None:
        raise ValueError("now is required — this module does not read the clock")
    state = as_dict(state)
    claims = dict(as_dict(state.get("claims")))
    claims[str(label)] = {"ts": float(now), "detail": str(detail)[:200]}
    cutoff = float(now) - ttl
    claims = {k: v for k, v in claims.items()
              if float(as_dict(v).get("ts") or 0) >= cutoff}
    out = dict(state)
    out["claims"] = claims
    return out


def is_claimed(state, label, now=None, ttl=CLAIM_TTL_S) -> bool:
    """True if `label` self-reported within `ttl`. Fails OPEN (False)."""
    if now is None:
        raise ValueError("now is required — this module does not read the clock")
    try:
        entry = as_dict(as_dict(as_dict(state).get("claims")).get(str(label)))
        if not entry:
            return False
        return (float(now) - float(entry.get("ts") or 0)) <= ttl
    except (TypeError, ValueError):
        return False


def partition_lines(lines, state, now=None, ttl=CLAIM_TTL_S,
                    prefixes: tuple[str, ...] = _LABEL_PREFIXES):
    """Split alert lines into (kept, dropped).

    `dropped` are lines whose job already reported the incident itself. A line
    with no recognisable label is always KEPT — fail open.
    """
    kept, dropped = [], []
    for line in lines:
        label = label_from_line(line, prefixes)
        if label and is_claimed(state, label, now=now, ttl=ttl):
            dropped.append(line)
        else:
            kept.append(line)
    return kept, dropped
