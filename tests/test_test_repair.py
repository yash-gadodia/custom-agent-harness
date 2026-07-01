"""Tests for lib/test_repair.py — the structural safety guard in front of the
Tier-2 selfheal pytest gate (selfheal-test-repair.py)."""
import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib" / "test_repair.py"


@pytest.fixture(scope="module")
def tr():
    spec = importlib.util.spec_from_file_location("test_repair", str(LIB))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PYTEST_OUT = """\
=================================== FAILURES ===================================
tests/unit/py/test_ai_learn.py:63: AssertionError
FAILED tests/unit/py/test_ai_learn.py::test_build_context_survives_missing_files - assert 'x' == ''
FAILED tests/unit/py/test_ai_learn.py::test_other - assert 1 == 2
ERROR tests/unit/py/test_broken_collect.py - ImportError
"""

OLD = """\
def test_a():
    assert foo() == 1
    assert bar() == 2

def test_b():
    assert baz() == 3
"""


def test_parse_failing_test_files_dedupes_and_sorts(tr):
    assert tr.parse_failing_test_files(PYTEST_OUT) == [
        "tests/unit/py/test_ai_learn.py",
        "tests/unit/py/test_broken_collect.py",
    ]


def test_parse_failing_empty(tr):
    assert tr.parse_failing_test_files("") == []
    assert tr.parse_failing_test_files("all good, 5 passed") == []


def test_failure_signature_order_independent(tr):
    assert tr.failure_signature(["b.py", "a.py"]) == tr.failure_signature(["a.py", "b.py"])
    assert tr.failure_signature(["a.py", "a.py"]) == tr.failure_signature(["a.py"])


def test_safe_repair_accepts_additive(tr):
    new = OLD.replace("    assert baz() == 3\n", "    assert baz() == 3\n    assert baz2() == 4\n")
    ok, reason = tr.safe_repair(OLD, new)
    assert ok, reason


def test_safe_repair_rejects_removed_test_function(tr):
    new = "def test_a():\n    assert foo() == 1\n    assert bar() == 2\n"
    ok, reason = tr.safe_repair(OLD, new)
    assert not ok and "test function" in reason


def test_safe_repair_rejects_removed_assertion(tr):
    new = OLD.replace("    assert bar() == 2\n", "")
    ok, reason = tr.safe_repair(OLD, new)
    assert not ok and "assertion" in reason


def test_safe_repair_rejects_noncompiling(tr):
    new = OLD + "\ndef test_c(:\n    assert True\n"
    ok, reason = tr.safe_repair(OLD, new)
    assert not ok and "compile" in reason


def test_safe_repair_rejects_too_large(tr):
    new = OLD + "".join(f"\ndef test_x{i}():\n    assert {i}\n" for i in range(40))
    ok, reason = tr.safe_repair(OLD, new, max_diff=10)
    assert not ok and "too large" in reason


def test_safe_repair_rejects_no_change(tr):
    ok, reason = tr.safe_repair(OLD, OLD)
    assert not ok and "no change" in reason


def _additive(src):
    return src.replace("    assert baz() == 3\n", "    assert baz() == 3\n    assert extra()\n")


def test_plan_repair_accepts_valid(tr):
    failing = ["tests/unit/py/test_ai_learn.py"]
    cands = [{"file": "tests/unit/py/test_ai_learn.py", "new_content": _additive(OLD), "rationale": "r"}]
    eligible, rejected = tr.plan_repair(cands, failing, read_old=lambda p: OLD)
    assert len(eligible) == 1 and not rejected


def test_plan_repair_rejects_non_test_path(tr):
    cands = [{"file": "openclaw/scripts/ai-learn.py", "new_content": _additive(OLD)}]
    eligible, rejected = tr.plan_repair(cands, ["openclaw/scripts/ai-learn.py"], read_old=lambda p: OLD)
    assert not eligible and "not a test file" in rejected[0][1]


def test_plan_repair_rejects_not_failing(tr):
    cands = [{"file": "tests/unit/py/test_other.py", "new_content": _additive(OLD)}]
    eligible, rejected = tr.plan_repair(cands, ["tests/unit/py/test_ai_learn.py"], read_old=lambda p: OLD)
    assert not eligible and "not among the failing" in rejected[0][1]


def test_plan_repair_rejects_missing_target(tr):
    cands = [{"file": "tests/unit/py/test_ai_learn.py", "new_content": _additive(OLD)}]
    eligible, rejected = tr.plan_repair(cands, ["tests/unit/py/test_ai_learn.py"], read_old=lambda p: None)
    assert not eligible and "does not exist" in rejected[0][1]


def test_is_test_only(tr):
    assert tr.is_test_only(["tests/unit/py/test_a.py", "tests/unit/py/test_b.py"])
    assert not tr.is_test_only(["tests/unit/py/test_a.py", "openclaw/scripts/x.py"])
    assert not tr.is_test_only([])
