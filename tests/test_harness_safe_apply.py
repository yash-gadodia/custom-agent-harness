"""Tests for ~/.openclaw/scripts/lib/harness_safe_apply.py.

The whole point of this module is the never-break invariant, so the load-bearing
tests are: a red gate leaves live byte-for-byte untouched, a green gate promotes,
a crashing gate is a no-op, and protected surfaces are refused.
"""
import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib" / "harness_safe_apply.py"


@pytest.fixture(scope="module")
def sa():
    spec = importlib.util.spec_from_file_location("harness_safe_apply", str(LIB))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def dirs(tmp_path):
    sandbox = tmp_path / "sandbox"
    live = tmp_path / "live"
    sandbox.mkdir()
    live.mkdir()
    (sandbox / "lib").mkdir()
    (live / "lib").mkdir()
    # live has the original; sandbox has the "stolen" version
    (live / "lib" / "helper.py").write_text("ORIGINAL\n")
    (sandbox / "lib" / "helper.py").write_text("STOLEN\n")
    return sandbox, live


GREEN = ["sh", "-c", "exit 0"]
RED = ["sh", "-c", "echo boom >&2; exit 1"]


class TestNeverBreak:
    def test_green_gate_promotes(self, sa, dirs):
        sandbox, live = dirs
        applied, reason = sa.safe_apply(str(sandbox), str(live), ["lib/helper.py"], GREEN)
        assert applied is True
        assert (live / "lib" / "helper.py").read_text() == "STOLEN\n"

    def test_red_gate_leaves_live_untouched(self, sa, dirs):
        sandbox, live = dirs
        applied, reason = sa.safe_apply(str(sandbox), str(live), ["lib/helper.py"], RED)
        assert applied is False
        assert "gate failed" in reason
        # the invariant that matters:
        assert (live / "lib" / "helper.py").read_text() == "ORIGINAL\n"

    def test_crashing_gate_is_noop(self, sa, dirs):
        sandbox, live = dirs
        applied, reason = sa.safe_apply(str(sandbox), str(live), ["lib/helper.py"],
                                        ["this-binary-does-not-exist-xyz"])
        assert applied is False
        assert "gate error" in reason
        assert (live / "lib" / "helper.py").read_text() == "ORIGINAL\n"

    def test_no_leftover_temp_or_backup_files(self, sa, dirs):
        sandbox, live = dirs
        sa.safe_apply(str(sandbox), str(live), ["lib/helper.py"], GREEN)
        leftovers = [p.name for p in (live / "lib").iterdir()
                     if p.name.endswith((".promote.tmp", ".promote.bak"))]
        assert leftovers == []

    def test_promote_creates_new_file_when_absent_in_live(self, sa, tmp_path):
        sandbox = tmp_path / "s"; live = tmp_path / "l"
        (sandbox / "lib").mkdir(parents=True); live.mkdir()
        (sandbox / "lib" / "new.py").write_text("BRAND NEW\n")
        applied, _ = sa.safe_apply(str(sandbox), str(live), ["lib/new.py"], GREEN)
        assert applied is True
        assert (live / "lib" / "new.py").read_text() == "BRAND NEW\n"


class TestSurfaceScoping:
    def test_protected_surface_is_refused(self, sa):
        for f in ("scripts/customer-agent-reply.py", "scripts/wa-chaser.py",
                  "scripts/gateway-restart.sh", "scripts/send-invoice.py"):
            ok, blocked = sa.is_auto_applicable([f])
            assert ok is False and blocked == [f]

    def test_plain_helper_is_eligible(self, sa):
        ok, blocked = sa.is_auto_applicable(["lib/ops_alert_routing.py", "lib/finance_lib.py"])
        assert ok is True and blocked == []

    def test_protected_surface_blocks_before_gate_runs(self, sa, dirs):
        # even with a green gate, a protected file must not auto-apply
        sandbox, live = dirs
        (live / "customer-agent.py").write_text("ORIGINAL\n")
        (sandbox / "customer-agent.py").write_text("STOLEN\n")
        applied, reason = sa.safe_apply(str(sandbox), str(live), ["customer-agent.py"], GREEN)
        assert applied is False and "protected surface" in reason
        assert (live / "customer-agent.py").read_text() == "ORIGINAL\n"
