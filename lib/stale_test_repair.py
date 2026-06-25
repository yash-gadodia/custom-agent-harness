"""Pure logic for Tier-2 selfheal: repair a STALE TEST that is blocking the
openclaw-backup commit gate (a working-tree code change updated behaviour but
left a test asserting the old behaviour).

The wrapper (selfheal-test-repair.py) owns pytest execution, the claude-cli call,
git, and Telegram. Everything here is side-effect-free and unit-tested.

Safety contract — why a bot is allowed to auto-edit a test at all:
  A red test has two valid fixes: change the test (it is stale) or revert the
  source (it is wrong). A bot cannot tell those apart, so it ONLY ever edits the
  test file, and only when the edit is *additive*: it may not delete a single
  test function or assertion, must stay small, and must compile. An LLM that
  tries to make red green by gutting coverage is rejected here and escalated to a
  human. The full pytest suite is the final gate in the wrapper; this module is
  the cheap structural guard in front of it.
"""
from __future__ import annotations

import difflib
import re

MAX_DIFF_LINES = 40

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(tests/\S+?\.py)(?:::|\s|$)", re.M)
_TESTFUNC_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_\w", re.M)
_ASSERT_RE = re.compile(r"\b(?:assert|pytest\.raises|pytest\.warns)\b")


def parse_failing_test_files(pytest_output: str) -> list[str]:
    """Test files named on FAILED/ERROR lines of pytest output, deduped + sorted."""
    return sorted(set(_FAILED_RE.findall(pytest_output or "")))


def failure_signature(failing_files: list[str]) -> str:
    """Stable key for one failure episode — so the wrapper attempts each distinct
    set of broken tests exactly once and never re-burns tokens on a stuck one."""
    return "|".join(sorted(set(failing_files)))


def _diff_lines(old: str, new: str) -> int:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), n=0)
    return sum(1 for ln in diff if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))


def safe_repair(old_src: str, new_src: str, max_diff: int = MAX_DIFF_LINES) -> tuple[bool, str]:
    """True iff editing old_src -> new_src is an allowed test repair: compiles,
    loses no test function or assertion, and stays small. Fail-closed."""
    try:
        compile(new_src, "<candidate>", "exec")
    except SyntaxError as e:
        return False, f"does not compile ({e.msg})"
    if len(_TESTFUNC_RE.findall(new_src)) < len(_TESTFUNC_RE.findall(old_src)):
        return False, "removes a test function (would weaken coverage)"
    if len(_ASSERT_RE.findall(new_src)) < len(_ASSERT_RE.findall(old_src)):
        return False, "removes an assertion (would weaken coverage)"
    n = _diff_lines(old_src, new_src)
    if n > max_diff:
        return False, f"diff too large ({n} lines > {max_diff}); needs a human"
    if n == 0:
        return False, "no change"
    return True, "ok"


def plan_repair(candidates, failing_files, *, read_old, max_diff: int = MAX_DIFF_LINES):
    """Split LLM candidates into (eligible, rejected). A candidate is eligible
    only if it targets one of the actually-failing test files, lives under tests/,
    and passes safe_repair. read_old(path)->str|None returns current file content.
    rejected is a list of (candidate, reason)."""
    failing = set(failing_files)
    eligible, rejected = [], []
    for c in candidates:
        path = (c.get("file") or "").strip()
        if not path.startswith("tests/") or not path.endswith(".py"):
            rejected.append((c, "not a test file under tests/"))
            continue
        if path not in failing:
            rejected.append((c, "not among the failing tests"))
            continue
        old = read_old(path)
        if old is None:
            rejected.append((c, "target test file does not exist"))
            continue
        ok, reason = safe_repair(old, c["new_content"], max_diff)
        (eligible if ok else rejected).append(c if ok else (c, reason))
    return eligible, rejected


def is_test_only(changed_paths) -> bool:
    """Post-condition guard: the working-tree diff after a repair touches tests/
    exclusively (never a source file). Empty set is not a valid repair."""
    paths = [p for p in changed_paths if p]
    return bool(paths) and all(p.startswith("tests/") for p in paths)
