"""Tests for lib/launchd_selfheal.py (live copy: ~/.openclaw/scripts/lib)."""
import copy
import importlib.util
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "launchd_selfheal.py"


@pytest.fixture(scope="module")
def lib():
    spec = importlib.util.spec_from_file_location("launchd_selfheal", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = 1_000_000.0
JOB = "com.example.blog-publisher"
JOB2 = "com.example.backup"
AL = frozenset({JOB, JOB2})


def attempted(ago, **kw):
    entry = {"attempted_at": NOW - ago, "status": "1", "attempts": 1, "escalated": False}
    entry.update(kw)
    return {JOB: entry}


class TestFirstFailure:
    def test_allowlisted_failure_gets_kickstarted(self, lib):
        kicks, healed, esc, state = lib.decide({JOB: "1"}, {}, NOW, allowlist=AL)
        assert kicks == [JOB]
        assert state[JOB]["attempts"] == 1
        assert state[JOB]["escalated"] is False
        assert healed == [] and esc == []

    def test_non_allowlisted_ignored(self, lib):
        kicks, healed, esc, state = lib.decide(
            {"com.example.unlisted-job": "1"}, {}, NOW, allowlist=AL)
        assert kicks == [] and state == {}

    def test_multiple_failures_kick_in_sorted_order(self, lib):
        kicks, _, _, _ = lib.decide({JOB2: "5", JOB: "1"}, {}, NOW, allowlist=AL)
        assert kicks == sorted([JOB, JOB2])

    def test_empty_allowlist_never_kicks(self, lib):
        kicks, _, esc, state = lib.decide({JOB: "1"}, {}, NOW, allowlist=frozenset())
        assert kicks == [] and esc == [] and state == {}


class TestVerification:
    def test_recovered_after_grace(self, lib):
        kicks, healed, esc, state = lib.decide(
            {}, attempted(lib.VERIFY_GRACE_S + 1), NOW, allowlist=AL)
        assert healed == [JOB]
        assert state == {} and kicks == [] and esc == []

    def test_no_verdict_mid_run_before_grace(self, lib):
        kicks, healed, esc, state = lib.decide({}, attempted(120), NOW, allowlist=AL)
        assert healed == [] and esc == [] and kicks == []
        assert JOB in state

    def test_escalates_once_when_still_failing(self, lib):
        kicks, healed, esc, state = lib.decide(
            {JOB: "1"}, attempted(lib.VERIFY_GRACE_S + 1), NOW, allowlist=AL)
        assert esc == [(JOB, "1")]
        assert kicks == [] and healed == []
        assert state[JOB]["escalated"] is True
        kicks2, _, esc2, _ = lib.decide({JOB: "1"}, state, NOW + 60, allowlist=AL)
        assert esc2 == [] and kicks2 == []

    def test_no_reattempt_within_cooldown(self, lib):
        kicks, _, esc, _ = lib.decide(
            {JOB: "1"}, attempted(3600, escalated=True), NOW, allowlist=AL)
        assert kicks == [] and esc == []

    def test_reattempt_after_cooldown(self, lib):
        kicks, _, _, state = lib.decide(
            {JOB: "1"}, attempted(lib.COOLDOWN_S + 1, escalated=True), NOW, allowlist=AL)
        assert kicks == [JOB]
        assert state[JOB]["attempts"] == 2
        assert state[JOB]["escalated"] is False

    def test_attempts_capped(self, lib):
        st = attempted(lib.COOLDOWN_S + 1, escalated=True, attempts=lib.MAX_ATTEMPTS)
        kicks, _, esc, state = lib.decide({JOB: "1"}, st, NOW, allowlist=AL)
        assert kicks == [] and esc == []
        assert state[JOB]["attempts"] == lib.MAX_ATTEMPTS

    def test_fresh_episode_after_recovery(self, lib):
        _, healed, _, state = lib.decide(
            {}, attempted(lib.VERIFY_GRACE_S + 1), NOW, allowlist=AL)
        assert healed == [JOB] and state == {}
        kicks, _, _, _ = lib.decide({JOB: "1"}, state, NOW + 60, allowlist=AL)
        assert kicks == [JOB]


class TestSafetyContract:
    def test_module_allowlist_is_default(self, lib):
        if not lib.ALLOWLIST:
            pytest.skip("no deployment allowlist in this checkout")
        job = sorted(lib.ALLOWLIST)[0]
        kicks, _, _, _ = lib.decide({job: "1"}, {}, NOW)
        assert kicks == [job]

    def test_no_banned_jobs_in_allowlist(self, lib):
        assert lib.NEVER_ALLOWLIST_SUBSTRINGS
        for label in lib.ALLOWLIST:
            assert not any(b in label for b in lib.NEVER_ALLOWLIST_SUBSTRINGS), label

    def test_gateway_never_allowlisted(self, lib):
        assert not any("gateway" in label for label in lib.ALLOWLIST)

    def test_decide_does_not_mutate_inputs(self, lib):
        st = attempted(lib.VERIFY_GRACE_S + 1)
        failures = {JOB: "1"}
        st_snap, failures_snap = copy.deepcopy(st), dict(failures)
        lib.decide(failures, st, NOW, allowlist=AL)
        assert st == st_snap and failures == failures_snap
