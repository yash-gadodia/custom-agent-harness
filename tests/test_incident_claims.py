"""Incident claims — one failure must produce one alert, not two."""
import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib" / "incident_claims.py"
spec = importlib.util.spec_from_file_location("incident_claims", LIB)
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)


class TestLabelExtraction:
    def test_extracts_from_html_wrapped_line(self):
        # THE regression test. This exact shape made the dedup a no-op for 206
        # consecutive alerts: every token starts with "<code>", not "com.".
        line = "• <code>com.example.job</code> exit 1"
        assert lib.label_from_line(line) == "com.example.job"

    def test_extracts_from_plain_line(self):
        assert lib.label_from_line("• com.example.job exit 1") == "com.example.job"

    @pytest.mark.parametrize("line,expected", [
        ("🩹 selfheal: kickstarted <code>ai.example.svc</code>, verifying", "ai.example.svc"),
        ("❌ selfheal didn't stick: <code>org.example.x</code> still exit 1", "org.example.x"),
        ("✅ selfheal verified: <code>com.a.b</code> recovered", "com.a.b"),
    ])
    def test_extracts_from_every_alert_shape(self, line, expected):
        assert lib.label_from_line(line) == expected

    def test_no_label_returns_none(self):
        assert lib.label_from_line("something broke") is None
        assert lib.label_from_line("") is None
        assert lib.label_from_line(None) is None

    def test_nested_markup_does_not_confuse_it(self):
        assert lib.label_from_line("<b>• <code>com.a.b</code></b> exit 2") == "com.a.b"


class TestClaimLifecycle:
    def test_claim_then_claimed(self):
        st = lib.claim({}, "com.a.b", "detail", now=1000.0)
        assert lib.is_claimed(st, "com.a.b", now=1000.0) is True

    def test_unclaimed_label_is_false(self):
        st = lib.claim({}, "com.a.b", now=1000.0)
        assert lib.is_claimed(st, "com.other", now=1000.0) is False

    def test_claim_expires_after_ttl(self):
        st = lib.claim({}, "com.a.b", now=1000.0)
        assert lib.is_claimed(st, "com.a.b", now=1000.0 + lib.CLAIM_TTL_S - 1) is True
        assert lib.is_claimed(st, "com.a.b", now=1000.0 + lib.CLAIM_TTL_S + 1) is False

    def test_expired_entries_pruned_on_write(self):
        st = lib.claim({}, "com.old", now=1000.0)
        st = lib.claim(st, "com.new", now=1000.0 + lib.CLAIM_TTL_S + 10)
        assert "com.old" not in st["claims"], "store must not grow without bound"
        assert "com.new" in st["claims"]

    def test_detail_is_truncated(self):
        st = lib.claim({}, "com.a.b", "x" * 5000, now=1000.0)
        assert len(st["claims"]["com.a.b"]["detail"]) <= 200

    def test_claim_preserves_other_state_keys(self):
        st = lib.claim({"entries": {"k": 1}, "deps": {}}, "com.a.b", now=1000.0)
        assert st["entries"] == {"k": 1} and "deps" in st

    def test_claim_does_not_mutate_input(self):
        original = {"claims": {}}
        snapshot = {"claims": {}}
        lib.claim(original, "com.a.b", now=1000.0)
        assert original == snapshot


class TestFailOpen:
    @pytest.mark.parametrize("bad", [None, [], "string", 42, {"claims": "not-a-dict"}])
    def test_malformed_state_is_not_claimed(self, bad):
        # Worst case must be a duplicate alert, never a swallowed one.
        assert lib.is_claimed(bad, "com.a.b", now=1000.0) is False

    def test_malformed_entry_is_not_claimed(self):
        assert lib.is_claimed({"claims": {"com.a.b": "garbage"}}, "com.a.b", now=1000.0) is False

    def test_missing_ts_is_not_claimed(self):
        assert lib.is_claimed({"claims": {"com.a.b": {}}}, "com.a.b", now=1000.0) is False

    def test_clock_must_be_supplied(self):
        # No hidden time.time(): the caller owns the clock so tests are exact.
        with pytest.raises(ValueError):
            lib.claim({}, "com.a.b")
        with pytest.raises(ValueError):
            lib.is_claimed({}, "com.a.b")


class TestPartitionLines:
    def test_drops_claimed_keeps_unclaimed(self):
        st = lib.claim({}, "com.claimed.job", now=1000.0)
        lines = ["• <code>com.claimed.job</code> exit 1",
                 "• <code>com.other.job</code> exit 1"]
        kept, dropped = lib.partition_lines(lines, st, now=1000.0)
        assert dropped == ["• <code>com.claimed.job</code> exit 1"]
        assert kept == ["• <code>com.other.job</code> exit 1"]

    def test_unlabelled_line_is_kept(self):
        kept, dropped = lib.partition_lines(["mystery failure"], {}, now=1000.0)
        assert kept == ["mystery failure"] and dropped == []

    def test_empty_input(self):
        assert lib.partition_lines([], {}, now=1000.0) == ([], [])

    def test_expired_claim_no_longer_suppresses(self):
        st = lib.claim({}, "com.a.b", now=1000.0)
        line = ["• <code>com.a.b</code> exit 1"]
        kept, dropped = lib.partition_lines(line, st, now=1000.0 + lib.CLAIM_TTL_S + 1)
        assert kept == line and dropped == [], "a stale claim must never mute a new failure"
