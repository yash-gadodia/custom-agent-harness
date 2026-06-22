"""Closed-loop apply stage for the daily self-improve review.

self-improve-review.py PROPOSES (prose). This stage takes concrete candidate
patches, gates each against the FULL test suite in an isolated HOME-redirected
sandbox, and promotes ONLY the green ones to live — git-committed individually
so every auto-edit is one `git revert` away.

Safety invariants (belt-and-suspenders on top of harness_safe_apply):
  - Customer/gateway/comms surfaces are NEVER auto-eligible (is_auto_applicable).
  - Only well-tested files are eligible: lib/*.py (all unit-tested) or a top-
    level script that has tests/unit/py/test_<name>.py. Else -> propose-only.
  - A candidate must py_compile and change <= MAX_DIFF_LINES lines.
  - At most MAX_APPLY_PER_DAY promotions per run.
  - The full gate must pass in the sandbox; live is touched only after green
    (the wrapper owns sandbox/gate/promote/git — those are the side effects).

parse_candidates + plan are pure so the eligibility logic is unit-tested
without claude, git, or the filesystem.
"""
from __future__ import annotations

import json
import os
import re

import harness_safe_apply as hsa

MAX_DIFF_LINES = 60
MAX_APPLY_PER_DAY = 2


def parse_candidates(llm_output: str) -> list[dict]:
    """Extract the candidate list from the LLM reply. Accepts a bare JSON array,
    a ```json fenced block, or an object with a 'candidates' key. Returns [] on
    anything malformed (fail-closed: no patch is better than a garbled one)."""
    if not llm_output or not llm_output.strip():
        return []
    text = llm_output.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except Exception:
            return []
    if isinstance(data, dict):
        data = data.get("candidates", [])
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if isinstance(c, dict) and isinstance(c.get("file"), str) and isinstance(c.get("new_content"), str):
            out.append({"file": c["file"], "new_content": c["new_content"],
                        "rationale": str(c.get("rationale", ""))[:300]})
    return out


def _path_safe(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/") and rel.endswith(".py")


def is_well_tested(rel: str, has_test) -> bool:
    """rel is relative to ~/.openclaw/scripts (e.g. 'lib/foo.py' or 'ops.py').
    lib/*.py are all unit-tested; top-level scripts must have a matching test."""
    if rel.startswith("lib/") and rel.endswith(".py"):
        return True
    return rel.endswith(".py") and has_test(os.path.basename(rel))


def validate(cand: dict, *, exists, has_test, compiles, diff_lines) -> tuple[bool, str]:
    """Pure static eligibility for one candidate. Injected callables keep it
    testable: exists(rel)->bool, has_test(basename)->bool, compiles(src)->bool,
    diff_lines(rel, new_content)->int."""
    rel = cand["file"]
    if not _path_safe(rel):
        return False, "unsafe path (must be a repo-relative .py under scripts/)"
    if not exists(rel):
        return False, "file does not exist (auto-apply only edits existing scripts)"
    ok, blocked = hsa.is_auto_applicable([rel])
    if not ok:
        return False, "protected surface: " + ", ".join(blocked)
    if not is_well_tested(rel, has_test):
        return False, "not a tested surface (needs lib/*.py or a test_<name>.py)"
    if not compiles(cand["new_content"]):
        return False, "new content fails to compile"
    n = diff_lines(rel, cand["new_content"])
    if n == 0:
        return False, "no change"
    if n > MAX_DIFF_LINES:
        return False, f"diff too large ({n} > {MAX_DIFF_LINES} lines)"
    return True, "eligible"


def plan(candidates, *, exists, has_test, compiles, diff_lines, max_apply=MAX_APPLY_PER_DAY):
    """Split candidates into (eligible, rejected). eligible is capped at
    max_apply; the overflow is reported as rejected with a cap reason. rejected
    is a list of (candidate, reason) for the propose-only section of the DM."""
    eligible, rejected = [], []
    for c in candidates:
        ok, reason = validate(c, exists=exists, has_test=has_test,
                              compiles=compiles, diff_lines=diff_lines)
        if ok and len(eligible) < max_apply:
            eligible.append(c)
        elif ok:
            rejected.append((c, f"over daily cap ({max_apply})"))
        else:
            rejected.append((c, reason))
    return eligible, rejected
