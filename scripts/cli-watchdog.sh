#!/bin/bash
# Tightens cli-watchdog defaults so stuck claude-cli children get killed
# faster. Default fresh.minMs is 180000ms (3min) — way too long for chat
# UX. After this patch:
#   fresh.minMs: 180000ms (3min)  → 45000ms (45s)
#   fresh.maxMs: 600000ms (10min) → 180000ms (3min)
#   resume.minMs: 60000ms (1min)  → 30000ms (30s)
#   resume.maxMs: 180000ms (3min) → 90000ms (90s)
#
# Why: today's incidents (2026-05-04) had claude-cli silently die / hold
# zombie HTTPS connections; gateway didn't notice for 3+ min. Tighter
# watchdog means stuck → fallback to anthropic API in 45s, not 3min.
#
# Idempotent — safe to re-run after openclaw upgrades.

set -eu

OC_ROOT="$(npm root -g 2>/dev/null)/openclaw/dist"
[ ! -d "$OC_ROOT" ] && { echo "PATCH-WATCHDOG: openclaw dist not found" >&2; exit 1; }

python3 - "$OC_ROOT" <<'PYEOF'
import sys, pathlib, re

root = pathlib.Path(sys.argv[1])

# Find the cli-watchdog-defaults file by content. The hash suffix changes
# on upgrades, so search every dist/*.js for the literal anchor string.
target = None
for path in root.rglob("cli-watchdog-defaults*.js"):
    if "CLI_FRESH_WATCHDOG_DEFAULTS" in path.read_text(encoding="utf-8"):
        target = path
        break
if not target:
    # Fallback — content search across all dist files
    for path in root.rglob("*.js"):
        try:
            txt = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "CLI_FRESH_WATCHDOG_DEFAULTS = {" in txt:
            target = path
            break

if not target:
    print("PATCH-WATCHDOG: cannot find cli-watchdog-defaults source", file=sys.stderr)
    sys.exit(1)

src = target.read_text(encoding="utf-8")

# Anchor: the FRESH defaults block. minMs: 18e4 (180s/3min), maxMs: 6e5 (600s/10min)
fresh_old = "CLI_FRESH_WATCHDOG_DEFAULTS = {\n\tnoOutputTimeoutRatio: .8,\n\tminMs: 18e4,\n\tmaxMs: 6e5\n}"
fresh_new = "CLI_FRESH_WATCHDOG_DEFAULTS = {\n\tnoOutputTimeoutRatio: .8,\n\tminMs: 45000,\n\tmaxMs: 18e4\n}"

# Anchor: the RESUME defaults block. minMs: 6e4 (60s), maxMs: 18e4 (180s)
resume_old = "CLI_RESUME_WATCHDOG_DEFAULTS = {\n\tnoOutputTimeoutRatio: .3,\n\tminMs: 6e4,\n\tmaxMs: 18e4\n}"
resume_new = "CLI_RESUME_WATCHDOG_DEFAULTS = {\n\tnoOutputTimeoutRatio: .3,\n\tminMs: 3e4,\n\tmaxMs: 9e4\n}"

# Re-applied check: detect already-patched values
already = ("minMs: 45000" in src) and ("minMs: 3e4" in src)

if already:
    print("PATCH-WATCHDOG: already applied")
    sys.exit(0)

new_src = src
fresh_count = new_src.count(fresh_old)
resume_count = new_src.count(resume_old)

if fresh_count == 0 and resume_count == 0:
    # Anchors missing — bundle layout changed. Refuse to patch silently.
    print(f"PATCH-WATCHDOG: anchors missing in {target.name} (fresh={fresh_count} resume={resume_count})", file=sys.stderr)
    sys.exit(1)

if fresh_count:
    new_src = new_src.replace(fresh_old, fresh_new, 1)
if resume_count:
    new_src = new_src.replace(resume_old, resume_new, 1)

target.write_text(new_src, encoding="utf-8")
rel = target.relative_to(root)
print(f"PATCH-WATCHDOG: applied to {rel} (fresh.minMs 180s→45s, fresh.maxMs 600s→180s, resume.minMs 60s→30s, resume.maxMs 180s→90s)")
PYEOF
