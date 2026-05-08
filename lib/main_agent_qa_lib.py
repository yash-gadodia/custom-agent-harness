"""Main-agent QA — deterministic drift detection over the last 7 days.

Mirrors ambassador_qa_lib.py but targets main agent session jsonls. The main
agent talks to customers in WA groups + Telegram threads; quality drift here
is harder to detect than ambassador (no canonical closing line, no escalation
verdict) so we focus on:

  - Reply length distribution: flag turns >600 chars (verbose drift)
  - Banned phrase hits: em-dashes, "I'll loop in", "have a great"
  - Compaction frequency: each compaction event = context bloat
  - Assistant turn count overall

Pure functions; driver does the IO + Anthropic + Telegram calls.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERBOSE_THRESHOLD_CHARS = 600

EM_DASH_RE = re.compile(r"[—–]")

BANNED_PHRASES = (
    "i'll loop in",
    "i'll pass this along",
    "i'll scope",
    "we'll be in touch",
    "have a great",
    "you're all set",
    "let me check",
    "based on what you said",
    "as an ai",
    "i'm just an ai",
)


@dataclass
class SessionStats:
    file: str
    user_turns: int
    assistant_turns: int
    assistant_total_chars: int
    assistant_max_chars: int
    verbose_replies: int  # count of assistant turns > VERBOSE_THRESHOLD_CHARS
    em_dashes: int
    banned_phrases: list[str] = field(default_factory=list)
    compaction_events: int = 0
    last_assistant_excerpt: str = ""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _extract_text(content) -> str:
    """Main session content is sometimes a string, sometimes a list of
    {type:'text', text:...} blocks (Anthropic-style). Flatten to a single
    string for the metric pass."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "\n".join(parts)
    return ""


def summarize_session(path: Path) -> SessionStats:
    entries = parse_jsonl(path)
    user_turns = 0
    assistant_turns = 0
    asst_total = 0
    asst_max = 0
    verbose = 0
    em_dashes = 0
    banned: list[str] = []
    compactions = 0
    last_excerpt = ""

    for entry in entries:
        # Compaction events show up as their own log line with type
        # mentioning "compaction" — count them when present.
        ev_type = entry.get("type", "")
        if "compaction" in str(ev_type).lower():
            compactions += 1
            continue

        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _extract_text(msg.get("content"))

        if role == "user":
            user_turns += 1
        elif role == "assistant" and text:
            assistant_turns += 1
            asst_total += len(text)
            if len(text) > asst_max:
                asst_max = len(text)
                last_excerpt = text[:300]
            if len(text) > VERBOSE_THRESHOLD_CHARS:
                verbose += 1
            em_dashes += len(EM_DASH_RE.findall(text))
            lower = text.lower()
            for phrase in BANNED_PHRASES:
                if phrase in lower:
                    banned.append(phrase)

    return SessionStats(
        file=path.name,
        user_turns=user_turns,
        assistant_turns=assistant_turns,
        assistant_total_chars=asst_total,
        assistant_max_chars=asst_max,
        verbose_replies=verbose,
        em_dashes=em_dashes,
        banned_phrases=banned,
        compaction_events=compactions,
        last_assistant_excerpt=last_excerpt,
    )


def find_recent_sessions(sessions_dir: Path, since: datetime) -> list[Path]:
    """Sessions with mtime in window. Skips .deleted/.reset/.archive sidecars."""
    if not sessions_dir.exists():
        return []
    out: list[Path] = []
    for path in sessions_dir.glob("*.jsonl"):
        name = path.name
        if ".deleted" in name or ".reset" in name or ".archive" in name:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= since:
            out.append(path)
    return sorted(out)


def aggregate(stats: list[SessionStats]) -> dict:
    total_assistant = sum(s.assistant_turns for s in stats)
    total_chars = sum(s.assistant_total_chars for s in stats)
    return {
        "sessions": len(stats),
        "total_user_turns": sum(s.user_turns for s in stats),
        "total_assistant_turns": total_assistant,
        "avg_assistant_chars": (
            round(total_chars / total_assistant, 1) if total_assistant else 0.0
        ),
        "max_assistant_chars": max((s.assistant_max_chars for s in stats), default=0),
        "verbose_replies": sum(s.verbose_replies for s in stats),
        "verbose_pct": (
            round(100.0 * sum(s.verbose_replies for s in stats) / total_assistant, 1)
            if total_assistant
            else 0.0
        ),
        "em_dashes": sum(s.em_dashes for s in stats),
        "banned_phrase_hits": sum(len(s.banned_phrases) for s in stats),
        "compaction_events": sum(s.compaction_events for s in stats),
    }


def find_worst_session(stats: list[SessionStats]) -> SessionStats | None:
    if not stats:
        return None
    return max(
        stats,
        key=lambda s: (
            s.verbose_replies,
            len(s.banned_phrases),
            s.em_dashes,
            s.assistant_max_chars,
        ),
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
