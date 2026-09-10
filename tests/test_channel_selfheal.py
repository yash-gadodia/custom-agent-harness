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


# ---------- breaker_decision (circuit breaker, approved 20260910-f265c7) ----
# 2026-09-01: whatsapp:default degraded at streak 875+ with kicks still firing
# after DAYS. After 3 failed kicks in 30 min the breaker opens for 2h (no
# kicks, no gateway escalation, ONE trip alert), then grants a single retry
# per hold expiry. Recovery closes and resets everything.


def _trip(cs, now=NOW):
    """Trip the breaker at `now` with 3 recent failed kicks; returns its state."""
    fk = [now - 20 * 60, now - 10 * 60, now - 5 * 60]
    allow, trip, fk2, hold, retry = cs.breaker_decision(fk, None, False, True, now)
    assert trip is True
    return fk2, hold, retry


def test_breaker_recovery_resets_everything(cs):
    allow, trip, fk, hold, retry = cs.breaker_decision(
        [NOW - 60], NOW + HOUR, True, False, NOW)
    assert (allow, trip, fk, hold, retry) == (True, False, [], None, False)


def test_breaker_stays_closed_under_threshold(cs):
    fk = [NOW - 10 * 60, NOW - 5 * 60]
    allow, trip, fk2, hold, retry = cs.breaker_decision(fk, None, False, True, NOW)
    assert allow is True and trip is False
    assert hold is None and fk2 == fk


def test_breaker_trips_on_three_recent_failed_kicks(cs):
    fk, hold, retry = _trip(cs)
    assert hold == NOW + cs.BREAKER_HOLD_S
    assert retry is False


def test_breaker_ignores_kicks_outside_window(cs):
    fk = [NOW - 2 * HOUR, NOW - 90 * 60, NOW - 5 * 60]  # only one recent
    allow, trip, fk2, hold, retry = cs.breaker_decision(fk, None, False, True, NOW)
    assert allow is True and trip is False
    assert fk2 == [NOW - 5 * 60]  # stale entries pruned


def test_breaker_holds_without_retripping(cs):
    _, hold, _ = _trip(cs)
    later = NOW + HOUR  # mid-hold
    allow, trip, _, hold2, _ = cs.breaker_decision([], hold, False, True, later)
    assert allow is False and trip is False
    assert hold2 == hold  # hold end unchanged


def test_breaker_grants_one_retry_at_expiry(cs):
    _, hold, _ = _trip(cs)
    at_expiry = hold + 1
    allow, trip, _, hold2, retry = cs.breaker_decision([], hold, False, True, at_expiry)
    assert allow is True and trip is False  # half-open: the one retry
    assert retry is True


def test_breaker_rearms_silently_after_failed_retry(cs):
    _, hold, _ = _trip(cs)
    after_retry = hold + 300  # next 5-min cycle, still degraded
    allow, trip, _, hold2, retry = cs.breaker_decision([], hold, True, True, after_retry)
    assert allow is False
    assert trip is False  # re-arm must NOT re-DM every 2h
    assert hold2 == after_retry + cs.BREAKER_HOLD_S
    assert retry is False  # fresh retry available at the next expiry


def test_breaker_successful_retry_closes(cs):
    _, hold, _ = _trip(cs)
    allow, trip, fk, hold2, retry = cs.breaker_decision([], hold, True, False, hold + 300)
    assert (allow, trip, fk, hold2, retry) == (True, False, [], None, False)


def test_breaker_full_incident_lifecycle(cs):
    # closed → 3 failed kicks → open → hold → half-open retry → fail →
    # re-armed open → next expiry retry → recovery → closed.
    t = NOW
    fk, hold, retry = [], None, False
    for i in range(3):  # three kicks, 10 min apart, none restore
        allow, trip, fk, hold, retry = cs.breaker_decision(fk, hold, retry, True, t)
        assert allow is True and trip is False
        fk = fk + [t]  # driver appends on each fired kick
        t += 10 * 60
    allow, trip, fk, hold, retry = cs.breaker_decision(fk, hold, retry, True, t)
    assert trip is True and allow is False           # opens
    t = hold + 1
    allow, _, fk, hold, retry = cs.breaker_decision(fk, hold, retry, True, t)
    assert allow is True and retry is True           # half-open retry
    fk = fk + [t]
    t += 300
    allow, trip, fk, hold, retry = cs.breaker_decision(fk, hold, retry, True, t)
    assert allow is False and trip is False          # re-armed, silent
    t = hold + 1
    allow, _, fk, hold, retry = cs.breaker_decision(fk, hold, retry, True, t)
    assert allow is True                             # second retry
    allow, _, fk, hold, retry = cs.breaker_decision(fk, hold, retry, False, t + 300)
    assert (fk, hold, retry) == ([], None, False)    # recovery resets
