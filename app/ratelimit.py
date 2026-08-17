"""Portable, in-app rate limiting for authentication endpoints.

The nginx config ships ``limit_req`` zones for /auth/login and friends, but that
only protects deployments that actually use it. Run the app behind a different
proxy, in a container, or locally, and there was no brute-force protection at
all. This module keeps a sliding window of failed attempts in SQLite so the
protection travels with the application.

Two independent keys are tracked per scope:

* **email** — the identifier the client submitted. Counting the submitted string
  (rather than a resolved user id) means an unknown address is throttled exactly
  like a real one, so the response cannot be used to enumerate accounts.
* **ip** — the source address, with a much higher threshold, to catch someone
  spraying many different addresses from one host.
"""

import logging
from datetime import timedelta

from app.config import Config
from app.utils import now_utc

logger = logging.getLogger(__name__)

SCOPE_LOGIN = "login"
SCOPE_REGISTER = "register"
SCOPE_PASSKEY = "passkey"
SCOPE_FORGOT = "forgot"


def _window_start() -> str:
    return (now_utc() - timedelta(seconds=Config.RATE_LIMIT_WINDOW_SECONDS)).isoformat()


async def record_failure(db, scope: str, email: str = None, ip: str = None) -> None:
    """Note a failed attempt against both keys."""
    if not Config.RATE_LIMIT_ENABLED:
        return
    stamp = now_utc().isoformat()
    for kind, value in (("email", (email or "").lower().strip()), ("ip", ip or "")):
        if not value:
            continue
        await db.execute(
            """
            INSERT INTO login_attempts (scope, key_kind, key_value, successful, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (scope, kind, value, stamp),
        )
    await db.commit()


async def clear_failures(db, scope: str, email: str = None) -> None:
    """Reset the counter after a successful authentication."""
    if not Config.RATE_LIMIT_ENABLED or not email:
        return
    await db.execute(
        "DELETE FROM login_attempts WHERE scope = ? AND key_kind = 'email' AND key_value = ?",
        (scope, email.lower().strip()),
    )
    await db.commit()


async def _failure_count(db, scope: str, kind: str, value: str) -> int:
    cursor = await db.execute(
        """
        SELECT COUNT(*) AS c FROM login_attempts
        WHERE scope = ? AND key_kind = ? AND key_value = ? AND successful = 0 AND created_at > ?
        """,
        (scope, kind, value, _window_start()),
    )
    return (await cursor.fetchone())["c"]


async def retry_after_minutes(db, scope: str, email: str = None, ip: str = None) -> int:
    """Minutes the caller must wait, or 0 if they may proceed.

    Checks the explicit account lock first (set once an email crosses its
    threshold, so it survives the attempt rows being pruned), then the sliding
    windows for the email and the source IP.
    """
    if not Config.RATE_LIMIT_ENABLED:
        return 0

    now = now_utc()

    if email:
        cursor = await db.execute(
            "SELECT locked_until FROM users WHERE email = ?", (email.lower().strip(),)
        )
        row = await cursor.fetchone()
        if row and row["locked_until"]:
            try:
                locked_until = _parse(row["locked_until"])
                if locked_until > now:
                    return max(1, int((locked_until - now).total_seconds() // 60) + 1)
            except ValueError:
                pass

        if await _failure_count(db, scope, "email", email) >= Config.LOGIN_MAX_FAILURES_PER_EMAIL:
            return Config.LOCKOUT_MINUTES

    if ip and await _failure_count(db, scope, "ip", ip) >= Config.LOGIN_MAX_FAILURES_PER_IP:
        return Config.LOCKOUT_MINUTES

    return 0


async def apply_lockout(db, email: str) -> None:
    """Lock an account once its failure threshold is crossed.

    Only real accounts get a locked_until stamp; an unknown address is still
    throttled by the sliding window, so both cases look identical to the client.
    """
    if not Config.RATE_LIMIT_ENABLED or not email:
        return
    email = email.lower().strip()
    if await _failure_count(db, SCOPE_LOGIN, "email", email) < Config.LOGIN_MAX_FAILURES_PER_EMAIL:
        return

    until = (now_utc() + timedelta(minutes=Config.LOCKOUT_MINUTES)).isoformat()
    await db.execute("UPDATE users SET locked_until = ? WHERE email = ?", (until, email))
    await db.commit()
    logger.warning("Locked account %s until %s after repeated failed logins", email, until)


async def unlock(db, email: str) -> None:
    """Clear an account lock (called after a successful sign-in or reset)."""
    if not email:
        return
    await db.execute(
        "UPDATE users SET locked_until = NULL WHERE email = ?", (email.lower().strip(),)
    )
    await db.commit()


async def prune(db) -> None:
    """Drop attempt rows older than the window so the table stays small."""
    if not Config.RATE_LIMIT_ENABLED:
        return
    await db.execute("DELETE FROM login_attempts WHERE created_at < ?", (_window_start(),))
    await db.commit()


def throttle_message(minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return (
        f"Too many attempts. For security, please wait about {minutes} {unit} before trying again."
    )


def _parse(value: str):
    from datetime import datetime

    stamp = datetime.fromisoformat(str(value).replace(" ", "T"))
    if stamp.tzinfo is None:
        from datetime import timezone

        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
