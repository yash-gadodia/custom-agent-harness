"""Trajectory compression for ~/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl.

Bounded-context summarization patterned on Hermes's trajectory_compressor:
- Protect first turn (system context / opening user msg).
- Protect last N turns (typically 4) — most-recent context the agent actually
  uses on the next turn.
- LLM-summarize everything in between when the file crosses a size threshold.
- Emit a new .compressed.jsonl with parent_session_id pointing at the original
  for audit. Do NOT delete the original — the MEMORY.md explicitly forbids
  deleting OpenClaw session transcripts (resets agent memory).

The LLM call goes through `claude --dangerously-skip-permissions -p` so it
runs on the Claude Max subscription, not the paid API — required by
feedback_no_anthropic_api_for_scripts. The prompt is piped via stdin (argv
tops out at ~1MB on macOS — E2BIG) and oversized middles are summarized in
chunks then merged, so every call stays inside the model context window.

This module is pure logic + an explicit subprocess seam (run_summarizer)
that tests stub out. No filesystem side-effects from compress_trajectory()
itself except the .compressed.jsonl write at the very end.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_SIZE_THRESHOLD_BYTES = 50 * 1024  # 50KB
DEFAULT_PROTECT_LAST_N = 4
DEFAULT_SUMMARY_TARGET_CHARS = 3000  # ~750 tokens at 4 chars/token
DEFAULT_CHUNK_CHAR_LIMIT = 300_000  # ~75k tokens; keeps each call well inside context


class CompressionResult:
    """Plain class (not @dataclass) — dataclasses break under importlib.util
    .spec_from_file_location on Python 3.12+ because __module__ resolves to
    None at type-construction time. Plain classes are immune."""

    __slots__ = ("source_path", "output_path", "original_turns", "kept_turns",
                 "summarized_turns", "bytes_before", "bytes_after",
                 "skipped", "skip_reason")

    def __init__(self, *, source_path, output_path, original_turns,
                 kept_turns, summarized_turns, bytes_before, bytes_after,
                 skipped=False, skip_reason=None):
        self.source_path = source_path
        self.output_path = output_path
        self.original_turns = original_turns
        self.kept_turns = kept_turns
        self.summarized_turns = summarized_turns
        self.bytes_before = bytes_before
        self.bytes_after = bytes_after
        self.skipped = skipped
        self.skip_reason = skip_reason

    def __repr__(self):
        return (f"CompressionResult(source={self.source_path}, "
                f"original_turns={self.original_turns}, "
                f"kept_turns={self.kept_turns}, "
                f"skipped={self.skipped}, skip_reason={self.skip_reason!r})")


def load_jsonl(path: Path) -> list[dict]:
    """Read JSONL. Tolerates blank lines + corrupted rows (logs the count
    of skipped rows via the returned list's missing entries — caller can
    detect by comparing len(loaded) to source line count if needed)."""
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open("r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def needs_compression(path: Path, threshold_bytes: int = DEFAULT_SIZE_THRESHOLD_BYTES) -> bool:
    return path.exists() and path.stat().st_size >= threshold_bytes


def select_turns_to_summarize(turns: Sequence[dict],
                               protect_last_n: int = DEFAULT_PROTECT_LAST_N) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (first_kept, middle_to_summarize, tail_kept).

    Rules:
    - Always keep first turn.
    - Always keep last `protect_last_n` turns.
    - Everything in between goes to the summarizer.
    - If there aren't enough turns to have a non-empty middle, return middle=[]
      (caller should skip compression in that case).
    """
    if len(turns) <= 1 + protect_last_n:
        return list(turns), [], []
    first = [turns[0]]
    if protect_last_n == 0:
        # turns[-0:] == turns[0:], the inverse of what we want — special-case
        return first, list(turns[1:]), []
    tail = list(turns[-protect_last_n:])
    middle = list(turns[1:-protect_last_n])
    return first, middle, tail


def build_summary_prompt(middle: Sequence[dict],
                          target_chars: int = DEFAULT_SUMMARY_TARGET_CHARS) -> str:
    """Build a deterministic prompt asking for a structured summary."""
    body = "\n".join(json.dumps(t) for t in middle)
    return (
        f"You are compressing an agent conversation trajectory. The block below "
        f"is {len(middle)} JSONL turns from the middle of a longer session. "
        f"Produce a faithful summary in <= {target_chars} characters that "
        f"preserves: (1) decisions made, (2) facts learned about the user/data, "
        f"(3) any open threads. Do NOT speculate beyond what's in the trajectory. "
        f"Reply with the summary text only — no preamble, no JSON.\n\n"
        f"--- BEGIN MIDDLE ({len(middle)} turns) ---\n"
        f"{body}\n"
        f"--- END MIDDLE ---"
    )


def run_summarizer(prompt: str, claude_bin: str = "claude",
                   timeout_s: int = 300) -> str:
    """Invoke `claude --dangerously-skip-permissions -p` with the prompt on
    stdin — argv would hit macOS's ~1MB E2BIG limit on big trajectories.

    Returns the summary string. Raises CalledProcessError on non-zero exit.
    Kept as a thin wrapper so tests can monkeypatch the whole function.
    """
    proc = subprocess.run(
        [claude_bin, "--dangerously-skip-permissions", "-p"],
        input=prompt, capture_output=True, text=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr,
        )
    return proc.stdout.strip()


