"""Tests for ~/.openclaw/scripts/lib/session_compress.py.

Covers: JSONL load tolerance, threshold check, turn-selection rules
(protect-first + protect-last-N), dry-run no-write, candidate discovery,
summarizer injection for offline testing.
"""
import importlib.util
import json
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "session_compress.py"


@pytest.fixture(scope="module")
def lib():
    spec = importlib.util.spec_from_file_location("session_compress", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_summarizer():
    """Returns a function + call-record tuple so tests can verify it ran."""
    calls = []

    def _f(prompt: str) -> str:
        calls.append(prompt)
        return "STUB_SUMMARY"

    _f.calls = calls
    return _f


def _write_traj(path: Path, n_turns: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"turn": i, "content": f"msg_{i}"}) for i in range(n_turns)]
    path.write_text("\n".join(lines) + "\n")


class TestLoadJsonl:
    def test_basic(self, lib, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"a":1}\n{"a":2}\n')
        assert lib.load_jsonl(p) == [{"a": 1}, {"a": 2}]

    def test_blank_lines_skipped(self, lib, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('\n{"a":1}\n\n{"a":2}\n\n')
        assert lib.load_jsonl(p) == [{"a": 1}, {"a": 2}]

    def test_corrupted_lines_skipped(self, lib, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"a":1}\nnot json\n{"a":2}\n')
        assert lib.load_jsonl(p) == [{"a": 1}, {"a": 2}]

    def test_missing_file(self, lib, tmp_path):
        assert lib.load_jsonl(tmp_path / "nope.jsonl") == []


class TestNeedsCompression:
    def test_under_threshold(self, lib, tmp_path):
        p = tmp_path / "small.jsonl"
        p.write_text("a" * 1000)
        assert lib.needs_compression(p, threshold_bytes=50_000) is False

    def test_over_threshold(self, lib, tmp_path):
        p = tmp_path / "big.jsonl"
        p.write_text("a" * 100_000)
        assert lib.needs_compression(p, threshold_bytes=50_000) is True

    def test_missing(self, lib, tmp_path):
        assert lib.needs_compression(tmp_path / "nope") is False


class TestSelectTurnsToSummarize:
    def test_too_few_turns_no_middle(self, lib):
        turns = [{"i": i} for i in range(3)]  # 1 first + 4 tail needed = 5 min
        first, middle, tail = lib.select_turns_to_summarize(turns, protect_last_n=4)
        assert first == turns
        assert middle == []
        assert tail == []

    def test_exactly_min_no_middle(self, lib):
        turns = [{"i": i} for i in range(5)]
        first, middle, tail = lib.select_turns_to_summarize(turns, protect_last_n=4)
        assert middle == []

    def test_has_middle(self, lib):
        turns = [{"i": i} for i in range(10)]
        first, middle, tail = lib.select_turns_to_summarize(turns, protect_last_n=4)
        assert first == [{"i": 0}]
        assert tail == [{"i": i} for i in range(6, 10)]
        assert middle == [{"i": i} for i in range(1, 6)]

    def test_protect_last_n_zero(self, lib):
        turns = [{"i": i} for i in range(5)]
        first, middle, tail = lib.select_turns_to_summarize(turns, protect_last_n=0)
        assert first == [{"i": 0}]
        assert middle == [{"i": i} for i in range(1, 5)]
        assert tail == []


class TestCompressTrajectory:
    def test_empty_skipped(self, lib, tmp_path, fake_summarizer):
        src = tmp_path / "abc.trajectory.jsonl"
        src.write_text("")
        result = lib.compress_trajectory(src, summarizer=fake_summarizer)
        assert result.skipped is True
        assert result.skip_reason == "empty_or_unreadable"
        assert fake_summarizer.calls == []

    def test_too_short_skipped(self, lib, tmp_path, fake_summarizer):
        src = tmp_path / "abc.trajectory.jsonl"
        _write_traj(src, 3)
        result = lib.compress_trajectory(src, summarizer=fake_summarizer)
        assert result.skipped is True
        assert result.skip_reason == "not_enough_turns"
        assert fake_summarizer.calls == []

    def test_compresses_and_writes_output(self, lib, tmp_path, fake_summarizer):
        src = tmp_path / "abc.trajectory.jsonl"
        _write_traj(src, 10)
        result = lib.compress_trajectory(src, summarizer=fake_summarizer)
        assert result.skipped is False
        assert result.original_turns == 10
        assert result.summarized_turns == 5
        assert result.kept_turns == 6  # 1 first + 1 summary + 4 tail
        assert result.output_path.exists()
        assert len(fake_summarizer.calls) == 1

        out_lines = result.output_path.read_text().strip().splitlines()
        assert len(out_lines) == 6
        summary_row = json.loads(out_lines[1])
        assert summary_row["type"] == "compressed_middle"
        assert summary_row["parent_session_id"] == "abc"
        assert summary_row["summary"] == "STUB_SUMMARY"
        assert summary_row["original_turn_count"] == 5

    def test_dry_run_no_write(self, lib, tmp_path, fake_summarizer):
        src = tmp_path / "abc.trajectory.jsonl"
        _write_traj(src, 10)
        out = tmp_path / "out.jsonl"
        result = lib.compress_trajectory(src, out=out,
                                          summarizer=fake_summarizer,
                                          dry_run=True)
        assert result.skipped is False
        assert out.exists() is False
        assert len(fake_summarizer.calls) == 1  # summarizer still runs

    def test_does_not_delete_source(self, lib, tmp_path, fake_summarizer):
        """MEMORY-critical: never delete session transcripts."""
        src = tmp_path / "abc.trajectory.jsonl"
        _write_traj(src, 10)
        lib.compress_trajectory(src, summarizer=fake_summarizer)
        assert src.exists()


class TestFindCandidates:
    def test_finds_over_threshold(self, lib, tmp_path):
        agents = tmp_path / "agents"
        big = agents / "agent-a" / "sessions" / "s1.trajectory.jsonl"
        small = agents / "agent-a" / "sessions" / "s2.trajectory.jsonl"
        big.parent.mkdir(parents=True)
        big.write_text("a" * 100_000)
        small.write_text("a" * 1000)
        out = lib.find_candidates(agents, threshold_bytes=50_000)
        assert big in out
        assert small not in out

    def test_skips_already_compressed(self, lib, tmp_path):
        agents = tmp_path / "agents"
        traj = agents / "x" / "sessions" / "s1.trajectory.jsonl"
        traj.parent.mkdir(parents=True)
        traj.write_text("a" * 100_000)
        compressed = traj.with_suffix(".compressed.jsonl")
        compressed.write_text("done")
        # Make compressed newer than source
        import os, time
        future = traj.stat().st_mtime + 100
        os.utime(compressed, (future, future))
        out = lib.find_candidates(agents, threshold_bytes=50_000)
        assert traj not in out

    def test_returns_when_compressed_stale(self, lib, tmp_path):
        agents = tmp_path / "agents"
        traj = agents / "x" / "sessions" / "s1.trajectory.jsonl"
        traj.parent.mkdir(parents=True)
        traj.write_text("a" * 100_000)
        compressed = traj.with_suffix(".compressed.jsonl")
        compressed.write_text("old")
        import os
        past = traj.stat().st_mtime - 100
        os.utime(compressed, (past, past))
        out = lib.find_candidates(agents, threshold_bytes=50_000)
        assert traj in out

    def test_empty_agents_dir(self, lib, tmp_path):
        assert lib.find_candidates(tmp_path / "nope") == []


class TestBuildSummaryPrompt:
    def test_includes_turn_count(self, lib):
        middle = [{"turn": i} for i in range(5)]
        p = lib.build_summary_prompt(middle)
        assert "5 turns" in p
        assert "BEGIN MIDDLE" in p
        assert "END MIDDLE" in p

    def test_includes_target_chars(self, lib):
        p = lib.build_summary_prompt([{"x": 1}], target_chars=1234)
        assert "1234 characters" in p


class TestChunkTurns:
    def test_single_chunk_when_small(self, lib):
        middle = [{"i": i} for i in range(5)]
        assert lib.chunk_turns(middle, char_limit=10_000) == [middle]

    def test_splits_on_limit_and_preserves_order(self, lib):
        middle = [{"data": f"{i:02d}" + "x" * 40} for i in range(10)]
        chunks = lib.chunk_turns(middle, char_limit=120)
        assert len(chunks) > 1
        assert [t for c in chunks for t in c] == middle
        for c in chunks:
            assert sum(len(json.dumps(t)) + 1 for t in c) <= 120

    def test_oversize_turn_truncated_not_dropped(self, lib):
        middle = [{"big": "y" * 500}]
        chunks = lib.chunk_turns(middle, char_limit=100)
        assert len(chunks) == 1 and len(chunks[0]) == 1
        t = chunks[0][0]
        assert t["type"] == "oversize_turn_truncated"
        assert t["original_chars"] > 100
        assert "head" in t

    def test_empty_middle(self, lib):
        assert lib.chunk_turns([], char_limit=100) == []


class TestSummarizeMiddle:
    def test_single_chunk_one_call(self, lib, fake_summarizer):
        middle = [{"i": i} for i in range(3)]
        out = lib.summarize_middle(middle, fake_summarizer)
        assert out == "STUB_SUMMARY"
        assert len(fake_summarizer.calls) == 1
        assert "BEGIN MIDDLE" in fake_summarizer.calls[0]

    def test_multi_chunk_map_reduce(self, lib):
        calls = []

        def s(prompt):
            calls.append(prompt)
            return f"PART{len(calls)}"

        middle = [{"data": f"{i:02d}" + "x" * 40} for i in range(10)]
        n_chunks = len(lib.chunk_turns(middle, 120))
        assert n_chunks > 1
        out = lib.summarize_middle(middle, s, chunk_char_limit=120)
        assert len(calls) == n_chunks + 1
        assert "merging" in calls[-1]
        assert "PART1" in calls[-1]
        assert out == f"PART{n_chunks + 1}"


class TestRunSummarizerStdin:
    def test_prompt_via_stdin_not_argv(self, lib, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = "OK\n"
            stderr = ""

        def fake_run(args, **kw):
            captured["args"] = args
            captured.update(kw)
            return FakeProc()

        monkeypatch.setattr(lib.subprocess, "run", fake_run)
        big = "z" * 2_000_000
        assert lib.run_summarizer(big) == "OK"
        assert captured["input"] == big
        assert all(len(a) < 100 for a in captured["args"])


class TestCompressTrajectoryChunked:
    def test_big_middle_compresses_via_chunks(self, lib, tmp_path):
        src = tmp_path / "big.trajectory.jsonl"
        lines = [json.dumps({"turn": i, "content": "x" * 40}) for i in range(20)]
        src.write_text("\n".join(lines) + "\n")
        calls = []

        def s(prompt):
            calls.append(prompt)
            return "S"

        r = lib.compress_trajectory(src, summarizer=s, chunk_char_limit=150)
        assert r.skipped is False
        assert len(calls) >= 3
        assert r.output_path.exists()
        assert src.exists()
        rows = [json.loads(l) for l in r.output_path.read_text().strip().splitlines()]
        assert rows[1]["type"] == "compressed_middle"
        assert rows[1]["summary"] == "S"
