"""Durable transcode queue.

Transcoding used to run as a FastAPI ``BackgroundTask``. That works right up
until the process restarts: the task vanishes, the video is left in
``uploading`` forever, and nothing retries it or tells anyone. It also meant
five simultaneous uploads spawned five simultaneous ffmpeg processes.

Instead, an upload inserts a row into ``transcode_jobs`` and returns. A small
pool of worker tasks claims jobs one at a time, so concurrency is bounded by
``TRANSCODE_CONCURRENCY`` and a job that dies with the process is picked up
again on the next start.
"""

import asyncio
import logging

import aiosqlite

from app.config import Config
from app.utils import now_utc

logger = logging.getLogger(__name__)

# How long a worker sleeps when it finds no queued job. Uploads are infrequent
# on a community site, so polling this slowly costs nothing and keeps the
# implementation free of any external broker.
POLL_SECONDS = 3.0

# Video statuses that mean "work is still outstanding".
PENDING_VIDEO_STATUSES = ("uploading", "processing")

_workers: list = []
_stop = asyncio.Event()


async def _connect() -> aiosqlite.Connection:
    """Open a worker-owned connection with the same pragmas as get_db()."""
    db = await aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 30000")
    return db


# ---------------------------------------------------------------------------
# Queue operations (callable from request handlers)
# ---------------------------------------------------------------------------

async def enqueue(db, video_id: int) -> int:
    """Queue a video for transcoding. Returns the new job id."""
    cursor = await db.execute(
        "INSERT INTO transcode_jobs (video_id, status, created_at) VALUES (?, 'queued', ?)",
        (video_id, now_utc().isoformat()),
    )
    await db.commit()
    return cursor.lastrowid


async def retry_video(db, video_id: int) -> bool:
    """Requeue a failed video. Returns False if there is already work pending."""
    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM transcode_jobs WHERE video_id = ? AND status IN ('queued', 'running')",
        (video_id,),
    )
    if (await cursor.fetchone())["c"]:
        return False

    # Clear the previous attempt's rendition rows so the retry starts clean;
    # transcode_video() inserts a fresh set.
    await db.execute("DELETE FROM video_renditions WHERE video_id = ?", (video_id,))
    await db.execute("UPDATE videos SET status = 'uploading' WHERE id = ?", (video_id,))
    await enqueue(db, video_id)
    return True


