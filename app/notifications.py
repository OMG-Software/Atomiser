"""New-video email notifications.

Members are subscribed by default and can opt out from their profile or from
the unsubscribe link in any notification. A video becoming visible to the site
fans out to every subscriber, so the messages go through a durable queue
(``email_queue``) drained by a worker rather than being sent inline: a hundred
members would otherwise mean a hundred blocking SMTP round trips inside the
transcode worker, and a restart mid-send would lose the rest.

The whole feature is inert unless SMTP is configured. See app/mail.py.
"""

import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app import mail
from app.config import Config
from app.db import get_db
from app.utils import generate_token, now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

templates = None  # initialised in main.py

# Identifies this process in email_queue.worker_id, as in app/jobs.py.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_workers: list = []
_stop = asyncio.Event()


def _lease_deadline() -> str:
    return (now_utc() + timedelta(seconds=Config.EMAIL_LEASE_SECONDS)).isoformat()


async def _connect() -> aiosqlite.Connection:
    """Open a worker-owned connection with the same pragmas as get_db()."""
    db = await aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 30000")
    return db


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------

async def unsubscribe_token(db, user_id: int) -> str:
    """Return the user's unsubscribe token, creating one on first use.

    Stored in the clear, unlike session and invite tokens, because the link has
    to be rebuilt for every message. That is acceptable here: the token grants
    exactly one capability - changing this user's notification preference - and
    nothing else. It is not a login.
    """
    cursor = await db.execute("SELECT notify_token FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    if not row:
        return ""
    if row["notify_token"]:
        return row["notify_token"]

    token = generate_token()
    await db.execute("UPDATE users SET notify_token = ? WHERE id = ?", (token, user_id))
    await db.commit()
    return token


async def user_by_token(db, token: str):
    if not token or not token.strip():
        return None
    cursor = await db.execute(
        "SELECT id, email, display_name, notify_new_videos FROM users WHERE notify_token = ?",
        (token.strip(),),
    )
    return await cursor.fetchone()


async def set_preference(db, user_id: int, enabled: bool) -> None:
    await db.execute(
        "UPDATE users SET notify_new_videos = ? WHERE id = ?", (1 if enabled else 0, user_id)
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------

def _site_link(path: str) -> str:
    path = "/" + path.lstrip("/")
    return f"{Config.SITE_URL}{path}"


def _body(site_title: str, video, uploader: str, video_url: str, unsubscribe_url: str) -> str:
    description = (video["description"] or "").strip()
    if len(description) > 400:
        description = description[:400].rstrip() + "..."

    lines = [
        f"{uploader} posted a new video on {site_title}.",
        "",
        video["title"],
    ]
    if description:
        lines += ["", description]
    lines += [
        "",
        f"Watch it here:\n{video_url}",
        "",
        "---",
        f"You are receiving this because you are a member of {site_title} "
        "with new-video notifications turned on.",
        f"Turn them off:\n{unsubscribe_url}",
    ]
    return "\n".join(lines) + "\n"


async def queue_new_video_notifications(db, video_id: int) -> int:
    """Queue a notification per subscriber for a newly visible video.

    Returns the number of messages queued. Safe to call more than once: the
    conditional UPDATE on ``notified_at`` is the gate, so a retried transcode or
    a second visibility change cannot email everyone twice.
    """
    if not Config.NOTIFY_NEW_VIDEOS or not mail.mail_enabled():
        return 0

    cursor = await db.execute(
        """
        SELECT v.id, v.uuid, v.title, v.description, v.owner_id, v.visibility, v.status,
               v.notified_at, u.display_name AS owner_name, u.email AS owner_email
        FROM videos v JOIN users u ON v.owner_id = u.id
        WHERE v.id = ?
        """,
        (video_id,),
    )
    video = await cursor.fetchone()
    if not video:
        return 0
    if video["status"] != "ready" or video["visibility"] != "site" or video["notified_at"]:
        return 0

    # Links are built from SITE_URL because the worker has no request to derive
    # a host from. Without it every link in the email would be broken, so refuse
    # rather than send something useless.
    if not Config.SITE_URL:
        logger.error(
            "SITE_URL is not set, so new-video notification links cannot be built. "
            "Skipping notifications for video %s.",
            video["uuid"],
        )
        return 0

    # Claim the fan-out. rowcount == 1 means this call owns it.
    stamp = now_utc().isoformat()
    cursor = await db.execute(
        "UPDATE videos SET notified_at = ? WHERE id = ? AND notified_at IS NULL",
        (stamp, video_id),
    )
    await db.commit()
    if cursor.rowcount != 1:
        return 0

    cursor = await db.execute(
        """
        SELECT id, email FROM users
        WHERE notify_new_videos = 1 AND id != ? AND email IS NOT NULL AND email != ''
        ORDER BY id
        """,
        (video["owner_id"],),
    )
    recipients = [dict(r) for r in await cursor.fetchall()]
    if not recipients:
        return 0

    site_title = await _site_title(db)
    uploader = video["owner_name"] or video["owner_email"]
    video_url = _site_link(f"/videos/{video['uuid']}")
    subject = f"New video on {site_title}: {video['title']}"

    queued = 0
    for recipient in recipients:
        token = await unsubscribe_token(db, recipient["id"])
        unsubscribe_url = _site_link(f"/notifications/unsubscribe?token={token}")
        await db.execute(
            """
            INSERT INTO email_queue
                (user_id, to_address, subject, body, kind, status, scheduled_for, created_at)
            VALUES (?, ?, ?, ?, 'new_video', 'queued', ?, ?)
            """,
            (
                recipient["id"],
                recipient["email"],
                subject,
                _body(site_title, video, uploader, video_url, unsubscribe_url),
                stamp,
                stamp,
            ),
        )
        queued += 1

    await db.commit()
    logger.info("Queued %d new-video notification(s) for %s", queued, video["uuid"])
    return queued


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

async def _claim_batch(db) -> list:
    """Take up to EMAIL_BATCH_SIZE due messages, or return an empty list.

    SQLite has no UPDATE ... LIMIT in a default build, so this selects
    candidates and then claims them with a conditional UPDATE. The
    status = 'queued' predicate is the concurrency gate against a second worker.
    """
    now = now_utc().isoformat()
    cursor = await db.execute(
        """
        SELECT id FROM email_queue
        WHERE status = 'queued' AND scheduled_for <= ?
        ORDER BY id
        LIMIT ?
        """,
        (now, Config.EMAIL_BATCH_SIZE),
    )
    ids = [row["id"] for row in await cursor.fetchall()]
    if not ids:
        return []

    placeholders = ",".join("?" * len(ids))
    cursor = await db.execute(
        f"UPDATE email_queue SET status = 'sending', attempts = attempts + 1, "
        f"worker_id = ?, lease_expires_at = ? "
        f"WHERE id IN ({placeholders}) AND status = 'queued'",
        [WORKER_ID, _lease_deadline()] + ids,
    )
    await db.commit()
    if not cursor.rowcount:
        return []

    cursor = await db.execute(
        f"SELECT id, user_id, to_address, subject, body, attempts FROM email_queue "
        f"WHERE id IN ({placeholders}) AND status = 'sending' ORDER BY id",
        ids,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _finish(db, message_id: int, error: str, attempts: int) -> None:
    if not error:
        await db.execute(
            """
            UPDATE email_queue
            SET status = 'sent', sent_at = ?, last_error = NULL, lease_expires_at = NULL
            WHERE id = ?
            """,
            (now_utc().isoformat(), message_id),
        )
        return

    if attempts >= Config.EMAIL_MAX_ATTEMPTS:
        await db.execute(
            "UPDATE email_queue SET status = 'failed', last_error = ?, lease_expires_at = NULL WHERE id = ?",
            (error, message_id),
        )
        logger.error("Giving up on queued email %s after %s attempts: %s", message_id, attempts, error)
        return

    # Exponential backoff, so a mail server that is briefly down does not burn
    # every attempt in the space of a minute.
    delay = Config.EMAIL_RETRY_MINUTES * (2 ** (attempts - 1))
    retry_at = (now_utc() + timedelta(minutes=delay)).isoformat()
    await db.execute(
        """
        UPDATE email_queue
        SET status = 'queued', scheduled_for = ?, last_error = ?,
            worker_id = NULL, lease_expires_at = NULL
        WHERE id = ?
        """,
        (retry_at, error, message_id),
    )
    logger.warning(
        "Queued email %s failed (attempt %s), retrying in %sm: %s",
        message_id, attempts, delay, error,
    )


async def _send_batch(db, batch: list) -> None:
    site_title = await _site_title(db)
    items = []
    for message in batch:
        token = await unsubscribe_token(db, message["user_id"]) if message["user_id"] else ""
        items.append({
            "to_address": message["to_address"],
            "subject": message["subject"],
            "body": message["body"],
            "unsubscribe_url": (
                _site_link(f"/notifications/unsubscribe?token={token}")
                if token and Config.SITE_URL else None
            ),
        })

    failures = await mail.send_batch(items, site_title)
    for index, message in enumerate(batch):
        await _finish(db, message["id"], failures.get(index), message["attempts"])
    await db.commit()


async def requeue_orphans(db) -> int:
    """Return messages left 'sending' by a crash to the queue.

    Lease-guarded for the same reason as transcode jobs: a second process
    starting up must not reclaim a batch another worker is mid-send on, or the
    recipients get the notification twice.
    """
    cursor = await db.execute(
        """
        UPDATE email_queue
        SET status = 'queued', worker_id = NULL, lease_expires_at = NULL
        WHERE status = 'sending' AND (lease_expires_at IS NULL OR lease_expires_at < ?)
        """,
        (now_utc().isoformat(),),
    )
    await db.commit()
    recovered = cursor.rowcount or 0
    if recovered:
        logger.info("Requeued %d email(s) left in flight by a previous run", recovered)
    return recovered


async def prune(db) -> int:
    """Drop delivered and abandoned rows past the retention window."""
    cutoff = (now_utc() - timedelta(days=Config.EMAIL_RETENTION_DAYS)).isoformat()
    cursor = await db.execute(
        "DELETE FROM email_queue WHERE status IN ('sent', 'failed') AND created_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount or 0


async def _worker_loop() -> None:
    db = await _connect()
    passes = 0
    try:
        while not _stop.is_set():
            batch = []
            try:
                if mail.mail_enabled():
                    batch = await _claim_batch(db)
                    if batch:
                        await _send_batch(db, batch)

                passes += 1
                # Housekeeping roughly hourly at the default poll interval.
                if passes % max(1, int(3600 / Config.EMAIL_POLL_SECONDS)) == 0:
                    await prune(db)
            except Exception:  # noqa: BLE001 - a bad batch must not kill the worker
                logger.exception("Email worker pass failed")

            if not batch:
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=Config.EMAIL_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
    finally:
        await db.close()


async def start_workers() -> None:
    """Recover in-flight messages, then start the single queue worker."""
    _stop.clear()
    db = await _connect()
    try:
        await requeue_orphans(db)
    finally:
        await db.close()

    if Config.NOTIFY_NEW_VIDEOS and mail.mail_enabled() and not Config.SITE_URL:
        logger.warning(
            "NOTIFY_NEW_VIDEOS is on and SMTP is configured, but SITE_URL is unset. "
            "Notification links cannot be built, so no notifications will be sent."
        )

    _workers.append(asyncio.create_task(_worker_loop(), name="email-worker"))
    logger.info("Started email queue worker")


async def stop_workers() -> None:
    _stop.set()
    if not _workers:
        return
    done, pending = await asyncio.wait(_workers, timeout=5.0)
    for task in pending:
        task.cancel()
    _workers.clear()


# ---------------------------------------------------------------------------
# Unsubscribe routes (no login required)
# ---------------------------------------------------------------------------
#
# These authenticate with the secret token in the link rather than a session,
# so someone can unsubscribe straight from their mail client. That is also why
# the POST carries no CSRF token: there is no ambient authority to abuse, since
# an attacker who does not hold the token cannot act on anyone, and one who does
# hold it could unsubscribe that user directly anyway. The GET deliberately
# changes nothing, because mail scanners and link prefetchers follow URLs in
# email and would otherwise unsubscribe people who never clicked.

@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_page(request: Request, token: str = "", db=Depends(get_db)):
    user = await user_by_token(db, token)
    return templates.TemplateResponse(
        "notifications/unsubscribe.html",
        {
            "request": request,
            "token": token,
            "target": user,
            "subscribed": bool(user and user["notify_new_videos"]),
            "done": False,
            "site_title": await _site_title(db),
        },
        status_code=status.HTTP_200_OK if user else status.HTTP_404_NOT_FOUND,
    )


@router.post("/unsubscribe")
async def unsubscribe_post(
    request: Request,
    token: str = Form(""),
    resubscribe: str = Form(""),
    db=Depends(get_db),
):
    # Mail clients doing RFC 8058 one-click send the token in the query string.
    token = (token or request.query_params.get("token", "")).strip()
    user = await user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown unsubscribe link")

    enabled = bool(resubscribe)
    await set_preference(db, user["id"], enabled)
    logger.info(
        "User %s %s new-video notifications via email link",
        user["id"], "re-enabled" if enabled else "disabled",
    )

    return templates.TemplateResponse(
        "notifications/unsubscribe.html",
        {
            "request": request,
            "token": token,
            "target": user,
            "subscribed": enabled,
            "done": True,
            "site_title": await _site_title(db),
        },
    )
