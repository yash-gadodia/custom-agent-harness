#!/bin/bash
# post-upgrade-check.sh — codified post-upgrade ritual for OpenClaw.
# Run after every `npm i -g openclaw@<version>` (and safe to run anytime).
#
# Upgrades are the #1 breakage vector for any deployment that carries dist
# patches or a hand-tuned config: anchors move between content-hashed chunks,
# config migrations mutate policy fields (we've seen a migration inject '*'
# into an allowFrom), and "it looked fine" is not verification. Encode the
# ritual as a script so it can't be forgotten.
#
# Hard gates (any failure → exit 1):
#   1. apply-patches run: every patch noop/applied (+ gate assertion if enabled)
#   2. your harness test suite (contract tests that police openclaw.json)
# Report-only (printed, never fails the check):
#   3. config diff vs newest rotating backup (eyeball migration mutations)
#   4. openclaw doctor --lint
#   5. openclaw security audit
#
# Configure via env:
#   HARNESS_TESTS_RUN   path to your test runner (default: ~/openclaw/tests/run.sh)
#   OPENCLAW_BIN        openclaw binary (default: from PATH)

set -uo pipefail

OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"
SCRIPTS_DIR="$HOME/.openclaw/scripts"
TESTS_RUN="${HARNESS_TESTS_RUN:-$HOME/openclaw/tests/run.sh}"
CONFIG="$HOME/.openclaw/openclaw.json"
PATCH_LOG="$SCRIPTS_DIR/apply-patches.log"

pass=0
fail=0
section() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
ok()   { pass=$((pass+1)); printf '  ✅ %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf '  ❌ %s\n' "$1"; }
info() { printf '  ℹ️  %s\n' "$1"; }

section "OpenClaw version"
info "$("$OPENCLAW_BIN" --version 2>/dev/null | head -1 || echo 'openclaw CLI not found')"

section "1/5 Patches (hard gate)"
if [ -f "$SCRIPTS_DIR/apply-patches.sh" ]; then
  if bash "$SCRIPTS_DIR/apply-patches.sh"; then
    ok "apply-patches exited 0"
  else
    bad "apply-patches exited non-zero — see $PATCH_LOG"
  fi
  if [ "${OPENCLAW_GATE_ASSERT:-0}" = "1" ]; then
    if tail -5 "$PATCH_LOG" 2>/dev/null | grep -q '"event":"gate_assert_ok"'; then
      ok "outbound gate runtime assertion ok"
    else
      bad "outbound gate runtime assertion NOT ok — see $PATCH_LOG"
    fi
  fi
else
  info "no apply-patches.sh — skipping (no dist patches to re-apply?)"
fi

section "2/5 Harness test suite (hard gate)"
if [ -f "$TESTS_RUN" ]; then
  if bash "$TESTS_RUN" >/tmp/post-upgrade-tests.out 2>&1; then
    ok "full suite green ($(grep -oE '[0-9]+ passed' /tmp/post-upgrade-tests.out | tail -1))"
  else
    bad "test suite FAILED — tail of /tmp/post-upgrade-tests.out:"
    tail -15 /tmp/post-upgrade-tests.out | sed 's/^/     /'
  fi
else
  bad "test runner not found at $TESTS_RUN (set HARNESS_TESTS_RUN)"
fi

section "3/5 Config diff vs newest backup (report-only)"
newest_bak=$(ls -t "$CONFIG".bak* 2>/dev/null | head -1)
if [ -n "$newest_bak" ]; then
  info "diffing against $(basename "$newest_bak") — expect only changes you made on purpose"
  if diff <(jq -S 'del(.meta)' "$CONFIG" 2>/dev/null) <(jq -S 'del(.meta)' "$newest_bak" 2>/dev/null) >/tmp/post-upgrade-config.diff; then
    info "no config drift"
  else
    info "config drift ($(wc -l < /tmp/post-upgrade-config.diff | tr -d ' ') diff lines) — review /tmp/post-upgrade-config.diff"
    grep -E 'allowFrom|dmPolicy|groupPolicy' /tmp/post-upgrade-config.diff | head -10 | sed 's/^/     /'
  fi
else
  info "no config backup found to diff against"
fi

section "4/5 openclaw doctor --lint (report-only)"
if "$OPENCLAW_BIN" doctor --lint >/tmp/post-upgrade-doctor.out 2>&1; then
  info "doctor --lint clean"
else
  info "doctor --lint reported issues (exit $?) — review /tmp/post-upgrade-doctor.out"
fi

section "5/5 openclaw security audit (report-only)"
if "$OPENCLAW_BIN" security audit >/tmp/post-upgrade-security.out 2>&1; then
  info "security audit clean"
else
  info "security audit reported findings (exit $?) — review /tmp/post-upgrade-security.out"
fi

section "Result"
printf '  %d hard-gate checks passed, %d failed\n' "$pass" "$fail"
if [ "$fail" -gt 0 ]; then
  printf '  🔴 POST-UPGRADE CHECK FAILED — do not walk away from this upgrade\n'
  exit 1
fi
printf '  🟢 post-upgrade check passed\n'
exit 0