def chunk_turns(middle: Sequence[dict],
                char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> list[list[dict]]:
    """Greedy-split middle turns into chunks whose serialized size stays under
    char_limit. A single turn bigger than the limit is replaced (in the chunk
    only — never on disk) by a truncated stand-in so the prompt stays bounded."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for t in middle:
        s = json.dumps(t)
        if len(s) > char_limit:
            t = {"type": "oversize_turn_truncated",
                 "original_chars": len(s),
                 "head": s[:char_limit // 2]}
            s = json.dumps(t)
        if cur and cur_len + len(s) + 1 > char_limit:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(t)
        cur_len += len(s) + 1
    if cur:
        chunks.append(cur)
    return chunks


def build_merge_prompt(partials: Sequence[str],
                       target_chars: int = DEFAULT_SUMMARY_TARGET_CHARS) -> str:
    body = "\n\n".join(f"[part {i + 1}]\n{p}" for i, p in enumerate(partials))
    return (
        f"You are merging {len(partials)} partial summaries of consecutive "
        f"segments of one agent conversation. Produce a single faithful summary "
        f"in <= {target_chars} characters preserving, in order: (1) decisions "
        f"made, (2) facts learned about the user/data, (3) any open threads. "
        f"Reply with the summary text only — no preamble.\n\n{body}"
    )


def summarize_middle(middle: Sequence[dict],
                     summarizer: Callable[[str], str],
                     target_chars: int = DEFAULT_SUMMARY_TARGET_CHARS,
                     chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> str:
    """Map-reduce summarization: one call for small middles, per-chunk calls
    plus a merge pass when the serialized middle exceeds chunk_char_limit."""
    chunks = chunk_turns(middle, chunk_char_limit)
    if len(chunks) == 1:
        return summarizer(build_summary_prompt(chunks[0], target_chars))
    partials = [summarizer(build_summary_prompt(c, target_chars)) for c in chunks]
    return summarizer(build_merge_prompt(partials, target_chars))


def compress_trajectory(source: Path,
                         out: Path | None = None,
                         protect_last_n: int = DEFAULT_PROTECT_LAST_N,
                         summarizer: Callable[[str], str] = run_summarizer,
                         dry_run: bool = False,
                         chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> CompressionResult:
    """Compress one trajectory file. Returns a result describing what happened.

    `summarizer` is injected for testability — pass a stub that returns a
    fixed string and you can unit-test the whole pipeline without a real
    Claude call.

    On dry_run=True: do all the work except writing the output file. The
    result still reports the byte deltas as if the write had happened.
    """
    out = out or source.with_suffix(".compressed.jsonl")
    bytes_before = source.stat().st_size if source.exists() else 0
    turns = load_jsonl(source)
    if not turns:
        return CompressionResult(
            source_path=source, output_path=out,
            original_turns=0, kept_turns=0, summarized_turns=0,
            bytes_before=bytes_before, bytes_after=bytes_before,
            skipped=True, skip_reason="empty_or_unreadable",
        )

    first, middle, tail = select_turns_to_summarize(turns, protect_last_n)
    if not middle:
        return CompressionResult(
            source_path=source, output_path=out,
            original_turns=len(turns), kept_turns=len(turns), summarized_turns=0,
            bytes_before=bytes_before, bytes_after=bytes_before,
            skipped=True, skip_reason="not_enough_turns",
        )

    summary_text = summarize_middle(middle, summarizer,
                                    chunk_char_limit=chunk_char_limit)

    parent_session_id = source.stem.split(".")[0]
    summary_turn = {
        "type": "compressed_middle",
        "parent_session_id": parent_session_id,
        "original_turn_count": len(middle),
        "summary": summary_text,
    }

    new_turns = first + [summary_turn] + tail
    new_bytes = "\n".join(json.dumps(t) for t in new_turns) + "\n"

    if not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(new_bytes)
        os.replace(tmp, out)

    return CompressionResult(
        source_path=source, output_path=out,
        original_turns=len(turns),
        kept_turns=len(new_turns),
        summarized_turns=len(middle),
        bytes_before=bytes_before,
        bytes_after=len(new_bytes.encode("utf-8")),
    )


def find_candidates(agents_root: Path,
                     threshold_bytes: int = DEFAULT_SIZE_THRESHOLD_BYTES) -> list[Path]:
    """Find all .trajectory.jsonl files over the threshold that don't
    already have a .compressed.jsonl sibling newer than the source."""
    if not agents_root.exists():
        return []
    out: list[Path] = []
    for traj in agents_root.glob("*/sessions/*.trajectory.jsonl"):
        if not needs_compression(traj, threshold_bytes):
            continue
        compressed = traj.with_suffix(".compressed.jsonl")
        if compressed.exists() and compressed.stat().st_mtime >= traj.stat().st_mtime:
            continue
        out.append(traj)
    return out
