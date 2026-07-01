"""Tests for ~/.openclaw/scripts/lib/usage_tracker.py.

Covers: line parsing, model shortening, percentile math, by-date aggregation,
spike detection (both gates), idempotent JSONL append (date dedupe).
"""
import importlib.util
import json
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "usage_tracker.py"


@pytest.fixture(scope="module")
def lib():
    spec = importlib.util.spec_from_file_location("usage_tracker", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE_LINE = (
    "2026-05-18T11:04:26.840+08:00 [agent/cli-backend] "
    "claude live session turn: provider=claude-cli "
    "model=claude-opus-4-7 durationMs=78704 rawLines=167"
)


class TestParseTurnLine:
    def test_happy_path(self, lib):
        r = lib.parse_turn_line(SAMPLE_LINE)
        assert r is not None
        assert r["date"] == "2026-05-18"
        assert r["provider"] == "claude-cli"
        assert r["model"] == "claude-opus-4-7"
        assert r["duration_ms"] == 78704
        assert r["raw_lines"] == 167

    def test_haiku_dated_model_id(self, lib):
        line = SAMPLE_LINE.replace("claude-opus-4-7", "claude-haiku-4-5-20251001")
        r = lib.parse_turn_line(line)
        assert r["model"] == "claude-haiku-4-5-20251001"

    def test_rejects_session_start(self, lib):
        line = (
            "2026-05-18T11:00:07.224+08:00 [agent/cli-backend] "
            "claude live session start: provider=claude-cli "
            "model=claude-opus-4-7 activeSessions=1"
        )
        assert lib.parse_turn_line(line) is None

    def test_rejects_unrelated_log(self, lib):
        assert lib.parse_turn_line("2026-05-18 [ambassador] hello") is None
        assert lib.parse_turn_line("") is None
        assert lib.parse_turn_line("garbage") is None

    def test_tolerates_surrounding_whitespace(self, lib):
        r = lib.parse_turn_line("  " + SAMPLE_LINE + "  ")
        assert r is not None


class TestShortModel:
    def test_drops_claude_and_date(self, lib):
        assert lib.short_model("claude-haiku-4-5-20251001") == "haiku-4-5"

    def test_no_date_suffix(self, lib):
        assert lib.short_model("claude-opus-4-7") == "opus-4-7"

    def test_unknown(self, lib):
        assert lib.short_model("") == "unknown"
        assert lib.short_model(None) == "unknown"


class TestNearestRank:
    def test_empty(self, lib):
        assert lib._nearest_rank([], 95) == 0

    def test_single(self, lib):
        assert lib._nearest_rank([42], 95) == 42

    def test_p50_of_three(self, lib):
        # rank = max(1, int(0.5*3 + 0.5)) = 2 → sorted[1] = 20
        assert lib._nearest_rank([10, 20, 30], 50) == 20

    def test_p95_of_twenty(self, lib):
        # rank = max(1, int(0.95*20 + 0.5)) = 19 → sorted[18]
        values = list(range(1, 21))
        assert lib._nearest_rank(values, 95) == 19


class TestAggregateByDate:
    def _turn(self, ts, model, duration, raw_lines):
        return {
            "ts": ts,
            "date": ts[:10],
            "provider": "claude-cli",
            "model": model,
            "duration_ms": duration,
            "raw_lines": raw_lines,
        }

    def test_single_day(self, lib):
        turns = [
            self._turn("2026-05-18T10:00:00.000+08:00", "claude-opus-4-7", 10000, 50),
            self._turn("2026-05-18T11:00:00.000+08:00", "claude-opus-4-7", 30000, 100),
            self._turn("2026-05-18T12:00:00.000+08:00", "claude-haiku-4-5", 5000, 20),
        ]
        out = lib.aggregate_by_date(turns)
        assert "2026-05-18" in out
        day = out["2026-05-18"]
        assert day["turns"] == 3
        assert day["by_model"] == {"haiku-4-5": 1, "opus-4-7": 2}
        assert day["duration_p50_ms"] in (10000, 30000)

    def test_splits_dates(self, lib):
        turns = [
            self._turn("2026-05-17T23:59:00.000+08:00", "claude-opus-4-7", 1, 1),
            self._turn("2026-05-18T00:00:01.000+08:00", "claude-opus-4-7", 2, 2),
        ]
        out = lib.aggregate_by_date(turns)
        assert set(out.keys()) == {"2026-05-17", "2026-05-18"}

    def test_empty(self, lib):
        assert lib.aggregate_by_date([]) == {}


class TestDetectSpike:
    def _day(self, turns):
        return {"turns": turns, "by_model": {}, "duration_p50_ms": 0,
                "duration_p95_ms": 0, "raw_lines_p95": 0}

    def test_no_history(self, lib):
        assert lib.detect_spike(self._day(200), []) is None

    def test_under_ratio(self, lib):
        history = [self._day(100), self._day(110), self._day(95)]
        assert lib.detect_spike(self._day(150), history) is None  # 1.5x, not 2x

    def test_under_abs_min(self, lib):
        history = [self._day(5), self._day(6), self._day(7)]
        # 6 baseline, 20 today → 3.3x but delta only 14 < abs_min 50
        assert lib.detect_spike(self._day(20), history) is None

    def test_spikes_when_both_gates_pass(self, lib):
        history = [self._day(100), self._day(110), self._day(95)]
        result = lib.detect_spike(self._day(250), history)
        assert result is not None
        assert result["baseline_median"] == 100
        assert result["today"] == 250
        assert result["ratio"] == 2.5

    def test_baseline_zero_returns_none(self, lib):
        history = [self._day(0), self._day(0)]
        assert lib.detect_spike(self._day(100), history) is None


class TestAppendDaily:
    def test_creates_file(self, lib, tmp_path):
        state = tmp_path / "usage-daily.jsonl"
        record = {"date": "2026-05-18", "turns": 42, "by_model": {"opus-4-7": 42}}
        lib.append_daily(state, record)
        assert state.exists()
        lines = state.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record

    def test_dedupes_same_date(self, lib, tmp_path):
        state = tmp_path / "usage-daily.jsonl"
        lib.append_daily(state, {"date": "2026-05-18", "turns": 10})
        lib.append_daily(state, {"date": "2026-05-18", "turns": 20})
        lines = state.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["turns"] == 20

    def test_preserves_other_dates(self, lib, tmp_path):
        state = tmp_path / "usage-daily.jsonl"
        lib.append_daily(state, {"date": "2026-05-17", "turns": 100})
        lib.append_daily(state, {"date": "2026-05-18", "turns": 200})
        lib.append_daily(state, {"date": "2026-05-18", "turns": 250})
        lines = state.read_text().strip().splitlines()
        assert len(lines) == 2
        records = [json.loads(line) for line in lines]
        assert records[0]["turns"] == 100
        assert records[1]["turns"] == 250

    def test_sorts_by_date(self, lib, tmp_path):
        state = tmp_path / "usage-daily.jsonl"
        lib.append_daily(state, {"date": "2026-05-18", "turns": 10})
        lib.append_daily(state, {"date": "2026-05-17", "turns": 20})
        records = [json.loads(line) for line in state.read_text().strip().splitlines()]
        assert [r["date"] for r in records] == ["2026-05-17", "2026-05-18"]

    def test_skips_corrupted_lines(self, lib, tmp_path):
        state = tmp_path / "usage-daily.jsonl"
        state.write_text('not json\n{"date":"2026-05-17","turns":5}\nalso bad\n')
        lib.append_daily(state, {"date": "2026-05-18", "turns": 10})
        records = [json.loads(line) for line in state.read_text().strip().splitlines()]
        assert len(records) == 2
        assert [r["date"] for r in records] == ["2026-05-17", "2026-05-18"]


class TestParseLog:
    def test_skips_non_turn_lines(self, lib, tmp_path):
        log = tmp_path / "gateway.log"
        log.write_text("\n".join([
            "garbage",
            SAMPLE_LINE,
            "another garbage line",
            SAMPLE_LINE.replace("78704", "12345"),
        ]) + "\n")
        turns = lib.parse_log(log)
        assert len(turns) == 2
        assert turns[1]["duration_ms"] == 12345

    def test_missing_log(self, lib, tmp_path):
        assert lib.parse_log(tmp_path / "nope.log") == []
