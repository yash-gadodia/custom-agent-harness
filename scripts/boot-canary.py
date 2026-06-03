#!/usr/bin/env python3
"""Reboot-proof health canary for the headless Mac Mini.

Why this exists: a reboot on 2026-06-02 left the Mac at the macOS login window
(auto-login was broken — /etc/kcpassword was missing). None of the ~69 user
LaunchAgents loaded, so every morning job silently missed its run. Nothing
alerted: the existing monitors (cron-failure-watcher, agent-health-probe) are
THEMSELVES user LaunchAgents that also died in the reboot.

This runs as a SYSTEM LaunchDaemon (com.example.boot-canary), which loads even
when the Mac is stranded at the login window. It detects that failure class and
DMs the operator via the the assistant bot — same helper agent-health-probe.py uses.

Alert-only by design: it does NOT auto-trigger the missed jobs (too risky for
v1) and makes no LLM calls. Honors DRY_RUN=1 (print to stderr, don't send).

The decision logic is the pure function evaluate(state); main() gathers the
real state off the live machine and feeds it in.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OPENCLAW_CFG = Path("/home/user/.openclaw/openclaw.json")
ALERT_CHAT_ID = "000000000"
ALERT_BOT_ACCOUNT = "happy"
SGT = ZoneInfo("Asia/Singapore")

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts/lib"))
import openclaw_refs as ocr  # noqa: E402


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def evaluate(state: dict) -> list[str]:
    alerts: list[str] = []

    if not (
        state["kcpassword_exists"]
        and state["autologin_user"] == "user"
    ):
        alerts.append(
            "auto-login is misconfigured — a reboot will strand all user agents "
            "(kcpassword/autoLoginUser)"
        )

    if state["console_owner"] == "root":
        alerts.append(
            "Mac is sitting at the login window with no desktop session — "
            "user LaunchAgents are NOT loaded"
        )

    today = state["weekday"]
    now_minute = state["now"].hour * 60 + state["now"].minute
    for job in state["jobs"]:
        weekdays = job["weekdays"]
        if weekdays is not None and today not in weekdays:
            continue
        due = job["due_minute"]
        if now_minute > due + job["grace_minutes"] and not job["artifact_present"]:
            alerts.append(
                f"{job['name']} did not run today (was due {_hhmm(due)})"
            )

    return alerts


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _gym_artifact_present(today_str: str) -> bool:
    pattern = f"/home/user/gym-booker/runs/launchd-{today_str}_*.log"
    return len(glob.glob(pattern)) > 0


def _standup_artifact_present(today_str: str) -> bool:
    run_log = Path(f"/home/user/.openclaw/scripts/standup/state/{today_str}/run.log")
    if not run_log.exists():
        return False
    try:
        return "done:" in run_log.read_text()
    except OSError:
        return False


def gather_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(SGT)
    today_str = now.strftime("%Y-%m-%d")

    autologin_user = _run([
        "defaults", "read",
        "/Library/Preferences/com.apple.loginwindow", "autoLoginUser",
    ])
    return {
        "now": now,
        "weekday": now.weekday(),
        "console_owner": _run(["stat", "-f", "%Su", "/dev/console"]),
        "kcpassword_exists": os.path.exists("/etc/kcpassword"),
        "autologin_user": autologin_user,
        "jobs": [
            {
                "name": "gym-booker",
                "weekdays": None,
                "due_minute": 8 * 60 + 57,
                "grace_minutes": 30,
                "artifact_present": _gym_artifact_present(today_str),
            },
            {
                "name": "the worker agent-standup",
                "weekdays": {0, 1, 2, 3, 4},
                "due_minute": 8 * 60,
                "grace_minutes": 30,
                "artifact_present": _standup_artifact_present(today_str),
            },
        ],
    }


def telegram_dm(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"boot-canary: telegram dm failed: {e}\n")


def main() -> int:
    state = gather_state()
    alerts = evaluate(state)
    if not alerts:
        return 0

    message = "🚨 boot-canary:\n" + "\n".join(alerts)

    if os.environ.get("DRY_RUN"):
        sys.stderr.write(f"[DRY_RUN] would DM {ALERT_CHAT_ID}:\n{message}\n")
        return 1

    try:
        token = ocr.bot_token(ALERT_BOT_ACCOUNT, config_path=OPENCLAW_CFG)
    except Exception as e:
        sys.stderr.write(
            f"boot-canary: could not resolve alert bot token "
            f"({ALERT_BOT_ACCOUNT}): {type(e).__name__}: {str(e)[:200]}\n"
        )
        return 1

    telegram_dm(token, ALERT_CHAT_ID, message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
