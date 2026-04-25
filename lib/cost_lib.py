"""Pure formatters for anthropic-cost-weekly.sh.

The HTTP fetch + Telegram POST stay in the bash heredoc; this lib owns
the deterministic value-formatting (dollars, model-name shortening, date
labels, workspace label fallback).
"""
from __future__ import annotations

import datetime


def to_dollars(cents_str) -> float:
    """API returns 'amount' as a string of cents (or whole dollars depending
    on the endpoint). Convert defensively, return 0 on parse fail."""
    try:
        return float(cents_str) / 100.0
    except (TypeError, ValueError):
        return 0.0


def fmt_money(x: float) -> str:
    """Compact $-formatted dollar amount."""
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 100:
        return f"${x:.0f}"
    if x >= 10:
        return f"${x:.1f}"
    return f"${x:.2f}"


def short_model(m: str | None) -> str:
    """Compact model id: 'claude-haiku-4-5-20251001' -> 'haiku-4-5'.

    Drops the 'claude-' prefix and any 8-digit date suffix. Unknown / empty
    values pass through.
    """
    if not m or m == "unknown":
        return m or "unknown"
    parts = m.split("-")
    out: list[str] = []
    for p in parts:
        if p == "claude":
            continue
        if p.isdigit() and len(p) == 8:
            continue
        out.append(p)
    return "-".join(out) or m


def date_label(s: str) -> str:
    """ISO datetime → '%-d %b' (e.g. '25 Apr')."""
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").strftime("%-d %b")


def ws_label(wid: str | None, ws_names: dict) -> str:
    """Workspace ID → display name, with sane fallbacks.

    None / "" → "Default"; unknown ID → 12-char prefix.
    """
    if wid is None or wid == "":
        return "Default"
    return ws_names.get(wid, wid[:12])


def cache_hit_pct(full_in: float, cached_in: float) -> float | None:
    """% of input $ that hit OpenAI's prompt cache.

    full_in = sum of "input" / "input, long context" line items
    cached_in = sum of "cached input" / "cached input, long context"

    Returns None when there's no input spend at all (avoids 0/0). Otherwise
    a float in [0, 100].
    """
    total = (full_in or 0) + (cached_in or 0)
    if total <= 0:
        return None
    return (cached_in or 0) / total * 100


def is_spike(curr: float, prior: float,
             ratio: float = 2.0, abs_min: float = 100.0) -> bool:
    """True if (project, key) crossed both spike gates.

    A spike must clear:
    - delta = curr - prior >= abs_min
    - curr / prior >= ratio (or prior == 0 and curr >= abs_min, treated as
      a "new mover" spike)

    Both gates exist on purpose: ratio without abs spams on tiny keys
    (a $0.50 key going to $1.50 is 3x but not actionable); abs without
    ratio spams on already-large keys whose normal week-over-week drift
    happens to top $100.
    """
    delta = curr - prior
    if delta < abs_min:
        return False
    if prior <= 0:
        return curr >= abs_min
    return (curr / prior) >= ratio


def next_threshold_crossed(pct: float, prior_alerted: int,
                           thresholds: tuple = (75, 90)) -> int:
    """For a per-person weekly-cap pacer, find the highest threshold the
    person has now passed that's strictly greater than the highest one we've
    already alerted on this week.

    Returns 0 if no new crossing (already alerted at or above current %, or
    not yet at the lowest threshold). Thresholds must be sorted ascending.
    """
    new_thr = 0
    for t in thresholds:
        if pct >= t and t > prior_alerted:
            new_thr = t
    return new_thr
