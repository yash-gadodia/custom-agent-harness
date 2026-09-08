"""Tests for lib/launchagent_failures.py (live copy: ~/.openclaw/scripts/lib)."""
import importlib.util
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "launchagent_failures.py"


@pytest.fixture(scope="module")
def lib():
    spec = importlib.util.spec_from_file_location("launchagent_failures", str(LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PREFIXES = ("com.example.", "org.example.")

SAMPLE = (
    "PID\tStatus\tLabel\n"
    "-\t0\tcom.example.good-job\n"
    "-\t1\tcom.example.broken-job\n"
    "-\t-15\torg.example.signal-killed\n"
    "123\t0\tcom.example.running-job\n"
    "456\t1\tcom.example.running-with-prev-fail\n"
    "-\t1\tcom.apple.not-watched\n"
    "-\t0\tai.openclaw.gateway\n"
    "-\t1\tcom.example.blog-publisher\n"
    "garbage line without tabs\n"
)


class TestParseLaunchctlList:
    def test_detects_failed_watched_jobs(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=PREFIXES))
        assert out["com.example.broken-job"] == "1"
        assert out["com.example.blog-publisher"] == "1"

    def test_negative_status_is_failure(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=PREFIXES))
        assert out["org.example.signal-killed"] == "-15"

    def test_running_jobs_excluded(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=PREFIXES))
        assert "com.example.running-job" not in out
        assert "com.example.running-with-prev-fail" not in out

    def test_clean_jobs_excluded(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=PREFIXES))
        assert "com.example.good-job" not in out

    def test_unwatched_prefixes_excluded(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=PREFIXES))
        assert "com.apple.not-watched" not in out
        assert "ai.openclaw.gateway" not in out

    def test_custom_prefixes(self, lib):
        out = dict(lib.parse_launchctl_list(SAMPLE, prefixes=("com.apple.",)))
        assert out == {"com.apple.not-watched": "1"}

    def test_default_prefixes_are_used_when_omitted(self, lib):
        if not lib.WATCH_PREFIXES:
            pytest.skip("no deployment prefixes in this checkout")
        sample = f"-\t1\t{lib.WATCH_PREFIXES[0]}some-job\n"
        out = dict(lib.parse_launchctl_list(sample))
        assert out == {f"{lib.WATCH_PREFIXES[0]}some-job": "1"}

    def test_header_and_garbage_ignored(self, lib):
        assert lib.parse_launchctl_list("PID\tStatus\tLabel\nnonsense\n", prefixes=PREFIXES) == []

    def test_empty_input(self, lib):
        assert lib.parse_launchctl_list("", prefixes=PREFIXES) == []


class TestParseLaunchctlRunning:
    """A job sampled mid-run has no knowable exit status.

    Reading that as 'recovered' is what produced the false-recovery flapping
    (repeat "didn't stick, needs you" DMs) fixed on 2026-09-07: the label was
    absent from failures, past VERIFY_GRACE_S, so decide() cleared the episode
    and the next pass saw the same failure as brand new.
    """

    def test_separates_running_from_failed(self, lib):
        text = (
            "-\t1\tcom.example.failed\n"
            "4321\t-\tcom.example.midrun\n"
            "-\t0\tcom.example.clean\n"
            "999\t0\tcom.unwatched.other\n"
        )
        assert lib.parse_launchctl_running(text, prefixes=PREFIXES) == {"com.example.midrun"}
        assert dict(lib.parse_launchctl_list(text, prefixes=PREFIXES)) == {"com.example.failed": "1"}

    def test_a_midrun_job_appears_in_neither_failures_nor_clean(self, lib):
        """This is the exact shape that produced the false recovery."""
        text = "4321\t-\tcom.example.apply-patches\n"
        assert dict(lib.parse_launchctl_list(text, prefixes=PREFIXES)) == {}
        assert lib.parse_launchctl_running(text, prefixes=PREFIXES) == {"com.example.apply-patches"}

