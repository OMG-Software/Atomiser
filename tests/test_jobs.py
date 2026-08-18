"""Tests for the durable transcode queue in app/jobs.py."""

import uuid

import pytest

from app import jobs


async def _create_video(db, owner_id, status="uploading"):
    video_uuid = str(uuid.uuid4())
    cursor = await db.execute(
        "INSERT INTO videos (uuid, owner_id, title, status, raw_path) VALUES (?, ?, ?, ?, ?)",
        (video_uuid, owner_id, "Queued video", status, "/tmp/raw.mp4"),
    )
    await db.commit()
    return cursor.lastrowid, video_uuid


async def _user_id(db, email="member@example.com"):
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    return row["id"]


@pytest.mark.asyncio
async def test_enqueue_creates_queued_job(db, member_user):
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)

    job_id = await jobs.enqueue(db, video_id)

    cursor = await db.execute("SELECT status, attempts FROM transcode_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 0


@pytest.mark.asyncio
async def test_claim_job_marks_running_and_counts_attempt(db, member_user):
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    await jobs.enqueue(db, video_id)

    job = await jobs._claim_job(db)
    assert job is not None
    assert job["video_id"] == video_id
    assert job["attempts"] == 1

    cursor = await db.execute("SELECT status FROM transcode_jobs WHERE id = ?", (job["id"],))
    assert (await cursor.fetchone())["status"] == "running"


@pytest.mark.asyncio
async def test_claim_job_returns_none_when_queue_empty(db, member_user):
    assert await jobs._claim_job(db) is None


@pytest.mark.asyncio
async def test_a_job_is_only_claimed_once(db, member_user):
    """The conditional UPDATE is the gate against two workers taking one job."""
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    await jobs.enqueue(db, video_id)

    first = await jobs._claim_job(db)
    second = await jobs._claim_job(db)

    assert first is not None
    assert second is None


async def _expire_lease(db, job_id):
    """Simulate the owning process dying: its lease lapses."""
    from datetime import timedelta

    from app.utils import now_utc

    await db.execute(
        "UPDATE transcode_jobs SET lease_expires_at = ? WHERE id = ?",
        ((now_utc() - timedelta(seconds=1)).isoformat(), job_id),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_requeue_orphans_recovers_jobs_with_an_expired_lease(db, member_user):
    """A job left 'running' by a crash must be picked back up, not stranded."""
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id, status="processing")
    await jobs.enqueue(db, video_id)
    claimed = await jobs._claim_job(db)
    await _expire_lease(db, claimed["id"])

    recovered = await jobs.requeue_orphans(db)
    assert recovered >= 1

    cursor = await db.execute("SELECT status FROM transcode_jobs WHERE id = ?", (claimed["id"],))
    assert (await cursor.fetchone())["status"] == "queued"


@pytest.mark.asyncio
async def test_requeue_orphans_leaves_live_jobs_alone(db, member_user):
    """The bug this guards: a second process starting up during a rolling
    restart used to requeue a job another process was still transcoding, so two
    ffmpeg runs wrote the same rendition paths."""
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id, status="processing")
    await jobs.enqueue(db, video_id)
    claimed = await jobs._claim_job(db)

    # Lease is still live, so a starting process must not touch it.
    await jobs.requeue_orphans(db)

    cursor = await db.execute(
        "SELECT status, worker_id FROM transcode_jobs WHERE id = ?", (claimed["id"],)
    )
    row = await cursor.fetchone()
    assert row["status"] == "running"
    assert row["worker_id"] == jobs.WORKER_ID

    # And it cannot be claimed out from under the running worker either.
    assert await jobs._claim_job(db) is None


@pytest.mark.asyncio
async def test_claim_records_worker_and_lease(db, member_user):
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    await jobs.enqueue(db, video_id)

    job = await jobs._claim_job(db)

    cursor = await db.execute(
        "SELECT worker_id, lease_expires_at FROM transcode_jobs WHERE id = ?", (job["id"],)
    )
    row = await cursor.fetchone()
    assert row["worker_id"] == jobs.WORKER_ID

    from app.utils import now_utc

    assert row["lease_expires_at"] > now_utc().isoformat()