async def requeue_orphans(db) -> int:
    """Recover work stranded by a restart.

    Two cases: a job left in 'running' because the process died mid-transcode,
    and a video in a pending state with no job row at all (either it predates
    this queue, or the request died between the INSERT and the enqueue).
    """
    recovered = 0

    cursor = await db.execute(
        "UPDATE transcode_jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
    )
    recovered += cursor.rowcount or 0

    cursor = await db.execute(
        f"""
        SELECT v.id FROM videos v
        WHERE v.status IN ({','.join('?' * len(PENDING_VIDEO_STATUSES))})
          AND NOT EXISTS (
              SELECT 1 FROM transcode_jobs j
              WHERE j.video_id = v.id AND j.status IN ('queued', 'running')
          )
        """,
        PENDING_VIDEO_STATUSES,
    )
    orphans = [row["id"] for row in await cursor.fetchall()]
    for video_id in orphans:
        await db.execute(
            "INSERT INTO transcode_jobs (video_id, status, created_at) VALUES (?, 'queued', ?)",
            (video_id, now_utc().isoformat()),
        )
    recovered += len(orphans)

    await db.commit()
    if recovered:
        logger.info("Requeued %d stranded transcode job(s) on startup", recovered)
    return recovered


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def _claim_job(db):
    """Atomically take the oldest queued job, or return None.

    The conditional UPDATE is the concurrency gate: two workers racing for the
    same row means exactly one of them sees rowcount == 1.
    """
    while True:
        cursor = await db.execute(
            "SELECT id, video_id, attempts FROM transcode_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        )
        row = await cursor.fetchone()
        if not row:
            return None

        cursor = await db.execute(
            """
            UPDATE transcode_jobs
            SET status = 'running', started_at = ?, attempts = attempts + 1
            WHERE id = ? AND status = 'queued'
            """,
            (now_utc().isoformat(), row["id"]),
        )
        await db.commit()
        if cursor.rowcount == 1:
            return {"id": row["id"], "video_id": row["video_id"], "attempts": row["attempts"] + 1}
        # Another worker won the race; look for the next one.


async def _finish_job(db, job_id: int, status: str, error: str = None):
    await db.execute(
        "UPDATE transcode_jobs SET status = ?, finished_at = ?, last_error = ? WHERE id = ?",
        (status, now_utc().isoformat(), error, job_id),
    )
    await db.commit()


async def _run_job(db, job) -> None:
    # Imported here rather than at module scope: videos.py imports this module
    # to enqueue work, so a top-level import would be circular.
    from app.videos import transcode_video

    cursor = await db.execute(
        "SELECT id, uuid, raw_path, status FROM videos WHERE id = ?", (job["video_id"],)
    )
    video = await cursor.fetchone()
    if not video:
        # The video was deleted while the job was queued. Nothing to do.
        await _finish_job(db, job["id"], "done", "video no longer exists")
        return

    if not video["raw_path"]:
        # The original was purged after a successful transcode, so there is
        # nothing left to work from. Retrying cannot help, so fail immediately
        # rather than burning every remaining attempt on it.
        await _finish_job(db, job["id"], "failed", "the original upload is no longer on disk")
        await db.execute("UPDATE videos SET status = 'failed' WHERE id = ?", (video["id"],))
        await db.commit()
        return

    try:
        await transcode_video(video["id"], video["raw_path"], video["uuid"])
    except Exception as exc:  # noqa: BLE001 - a job must never kill the worker
        logger.exception("Transcode job %s raised", job["id"])
        await _handle_failure(db, job, str(exc))
        return

    cursor = await db.execute("SELECT status FROM videos WHERE id = ?", (job["video_id"],))
    row = await cursor.fetchone()
    final_status = row["status"] if row else "failed"

    if final_status == "ready":
        await _finish_job(db, job["id"], "done")
        await _notify_subscribers(db, job["video_id"])
    else:
        await _handle_failure(db, job, "transcode produced no usable renditions")


async def _notify_subscribers(db, video_id: int) -> None:
    """Queue new-video emails. Never let this break a completed transcode."""
    from app import notifications

    try:
        await notifications.queue_new_video_notifications(db, video_id)
    except Exception:  # noqa: BLE001 - the video is ready either way
        logger.exception("Could not queue notifications for video %s", video_id)


async def _handle_failure(db, job, error: str) -> None:
    """Requeue while attempts remain, otherwise mark the video failed."""
    if job["attempts"] < Config.TRANSCODE_MAX_ATTEMPTS:
        await db.execute(
            "UPDATE transcode_jobs SET status = 'queued', started_at = NULL, last_error = ? WHERE id = ?",
            (error, job["id"]),
        )
        await db.execute(
            "UPDATE videos SET status = 'uploading' WHERE id = ?", (job["video_id"],)
        )
        await db.commit()
        logger.warning(
            "Transcode job %s failed (attempt %s/%s), requeued: %s",
            job["id"], job["attempts"], Config.TRANSCODE_MAX_ATTEMPTS, error,
        )
        return

    await _finish_job(db, job["id"], "failed", error)
    await db.execute("UPDATE videos SET status = 'failed' WHERE id = ?", (job["video_id"],))
    await db.commit()
    logger.error("Transcode job %s failed permanently: %s", job["id"], error)


async def _worker_loop(worker_id: int) -> None:
    db = await _connect()
    try:
        while not _stop.is_set():
            try:
                job = await _claim_job(db)
            except Exception:  # noqa: BLE001 - a transient DB error must not kill the worker
                logger.exception("Transcode worker %s failed to claim a job", worker_id)
                job = None

            if job is None:
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            logger.info("Transcode worker %s picked up job %s", worker_id, job["id"])
            await _run_job(db, job)
    finally:
        await db.close()


async def start_workers() -> None:
    """Recover stranded work, then start the worker pool."""
    _stop.clear()
    db = await _connect()
    try:
        await requeue_orphans(db)
    finally:
        await db.close()

    for i in range(Config.TRANSCODE_CONCURRENCY):
        _workers.append(asyncio.create_task(_worker_loop(i + 1), name=f"transcode-worker-{i + 1}"))
    logger.info("Started %d transcode worker(s)", len(_workers))


async def stop_workers() -> None:
    """Signal the workers to finish and wait briefly for the current job."""
    _stop.set()
    if not _workers:
        return
    # A transcode in flight can take minutes; do not block shutdown on it. The
    # job stays 'running' and requeue_orphans() picks it up next start.
    done, pending = await asyncio.wait(_workers, timeout=5.0)
    for task in pending:
        task.cancel()
    _workers.clear()
