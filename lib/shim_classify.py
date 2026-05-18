"""Classify the exit reason of a `claude` invocation so the gateway can
route fallback decisions intelligently instead of blindly hopping to the
Anthropic API on every blip.

Categories:
    ok        — clean exit, no signal needed
    auth      — OAuth refresh failure, invalid token, keychain miss
    rate      — Anthropic 429 / Claude Max throttle
    context   — context window exceeded
    network   — connection refused, DNS, timeout
    server    — 5xx from Anthropic backend
    canceled  — SIGINT / killed
    unknown   — exit != 0 with stderr that doesn't match the patterns above

The classifier is pure: it takes (exit_code, stderr_text) and returns one
of those strings. Bash side writes ~/.openclaw/state/shim-last-error.json
so the daemon (or a future watchdog) can act on it.

Pattern selection priority follows the order above (auth before rate
matters because some auth errors include the word "rate" in the message).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


# Patterns ordered: more-specific (auth) → less-specific (network, server)
_PATTERNS = [
    ("auth", re.compile(r"(?i)(invalid[_ -]?api[_ -]?key|"
                          r"oauth|"
                          r"unauthorized|"
                          r"401|"
                          r"keychain|"
                          r"credentials? (?:not |un)?found|"
                          r"please (?:re-?login|sign in))")),
    ("rate", re.compile(r"(?i)(rate[_ -]?limit|"
                          r"429|"
                          r"too many requests|"
                          r"throttle|"
                          r"quota exceeded)")),
    ("context", re.compile(r"(?i)(context (?:window |length )?exceeded|"
                             r"prompt is too long|"
                             r"413|"
                             r"input_tokens.*exceeds)")),
    ("network", re.compile(r"(?i)(connection (?:refused|reset|aborted)|"
                              r"dns|"
                              r"getaddrinfo|"
                              r"econnrefused|"
                              r"network (?:is )?unreachable|"
                              r"timed? out|"
                              r"timeout)")),
    ("server", re.compile(r"(?i)(5\d\d|"
                            r"internal server error|"
                            r"bad gateway|"
                            r"service unavailable|"
                            r"gateway timeout)")),
]


def classify(exit_code: int, stderr_text: str) -> str:
    """Return the category string for a (exit, stderr) pair."""
    if exit_code == 0:
        return "ok"
    if exit_code in (130, 143):  # SIGINT, SIGTERM
        return "canceled"
    text = stderr_text or ""
    for name, pat in _PATTERNS:
        if pat.search(text):
            return name
    return "unknown"


def should_retry_primary(category: str) -> bool:
    """Whether a transient retry on the SAME backend (claude-cli) makes sense.

    rate    → yes, after backoff
    server  → yes, after backoff
    network → yes, after short backoff
    others  → no (fall back or surface)
    """
    return category in {"rate", "server", "network"}


def should_fail_over(category: str) -> bool:
    """Whether to hop to the Anthropic-API fallback for THIS turn.

    auth    → yes (claude-cli credentials are stale; API key may work)
    unknown → yes (defensive — fail open)
    others  → no (rate/server/network: retry primary; context: not solved by failover)
    """
    return category in {"auth", "unknown"}


def write_state(state_path: Path, *, exit_code: int, category: str,
                  stderr_excerpt: str = "", argv: list[str] | None = None) -> None:
    """Atomically write the classification state for the daemon to read."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _now_iso(),
        "exit_code": exit_code,
        "category": category,
        "stderr_excerpt": (stderr_excerpt or "")[:500],
        "argv": argv or [],
        "should_retry_primary": should_retry_primary(category),
        "should_fail_over": should_fail_over(category),
    }
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, state_path)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
