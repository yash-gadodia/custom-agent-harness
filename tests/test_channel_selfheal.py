"""Tests for lib/channel_selfheal.py (live copy: ~/.openclaw/scripts/lib).

Covers the node-host wedge detector (channel_decision) and the claude-cli token
classifier (auth_decision) — the two recurring causes of an always-on agent going
silent (2026-06-22 incident).
"""
import importlib.util
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "channel_selfheal.py"


@pytest.fixture(scope="module")
def cs():
    spec = importlib.util.spec_from_file_location("channel_selfheal", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = 1_000_000.0
HOUR = 3600


def health(**channels):
    return {"channels": channels}


def chan(connected=True, running=True, enabled=True, configured=True):
    return {"connected": connected, "running": running,
            "enabled": enabled, "configured": configured}


# ---------- channel_decision ----------

def test_all_connected_is_healthy(cs):
    h = health(telegram=chan(), whatsapp=chan())
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 0, None, NOW)
    assert (degraded, kick, down, streak) == (False, False, [], 0)


def test_single_degraded_check_does_not_kick(cs):
    # First degraded observation: flag it, but wait (avoid kicking on a transient
    # gateway bounce). Threshold is 2.
    h = health(telegram=chan(connected=False), whatsapp=chan())
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 0, None, NOW)
    assert degraded and not kick
    assert down == ["telegram"] and streak == 1 and kick_at is None


def test_second_degraded_check_kicks(cs):
    h = health(telegram=chan(connected=False), whatsapp=chan(running=False))
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 1, None, NOW)
    assert degraded and kick
    assert down == ["telegram", "whatsapp"] and streak == 2 and kick_at == NOW


def test_kick_respects_cooldown(cs):
    # Just kicked 1 min ago, still down -> streak climbs but no re-kick.
    h = health(telegram=chan(connected=False))
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 2, NOW - 60, NOW)
    assert degraded and not kick and streak == 3 and kick_at == NOW - 60


def test_kick_again_after_cooldown(cs):
    h = health(telegram=chan(connected=False))
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 3, NOW - (HOUR), NOW)
    assert kick and kick_at == NOW


def test_recovery_resets_streak(cs):
    h = health(telegram=chan(), whatsapp=chan())
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 5, NOW - 60, NOW)
    assert not degraded and not kick and streak == 0


def test_disabled_channel_ignored(cs):
    # A disabled/unconfigured channel that isn't connected must not trigger a kick.
    h = health(telegram=chan(), whatsapp=chan(connected=False, enabled=False))
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 1, None, NOW)
    assert not degraded and down == []


def test_malformed_channel_entry_ignored(cs):
    h = {"channels": {"telegram": chan(), "bogus": "not-a-dict"}}
    degraded, kick, down, streak, kick_at = cs.channel_decision(h, 0, None, NOW)
    assert not degraded


# ---------- auth_decision ----------

def test_healthy_token_with_refresh_is_silent(cs):
    # Token with a refresh token self-renews -> even near expiry, no alert.
    tok = {"present": True, "expires_at_ms": NOW * 1000 + 60_000, "has_refresh": True}
    assert cs.auth_decision(tok, int(NOW * 1000)) == (None, None)


def test_missing_token_alerts(cs):
    level, detail = cs.auth_decision({"present": False}, int(NOW * 1000))
    assert level == "missing"


def test_expired_no_refresh_alerts(cs):
    # The exact 2026-06-22 dead state: past expiry, no refresh token.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) - 1, "has_refresh": False}
    level, detail = cs.auth_decision(tok, int(NOW * 1000))
    assert level == "expired"


def test_no_refresh_token_warns_early(cs):
    # Not yet expired but no refresh token -> will hard-die; warn now.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000, "has_refresh": False}
    level, detail = cs.auth_decision(tok, int(NOW * 1000))
    assert level == "no_refresh" and "min" in detail


DAY_MS = 86_400_000


def test_refresh_token_expired_is_fatal(cs):
    # The exact 2026-08-03 dead state: refresh token PRESENT but hard-expired,
    # so renewal 401s. Pre-fix this classified silent (has_refresh -> healthy).
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) - 1}
    level, detail = cs.auth_decision(tok, int(NOW * 1000))
    assert level == "refresh_expired" and "hard-expired" in detail


def test_refresh_token_expiring_warns_before_outage(cs):
    # Inside the warn window: agents still work, but re-auth while it's cheap.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) + 2 * DAY_MS}
    level, detail = cs.auth_decision(tok, int(NOW * 1000))
    assert level == "refresh_expiring" and "~2d" in detail


def test_refresh_token_expiring_rounds_up_not_to_zero(cs):
    # 6h left must not render as "~0d".
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 60_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) + 6 * 3_600_000}
    _, detail = cs.auth_decision(tok, int(NOW * 1000))
    assert "~1d" in detail


def test_refresh_token_far_out_is_silent(cs):
    # Healthy 28-day refresh token (the state right after a fresh login).
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) + 28 * DAY_MS}
    assert cs.auth_decision(tok, int(NOW * 1000)) == (None, None)


def test_absent_refresh_expiry_field_stays_silent(cs):
    # Older token records carry no refreshTokenExpiresAt — must not regress to
    # an alert just because the field is missing.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": None}
    assert cs.auth_decision(tok, int(NOW * 1000)) == (None, None)


# ---------- is_token_healthy (reseed gate) ----------

def test_is_token_healthy_true_with_refresh(cs):
    # has refresh -> healthy even if the access token is already past expiry.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) - 1, "has_refresh": True}
    assert cs.is_token_healthy(tok, int(NOW * 1000)) is True


def test_is_token_healthy_false_when_missing(cs):
    assert cs.is_token_healthy({"present": False}, int(NOW * 1000)) is False


def test_is_token_healthy_false_when_blanked(cs):
    # The blanked-entry outage state: present but no access/refresh, exp 0.
    tok = {"present": True, "expires_at_ms": 0, "has_refresh": False}
    assert cs.is_token_healthy(tok, int(NOW * 1000)) is False


def test_is_token_healthy_false_when_refresh_expired(cs):
    # Must be rejected as a reseed SOURCE — restoring it would "succeed" and
    # then 401, which is how 12 backups burned on 2026-08-03.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) - 1}
    assert cs.is_token_healthy(tok, int(NOW * 1000)) is False


def test_is_token_healthy_true_when_refresh_merely_expiring(cs):
    # Warn-only, NOT fatal: a token with 2 days left still works, so it must
    # stay selectable as a reseed source. Guards against the warn level
    # accidentally disqualifying every usable backup.
    tok = {"present": True, "expires_at_ms": int(NOW * 1000) + 3_600_000,
           "has_refresh": True, "refresh_expires_at_ms": int(NOW * 1000) + 2 * DAY_MS}
    assert cs.is_token_healthy(tok, int(NOW * 1000)) is True
