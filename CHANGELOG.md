# Changelog

A layer-by-layer evolution of the harness. Dates are when each piece first
shipped in the live deployment it was extracted from.

## 2026-06-22 — agent + channel self-heal
- `lib/agent_selfheal.py` + `scripts/agent-health-probe.py`: synthetic per-agent
  ping; on N consecutive silent runs, cooldown-gated auto-restart, then escalate.
- `scripts/channel-selfheal.sh` + `lib/channel_selfheal.py`: recover chat
  channels that go silent without crashing.
- `lib/self_improve_apply.py`: apply path for self-proposed patches.

## 2026-06-16 — safe self-improvement
- `lib/harness_safe_apply.py`: only auto-apply patches to files that can't
  message a customer or take down the runtime.

## 2026-06-10 — launchd watcher + self-heal core
- `lib/launchagent_failures.py`, `lib/launchd_selfheal.py`,
  `watcher/cron-failure-watcher.sh`: detect failed scheduled jobs and kick the
  allowlisted, idempotent ones.

## 2026-06-03 — boot + gateway resilience
- `scripts/boot-canary.py`: alert if scheduled jobs don't come back after reboot.
- `scripts/gateway-watchdog.sh`: restart a wedged gateway.

## 2026-06-02 — network self-heal
- `scripts/net-selfheal.sh` (+ installer): repair a stuck network unattended.

## 2026-05-18 — observability + error classification
- `lib/shim_classify.py`: classify CLI-backend errors (retry / failover / surface).
- `lib/session_compress.py`, `lib/usage_tracker.py`.

## 2026-05-08 → 05-11 — QA + safety utilities
- `lib/main_agent_qa_lib.py`, `lib/pii_redact.py`,
  `scripts/backup-weekly.sh`.

## 2026-05-03 → 05-04 — first ops scripts
- `scripts/backup-claude-creds.sh`, `scripts/cli-watchdog.sh`.

## 2026-04-25 — foundation
- `lib/cost_lib.py`: token/cost accounting.
