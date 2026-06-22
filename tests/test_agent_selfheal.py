"""Tests for lib/agent_selfheal.py (live copy: ~/.openclaw/scripts/lib)."""
import importlib.util
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "agent_selfheal.py"


@pytest.fixture(scope="module")
def lib():
    spec = importlib.util.spec_from_file_location("agent_selfheal", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_restart_below_streak(lib):
    ok, reason = lib.decide_self_heal(streak=1, last_restart_at=None, now=1000)
    assert ok is False
    assert "alert-only" in reason


def test_restart_when_streak_met(lib):
    ok, reason = lib.decide_self_heal(streak=2, last_restart_at=None, now=1000)
    assert ok is True
    assert "restarting" in reason


def test_cooldown_blocks_restart(lib):
    now = 100_000
    ok, reason = lib.decide_self_heal(streak=3, last_restart_at=now - 600, now=now)
    assert ok is False
    assert "cooldown" in reason


def test_restart_after_cooldown(lib):
    now = 100_000
    ok, _ = lib.decide_self_heal(
        streak=3, last_restart_at=now - (lib.RESTART_COOLDOWN_SEC + 1), now=now)
    assert ok is True


def test_streak_boundary(lib):
    assert lib.decide_self_heal(2, None, 0)[0] is True
    assert lib.decide_self_heal(1, None, 0)[0] is False


def test_state_roundtrip(lib, tmp_path):
    p = tmp_path / "state.json"
    lib.save_state(4, 12345.0, p)
    s = lib.load_state(p)
    assert s["streak"] == 4
    assert s["last_restart_at"] == 12345.0


def test_state_defaults_on_missing(lib, tmp_path):
    s = lib.load_state(tmp_path / "nope.json")
    assert s == {"streak": 0, "last_restart_at": None}