@pytest.mark.asyncio
async def test_requeue_orphans_queues_videos_with_no_job(db, member_user):
    """A video stuck 'uploading' with no job row at all gets one."""
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id, status="uploading")

    await jobs.requeue_orphans(db)

    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM transcode_jobs WHERE video_id = ? AND status = 'queued'",
        (video_id,),
    )
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_requeue_orphans_ignores_ready_videos(db, member_user):
    owner_id = await _user_id(db)
    await _create_video(db, owner_id, status="ready")

    await jobs.requeue_orphans(db)

    cursor = await db.execute("SELECT COUNT(*) AS c FROM transcode_jobs")
    assert (await cursor.fetchone())["c"] == 0


@pytest.mark.asyncio
async def test_retry_video_requeues_and_clears_renditions(db, member_user):
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id, status="failed")
    await db.execute(
        "INSERT INTO video_renditions (video_id, label, file_path, status) VALUES (?, '720p', 'a.mp4', 'failed')",
        (video_id,),
    )
    await db.commit()

    assert await jobs.retry_video(db, video_id) is True

    cursor = await db.execute("SELECT status FROM videos WHERE id = ?", (video_id,))
    assert (await cursor.fetchone())["status"] == "uploading"

    cursor = await db.execute("SELECT COUNT(*) AS c FROM video_renditions WHERE video_id = ?", (video_id,))
    assert (await cursor.fetchone())["c"] == 0

    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM transcode_jobs WHERE video_id = ? AND status = 'queued'", (video_id,)
    )
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_retry_video_refuses_when_already_queued(db, member_user):
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    await jobs.enqueue(db, video_id)

    assert await jobs.retry_video(db, video_id) is False


@pytest.mark.asyncio
async def test_failure_requeues_until_attempts_exhausted(db, member_user):
    from app.config import Config

    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    job_id = await jobs.enqueue(db, video_id)

    # One attempt short of the limit: the job goes back on the queue.
    await jobs._handle_failure(
        db, {"id": job_id, "video_id": video_id, "attempts": Config.TRANSCODE_MAX_ATTEMPTS - 1}, "boom"
    )
    cursor = await db.execute("SELECT status, last_error FROM transcode_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    assert row["status"] == "queued"
    assert row["last_error"] == "boom"

    # At the limit it fails for good and the video is marked failed.
    await jobs._handle_failure(
        db, {"id": job_id, "video_id": video_id, "attempts": Config.TRANSCODE_MAX_ATTEMPTS}, "boom"
    )
    cursor = await db.execute("SELECT status FROM transcode_jobs WHERE id = ?", (job_id,))
    assert (await cursor.fetchone())["status"] == "failed"

    cursor = await db.execute("SELECT status FROM videos WHERE id = ?", (video_id,))
    assert (await cursor.fetchone())["status"] == "failed"


@pytest.mark.asyncio
async def test_job_fails_fast_when_original_is_gone(db, member_user):
    """Retrying cannot conjure the source file back, so do not burn attempts."""
    owner_id = await _user_id(db)
    video_id, _ = await _create_video(db, owner_id)
    await db.execute("UPDATE videos SET raw_path = NULL WHERE id = ?", (video_id,))
    await db.commit()
    await jobs.enqueue(db, video_id)

    job = await jobs._claim_job(db)
    await jobs._run_job(db, job)

    cursor = await db.execute("SELECT status, last_error FROM transcode_jobs WHERE id = ?", (job["id"],))
    row = await cursor.fetchone()
    assert row["status"] == "failed"
    assert "no longer on disk" in row["last_error"]

    cursor = await db.execute("SELECT status FROM videos WHERE id = ?", (video_id,))
    assert (await cursor.fetchone())["status"] == "failed"


@pytest.mark.asyncio
async def test_upload_enqueues_a_job(client, logged_in_member, csrf):
    """A completed chunked upload must leave a job behind for the worker."""
    import io

    from tests.test_videos import MP4_FTYP, _chunk_data

    upload_id = "jobqueue-aaaaaaaa"
    await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, upload_id, 0, 1, len(MP4_FTYP)),
        files={"chunk": ("clip.mp4", io.BytesIO(MP4_FTYP), "video/mp4")},
    )
    resp = await client.post("/upload/complete", data={"csrf": csrf, "upload_id": upload_id})
    assert resp.status_code == 200
    video_uuid = resp.json()["uuid"]

    import aiosqlite

    from app.config import Config

    async with aiosqlite.connect(Config.DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT j.status FROM transcode_jobs j
            JOIN videos v ON j.video_id = v.id
            WHERE v.uuid = ?
            """,
            (video_uuid,),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["status"] == "queued"
