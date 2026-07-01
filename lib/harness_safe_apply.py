"""Never-break promote gate for auto-applied upstream "steals".

The guarantee, stated honestly: tests cannot prove the absence of all bugs, so
this does NOT promise stolen code is always correct. It promises the invariant
that actually matters for a production system serving live customers:

  1. Live code is modified ONLY if a full test gate passes against an isolated
     sandbox copy first (never edited in place).
  2. The promote is transactional — all files swap in or none do, with rollback,
     so live is never left half-written.
  3. A failing gate, a crash, or a timeout is a strict no-op on live: it stays
     byte-for-byte identical and the caller gets a reason to surface.
  4. Customer-facing / gateway surfaces are never eligible for auto-apply
     (is_auto_applicable) — they route to propose-only instead.

decide-what-runs lives in the caller; this module owns the safety mechanics.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# A steal touching any of these (case-insensitive substring of the repo-relative
# path) is NOT eligible for auto-apply — it goes to propose-only in the digest.
# Mirrors the selfheal NEVER_ALLOWLIST philosophy: nothing that can message a
# customer or take down the gateway gets auto-changed.
NEVER_AUTOAPPLY_SUBSTRINGS = (  # per deployment: protected surfaces (customer-facing, gateway, outbound)
    "gateway", "customer-agent", "whatsapp", "wa-", "send",
)


def is_auto_applicable(rel_files, denylist=NEVER_AUTOAPPLY_SUBSTRINGS):
    """Return (ok, blocked) — ok is False if any file hits a protected surface."""
    blocked = [f for f in rel_files if any(s in f.lower() for s in denylist)]
    return (not blocked), blocked


def run_gate(sandbox_dir, gate_cmd, timeout=900, _run=subprocess.run):
    """Run the gate command inside the sandbox. Returns (passed, output)."""
    r = _run(gate_cmd, cwd=sandbox_dir, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def promote(sandbox_dir, live_dir, rel_files):
    """Transactionally copy rel_files from sandbox to live.

    Stages every file as a temp copy, backs up existing live versions, swaps all
    in via os.replace (atomic per file), then drops backups. If anything raises
    mid-swap, restores every backup, removes any files that didn't previously
    exist in live, and removes leftover temps so live is returned to its exact
    prior state. Raises on failure (after rollback).
    """
    staged = []   # (dst, tmp)
    backups = []  # (dst, bak)
    created = []  # dsts that did not exist in live before this promote
    try:
        for rel in rel_files:
            src = os.path.join(sandbox_dir, rel)
            dst = os.path.join(live_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".promote.tmp"
            shutil.copy2(src, tmp)
            staged.append((dst, tmp))
        for dst, tmp in staged:
            existed = os.path.exists(dst)
            if existed:
                bak = dst + ".promote.bak"
                shutil.copy2(dst, bak)
                backups.append((dst, bak))
            os.replace(tmp, dst)
            if not existed:
                created.append(dst)
        for _, bak in backups:
            os.remove(bak)
    except Exception:
        for dst, bak in backups:
            os.replace(bak, dst)
        for dst in created:
            if os.path.exists(dst):
                os.remove(dst)
        for _, tmp in staged:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise


def safe_apply(sandbox_dir, live_dir, rel_files, gate_cmd, *, _run=subprocess.run):
    """Gate in sandbox, promote to live only if green. Live is untouched on any
    failure. Returns (applied: bool, reason: str)."""
    ok, blocked = is_auto_applicable(rel_files)
    if not ok:
        return False, "blocked: protected surface " + ", ".join(blocked)
    try:
        passed, out = run_gate(sandbox_dir, gate_cmd, _run=_run)
    except Exception as e:  # timeout / crash -> red, live untouched
        return False, f"gate error: {e}"
    if not passed:
        return False, "gate failed: " + out.strip()[-500:]
    try:
        promote(sandbox_dir, live_dir, rel_files)
    except Exception as e:
        return False, f"promote rolled back: {e}"
    return True, "applied: " + ", ".join(rel_files)
