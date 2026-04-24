# custom-agent-harness

A self-healing operations harness for long-running LLM agents. It wraps an agent
runtime (here, OpenClaw) with the boring-but-critical machinery that keeps a
fleet of agents alive unattended: health probes, layered self-heal, failure
watchers, safe self-improvement, and PII-safe logging.

It exists because the failure mode that actually hurts is **silent**: an agent
that looks up but quietly stops answering. This harness is built around two
rules learned the hard way:

- **Verify before claiming.** A probe asserts an agent really replies, not that
  the process is running.
- **Fail closed.** Auto-remediation is cooldown-gated; anything that could
  message a customer or take down the gateway is never auto-touched.

## Self-heal at every layer

| Layer | Component |
|-------|-----------|
| Network | `scripts/net-selfheal.sh` — flush DNS → bounce wifi → renew DHCP → reboot |
| Boot | `scripts/boot-canary.py` — alert if scheduled jobs don't return after reboot |
| Gateway | `scripts/gateway-watchdog.sh` — restart a wedged gateway |
| CLI backend | `scripts/cli-watchdog.sh`, `lib/shim_classify.py` — classify errors → retry/failover/surface |
| Channel | `scripts/channel-selfheal.sh`, `lib/channel_selfheal.py` — recover silent chat channels |
| Cron / launchd | `watcher/cron-failure-watcher.sh`, `lib/launchagent_failures.py`, `lib/launchd_selfheal.py` |
| Agent | `scripts/agent-health-probe.py`, `lib/agent_selfheal.py` — synthetic ping → cooldown-gated auto-restart |
| Data / creds | `scripts/backup-claude-creds.sh`, `scripts/backup-weekly.sh`, `lib/pii_redact.py` |

## Safe self-improvement
`lib/harness_safe_apply.py` + `lib/self_improve_apply.py` let the harness apply
its own proposed patches — but only to files that can't message a customer or
take down the runtime; everything else is proposed, not applied.

## Observability
`lib/cost_lib.py`, `lib/usage_tracker.py`, `lib/session_compress.py`,
`lib/main_agent_qa_lib.py`.

## Tests
`python -m pytest tests/` — the pure decision logic (self-heal, watchers) is
unit-tested; CI also runs a full-history `gitleaks` scan.

The commit history is a real, dated evolution of these layers — see CHANGELOG.md.
