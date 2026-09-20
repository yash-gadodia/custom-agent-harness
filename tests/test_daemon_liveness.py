"""Liveness supervision for always-on LaunchAgents."""
import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib" / "daemon_liveness.py"
spec = importlib.util.spec_from_file_location("daemon_liveness", LIB)
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)

AO = frozenset({"com.test.alpha", "com.test.beta"})


def listing(*rows):
    return "\n".join(f"{pid}\t{status}\t{label}" for pid, status, label in rows)


class TestDownDetection:
    def test_live_pid_is_up(self):
        assert lib.down_labels(listing(("123", "0", "com.test.alpha")), AO) == {"com.test.beta"}

    def test_dash_pid_is_down(self):
        # The exact shape of the 2026-09-20 finding: KeepAlive job, no PID,
        # stale negative exit status from a crash a month earlier.
        out = lib.down_labels(listing(("-", "-9", "com.test.alpha")), AO)
        assert "com.test.alpha" in out

    def test_missing_label_is_down(self):
        assert lib.down_labels("", AO) == set(AO)

    def test_unsupervised_labels_ignored(self):
        assert lib.down_labels(listing(("-", "1", "com.other.thing")), AO) == set(AO)

    def test_zero_pid_is_down(self):
        assert "com.test.alpha" in lib.down_labels(listing(("0", "0", "com.test.alpha")), AO)


class TestRestartPolicy:
    def test_first_miss_does_not_restart(self):
        kick, esc, st = lib.decide("", {}, 1000.0, AO)
        assert kick == [] and esc == []
        assert st["com.test.alpha"]["misses"] == 1

    def test_second_consecutive_miss_restarts(self):
        _, _, st = lib.decide("", {}, 1000.0, AO)
        kick, esc, st = lib.decide("", st, 1001.0, AO)
        assert sorted(kick) == ["com.test.alpha", "com.test.beta"]
        assert esc == []

    def test_cooldown_blocks_immediate_retry(self):
        _, _, st = lib.decide("", {}, 1000.0, AO)
        kick, _, st = lib.decide("", st, 1001.0, AO)
        assert kick
        kick2, _, st = lib.decide("", st, 1002.0, AO)
        assert kick2 == [], "must not kickstart again inside the cooldown"

    def test_retry_allowed_after_cooldown(self):
        _, _, st = lib.decide("", {}, 1000.0, AO)
        _, _, st = lib.decide("", st, 1001.0, AO)
        kick, _, st = lib.decide("", st, 1001.0 + lib.RESTART_COOLDOWN_S + 1, AO)
        assert sorted(kick) == ["com.test.alpha", "com.test.beta"]

    def test_escalates_once_then_stays_quiet(self):
        st, now = {}, 1000.0
        _, _, st = lib.decide("", st, now, AO)
        for _ in range(lib.MAX_RESTARTS):
            now += lib.RESTART_COOLDOWN_S + 1
            _, _, st = lib.decide("", st, now, AO)
        now += lib.RESTART_COOLDOWN_S + 1
        _, esc, st = lib.decide("", st, now, AO)
        assert sorted(esc) == ["com.test.alpha", "com.test.beta"], "budget spent -> escalate"
        # The whole point of this exercise: it must not keep shouting.
        for _ in range(5):
            now += lib.RESTART_COOLDOWN_S + 1
            kick, esc, st = lib.decide("", st, now, AO)
            assert esc == [], "escalation must fire at most once per episode"
            assert kick == []

    def test_recovery_ends_episode(self):
        st = {}
        _, _, st = lib.decide("", st, 1000.0, AO)
        _, _, st = lib.decide("", st, 1001.0, AO)
        up = listing(("5", "0", "com.test.alpha"), ("6", "0", "com.test.beta"))
        kick, esc, st = lib.decide(up, st, 1002.0, AO)
        assert (kick, esc, st) == ([], [], {})

    def test_failure_after_recovery_starts_fresh(self):
        st = {}
        _, _, st = lib.decide("", st, 1000.0, AO)
        _, _, st = lib.decide("", st, 1001.0, AO)
        up = listing(("5", "0", "com.test.alpha"), ("6", "0", "com.test.beta"))
        _, _, st = lib.decide(up, st, 1002.0, AO)
        # Fresh episode: needs two misses again, and has a full attempt budget.
        kick, _, st = lib.decide("", st, 1003.0, AO)
        assert kick == []
        kick, _, st = lib.decide("", st, 1004.0, AO)
        assert sorted(kick) == ["com.test.alpha", "com.test.beta"]

    def test_decide_does_not_mutate_caller_state(self):
        st = {"com.test.alpha": {"misses": 1, "restarts": 0, "last_restart_at": 0}}
        snapshot = {k: dict(v) for k, v in st.items()}
        lib.decide("", st, 1000.0, AO)
        assert st == snapshot


class TestDeploymentConfig:
    """ALWAYS_ON is deployment config: harness-export.sh replaces it with an
    empty frozenset in the public repo, so anything asserting on its CONTENTS
    must skip there rather than fail the export's test gate."""

    def test_always_on_is_a_frozenset_of_labels(self):
        assert isinstance(lib.ALWAYS_ON, frozenset)
        if not lib.ALWAYS_ON:
            pytest.skip("ALWAYS_ON genericized by harness-export")
        for label in lib.ALWAYS_ON:
            assert label.count(".") >= 2 and not label.startswith("."), label

    def test_gateway_is_not_supervised_here(self):
        # The gateway has its own TCP-level watchdog; a PID-level check would
        # call it healthy while it was wedged and not serving. Holds trivially
        # on the genericized build, which is the correct default.
        assert not any("gateway" in l for l in lib.ALWAYS_ON)

    def test_export_region_markers_present(self):
        src = LIB.read_text()
        if not lib.ALWAYS_ON:
            pytest.skip("deployment-config region already replaced by harness-export")
        assert src.count("# --- deployment config (replaced by harness-export.sh in the public repo) ---") == 1
        assert src.count("# --- end deployment config ---") == 1
