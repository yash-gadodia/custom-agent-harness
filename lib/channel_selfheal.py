"""Self-heal for the two recurring causes of an always-on agent going silent.

1. Node-host wedge. The gateway watchdog bounces the gateway (~every 12h), but
   the long-lived node host that holds the Telegram + WhatsApp connections does
   NOT restart with it — it crash-loops `ECONNREFUSED 127.0.0.1:18789` and every
   channel goes silent. `openclaw status`/`openclaw health` still report the
   LaunchAgents "running", so the wedge is invisible there; only
   channels.*.connected exposes it. Fix = kickstart `ai.openclaw.node`.

2. claude-cli auth death. On 2026-06-22 the keychain OAuth token expired with NO
   refresh token, 401-ing every agent's model call. The renewed token now
   carries a refresh token (self-renews), but a missing token or a token with no
   refresh token must alert early — that absence is the precursor to the outage.

   Refresh tokens themselves hard-expire (`refreshTokenExpiresAt`, ~30d). On
   2026-08-03 the isolated token died that way: it still HAD a refresh token, so
   the old classifier saw nothing wrong until the renewal itself 401'd. Worse,
   every credential backup shares one refresh-token lineage, so they all died on
   the same instant (2026-08-01 16:25) and reseed burned 12 candidates for
   nothing. The expiry instant is known ~30 days ahead, so it is now read
   directly: past it = fatal, within AUTH_WARN_DAYS = warn while a re-auth is
   still cheap.

Both decision functions are pure; the wrapper owns launchctl + Telegram so this
stays unit-testable. Kicking the node host is idempotent (it just re-opens the
channel connections), so it is safe to automate, mirroring the gateway watchdog.
"""
from __future__ import annotations

CHANNEL_FAIL_THRESHOLD = 2          # consecutive degraded checks before a kick (~10min at 300s)
KICK_COOLDOWN_S = 15 * 60           # don't re-kick within this window of the last kick
AUTH_WARN_DAYS = 3
AUTH_WARN_MS = AUTH_WARN_DAYS * 24 * 60 * 60 * 1000

# Levels that mean "agents are down / will be down and cannot self-recover".
# is_token_healthy keys off this set, so a level absent from it (refresh_expiring)
# still counts as a usable reseed source — a token expiring in 2 days is a fine
# thing to restore from, it just deserves an alert.
FATAL_AUTH_LEVELS = frozenset({"missing", "expired", "no_refresh", "refresh_expired"})


def channel_decision(health, streak, last_kick_at, now):
    """Decide whether the node host is wedged and should be kicked.

    Returns (degraded, kick, down, new_streak, new_last_kick_at).
      degraded         any enabled+configured channel not connected+running
      kick             streak crossed threshold and outside the kick cooldown
      down             list of degraded channel names (for the alert)
    """
    channels = (health or {}).get("channels", {}) or {}
    down = []
    for name, c in channels.items():
        if not isinstance(c, dict):
            continue
        if not c.get("enabled") or not c.get("configured"):
            continue
        if not (c.get("connected") and c.get("running")):
            down.append(name)

    if not down:
        return False, False, [], 0, last_kick_at

    new_streak = streak + 1
    kick = new_streak >= CHANNEL_FAIL_THRESHOLD and (
        last_kick_at is None or now - last_kick_at >= KICK_COOLDOWN_S
    )
    return True, kick, sorted(down), new_streak, (now if kick else last_kick_at)


def auth_decision(token, now_ms):
    """Classify claude-cli token health. Returns (level, detail) or (None, None).

    A token WITH a live refresh token self-renews, so access-token expiry alone
    is a non-event. The states that take agents down (and so warrant an alert):
      missing          no token at all
      expired          past expiry AND no refresh token to renew it
      no_refresh       token present but carries no refresh token
      refresh_expired  refresh token itself hard-expired — renewal 401s
    Plus one warn-only level, which is NOT fatal and stays a valid reseed source:
      refresh_expiring refresh token hard-expires within AUTH_WARN_DAYS
    """
    if not token or not token.get("present"):
        return "missing", "no claude-cli token found — agents cannot call models"

    exp = token.get("expires_at_ms")
    has_refresh = bool(token.get("has_refresh"))
    refresh_exp = token.get("refresh_expires_at_ms")

    if has_refresh and refresh_exp is not None and refresh_exp <= now_ms:
        # Reseed can still fix this, but only from a source on a DIFFERENT
        # lineage (e.g. the default keychain) — never from a sibling backup.
        return ("refresh_expired",
                "claude-cli refresh token hard-expired — it cannot renew; re-auth required")
    if exp is not None and exp <= now_ms and not has_refresh:
        return "expired", "claude-cli token expired with no refresh token — re-auth required"
    if not has_refresh:
        # Reachable only when exp is None or exp > now_ms (the expired+no_refresh
        # case returned above), so diff_ms is always positive here.
        when = ""
        if exp is not None:
            # Ceiling so a 30s-remaining token doesn't round down to 0 min.
            mins = max(1, (exp - now_ms + 59_999) // 60_000)
            when = f" (hard-expires in ~{mins} min)"
        return "no_refresh", f"claude-cli token has NO refresh token{when} — it cannot self-renew; re-auth soon"
    if refresh_exp is not None and refresh_exp - now_ms <= AUTH_WARN_MS:
        # Ceiling so a 20h-remaining token doesn't round down to "0 days".
        days = max(1, (refresh_exp - now_ms + 86_399_999) // 86_400_000)
        return ("refresh_expiring",
                f"claude-cli refresh token hard-expires in ~{days}d — re-auth before it does")
    return None, None


def is_token_healthy(token, now_ms):
    """True when the isolated claude-cli token can serve/self-renew — i.e.
    auth_decision finds none of the FATAL_AUTH_LEVELS. A token with a live
    refresh token is healthy even past its access-token expiry; one that is
    merely nearing its refresh-token expiry (refresh_expiring) is healthy too,
    so it stays selectable as a reseed source.
    Used by reseed-openclaw-cred.py to decide when a reseed is needed and which
    backup/keychain sources are safe to reseed from."""
    level, _ = auth_decision(token, now_ms)
    return level not in FATAL_AUTH_LEVELS
