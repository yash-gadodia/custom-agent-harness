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


# ---------- corrupt last_restart_at (regression: b25fa33) -------------------
# NaN comparisons are always False, so a hand-edited NaN (json.loads accepts
# bare NaN) would make the cooldown check evaluate False and silently bypass
# the restart-storm guard. load_state must coerce NaN/negative back to None.

def test_load_state_rejects_nan_restart_at(lib, tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"streak": 3, "last_restart_at": NaN}')
    s = lib.load_state(p)
    assert s["last_restart_at"] is None
    assert s["streak"] == 3  # other fields untouched


def test_load_state_rejects_negative_restart_at(lib, tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"streak": 2, "last_restart_at": -5}')
    assert lib.load_state(p)["last_restart_at"] is None


def test_load_state_rejects_bool_and_string_restart_at(lib, tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"streak": 2, "last_restart_at": true}')
    assert lib.load_state(p)["last_restart_at"] is None
    p.write_text('{"streak": 2, "last_restart_at": "12345"}')
    assert lib.load_state(p)["last_restart_at"] is None


def test_load_state_keeps_valid_restart_at(lib, tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"streak": 2, "last_restart_at": 12345.5}')
    assert lib.load_state(p)["last_restart_at"] == 12345.5


def test_save_state_cleans_up_tempfile_on_dump_failure(lib, tmp_path):
    p = tmp_path / "state.json"
    with pytest.raises(TypeError):
        lib.save_state(1, object(), p)  # unserialisable value
    assert list(tmp_path.glob(".sh-*.tmp")) == []  # tempfile unlinked
    assert not p.exists()  # no partial state written
