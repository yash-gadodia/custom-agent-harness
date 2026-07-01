"""Aggregate turn-level usage from ~/.openclaw/logs/gateway.log.

claude-cli backend does NOT surface tokens to the gateway log, so this
tracker is built on the two signals that ARE logged per turn:

    [agent/cli-backend] claude live session turn:
        provider=claude-cli model=<model> durationMs=<int> rawLines=<int>

durationMs is wall-clock for the full turn (including tool calls);
rawLines is the number of stream-json events the CLI emitted on that
turn (proxy for reasoning + tool-call density). Neither is dollars,
but together they answer: "did anything spike today?" which is the
real question on a Claude Max subscription (no per-token billing).

For per-agent attribution the log would need to be cross-referenced
with the adjacent [<agent-name>] lines from the same sub-second window
— deferred to v2 since the simple aggregate-by-day already catches
the spike-detection use case.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from pathlib import Path
from typing import Iterable


TURN_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+\d{2}:\d{2}) "
    r"\[agent/cli-backend\] claude live session turn: "
    r"provider=(?P<provider>\S+) "
    r"model=(?P<model>\S+) "
    r"durationMs=(?P<duration>\d+) "
    r"rawLines=(?P<raw_lines>\d+)"
)


def parse_turn_line(line: str) -> dict | None:
    """Parse one gateway.log line. Returns None if it's not a turn event.

    Tolerates extra trailing fields and surrounding whitespace.
    """
    m = TURN_RE.match(line.strip())
    if not m:
        return None
    return {
        "ts": m.group("ts"),
        "date": m.group("ts")[:10],
        "provider": m.group("provider"),
        "model": m.group("model"),
        "duration_ms": int(m.group("duration")),
        "raw_lines": int(m.group("raw_lines")),
    }


def short_model(m: str) -> str:
    """'claude-haiku-4-5-20251001' -> 'haiku-4-5'. Mirrors cost_lib.short_model
    but kept local to avoid lib cross-import drag in tests."""
    if not m:
        return "unknown"
    parts = [p for p in m.split("-") if p != "claude" and not (p.isdigit() and len(p) == 8)]
    return "-".join(parts) or m


def aggregate_by_date(turns: Iterable[dict]) -> dict[str, dict]:
    """Group turns by date. Returns {date: {turns, by_model, duration_p50_ms,
    duration_p95_ms, raw_lines_p95}}.

    Percentiles use Nearest-Rank on a sorted list. statistics.quantiles is
    not used because it interpolates; we want an actually-seen value.
    """
    by_date: dict[str, list[dict]] = {}
    for t in turns:
        by_date.setdefault(t["date"], []).append(t)

    out: dict[str, dict] = {}
    for date, day_turns in by_date.items():
        by_model: dict[str, int] = {}
        for t in day_turns:
            sm = short_model(t["model"])
            by_model[sm] = by_model.get(sm, 0) + 1
        durations = sorted(t["duration_ms"] for t in day_turns)
        raw_lines = sorted(t["raw_lines"] for t in day_turns)
        out[date] = {
            "date": date,
            "turns": len(day_turns),
            "by_model": dict(sorted(by_model.items())),
            "duration_p50_ms": _nearest_rank(durations, 50),
            "duration_p95_ms": _nearest_rank(durations, 95),
            "raw_lines_p95": _nearest_rank(raw_lines, 95),
        }
    return out


def _nearest_rank(sorted_values: list[int], pct: int) -> int:
    """Nearest-rank percentile on a pre-sorted list. Returns 0 if empty."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    rank = max(1, int((pct / 100.0) * n + 0.5))
    return sorted_values[min(rank, n) - 1]


def parse_log(log_path: Path) -> list[dict]:
    """Parse all turn events from a log file. Skips non-turn lines silently."""
    turns: list[dict] = []
    if not log_path.exists():
        return turns
    with log_path.open("r", errors="replace") as f:
        for line in f:
            t = parse_turn_line(line)
            if t is not None:
                turns.append(t)
    return turns


def detect_spike(today: dict, history: list[dict],
                 ratio: float = 2.0, abs_min: int = 50) -> dict | None:
    """Compare today's turn count to the median of last-7-day history.

    Returns {"baseline_median": N, "today": M, "ratio": X.Y} when both
    gates pass:
        - today.turns / baseline_median >= ratio
        - today.turns - baseline_median >= abs_min

    Returns None otherwise. Mirrors is_spike() in cost_lib.py.
    """
    if not history:
        return None
    counts = sorted(h["turns"] for h in history)
    baseline = counts[len(counts) // 2]
    if baseline <= 0:
        return None
    today_n = today.get("turns", 0)
    if today_n - baseline < abs_min:
        return None
    if (today_n / baseline) < ratio:
        return None
    return {
        "baseline_median": baseline,
        "today": today_n,
        "ratio": round(today_n / baseline, 2),
    }


def append_daily(state_path: Path, day_record: dict) -> None:
    """Append one day record to ~/.openclaw/state/usage-daily.jsonl.

    Idempotent on (date) — if the same date already exists at the tail,
    overwrite it instead of appending a duplicate. This lets the cron
    fire multiple times a day (e.g. every 6h) without bloating the file.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if state_path.exists():
        for line in state_path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    existing = [r for r in existing if r.get("date") != day_record.get("date")]
    existing.append(day_record)
    existing.sort(key=lambda r: r.get("date", ""))
    tmp = state_path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r) for r in existing) + "\n")
    os.replace(tmp, state_path)
