"""Invoke claude-cli for fuzzy LLM judgment from cron/scripts.

Routes through the Claude Max sub via the local `claude` binary instead of
the Anthropic API directly (per feedback_no_anthropic_api_for_scripts.md —
paid API leaks $ that the Max sub already covers).

Usage:

    import os, sys
    sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts/lib"))
    from claude_cli_call import haiku

    text = haiku(system="You are terse.", user="say hi in 3 words")

Failures raise RuntimeError. Callers handle fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess

CLAUDE_BIN = os.environ.get(
    "CLAUDE_CLI_BIN",
    shutil.which("claude") or "/home/user/.local/bin/claude",
)
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT = 60


def haiku(system: str, user: str,
          model: str = DEFAULT_MODEL,
          timeout: int = DEFAULT_TIMEOUT,
          max_budget_usd: float | None = None) -> str:
    """Single-shot Haiku call. Returns stripped text. Raises on failure."""
    cmd = [
        CLAUDE_BIN,
        "--dangerously-skip-permissions",
        "--model", model,
        "--append-system-prompt", system,
        "-p",
    ]
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    proc = subprocess.run(
        cmd,
        input=user,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude-cli exit={proc.returncode}: {proc.stderr[:500]}"
        )
    return proc.stdout.strip()
