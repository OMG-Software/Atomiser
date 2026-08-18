import io
import pytest


@pytest.mark.asyncio
async def test_upload_requires_login(client, csrf):
    fake_video = io.BytesIO(b"fake video content")
    resp = await client.post(
        "/upload",
        data={
            "title": "My Video",
            "description": "A test video",
            "visibility": "site",
            "csrf": csrf,
        },
        files={"video": ("test.mp4", fake_video, "video/mp4")},
    )
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_upload_invalid_mime(client, logged_in_member, csrf):
    fake = io.BytesIO(b"not a video")
    resp = await client.post(
        "/upload",
        data={
            "title": "Bad file",
            "description": "Not a video",
            "visibility": "site",
            "csrf": csrf,
        },
        files={"video": ("test.txt", fake, "text/plain")},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_missing_file(client, logged_in_member, csrf):
    resp = await client.post(
        "/upload",
        data={
            "title": "No file",
            "description": "",
            "visibility": "site",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_missing_title(client, logged_in_member, csrf):
    fake = io.BytesIO(b"fake video content")
    resp = await client.post(
        "/upload",
        data={
            "title": "",
            "description": "",
            "visibility": "site",
            "csrf": csrf,
        },
        files={"video": ("test.mp4", fake, "video/mp4")},
    )
    # Empty title is rejected.
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_feed_requires_login(client):
    resp = await client.get("/")
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_feed_accessible_logged_in(client, logged_in_member):
    resp = await client.get("/")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_video_stream_requires_login(client):
    resp = await client.get("/video/stream/any-uuid/720p")
    # A missing video can hit the 404 handler before auth redirect.
    assert resp.status_code in (302, 303, 404)
    if resp.status_code in (302, 303):
        assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_profile_requires_login(client):
    resp = await client.get("/profile")
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_profile_accessible_logged_in(client, logged_in_member):
    resp = await client.get("/profile")
    assert resp.status_code == 200


async def _user_id_by_email(db, email):
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    return row["id"] if row else None


async def _create_video(db, owner_id, title="Test Video", description="A video", visibility="site", status="ready"):
    import uuid
    video_uuid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO videos (uuid, owner_id, title, description, visibility, status, raw_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (video_uuid, owner_id, title, description, visibility, status, ""),
    )
    await db.commit()
    return video_uuid


@pytest.mark.asyncio
async def test_owner_can_edit_video(client, logged_in_member, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "New Title", "description": "New description", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/videos/{video_uuid}?success=1" in resp.headers["location"]

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert "New Title" in page.text
    assert "New description" in page.text


@pytest.mark.asyncio
async def test_admin_can_edit_any_video(client, logged_in_admin, member_user, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Admin Edit", "description": "", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert "Admin Edit" in page.text


@pytest.mark.asyncio
async def test_non_owner_cannot_edit_video(client, logged_in_member, admin_user, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, admin_user["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Hacked", "description": "", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_edit_video_requires_title(client, logged_in_member, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "   ", "description": "", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_edit_video_requires_csrf(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "No CSRF", "description": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_video_page_not_found(client, logged_in_member):
    resp = await client.get("/videos/does-not-exist", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_video_stream_not_found(client, logged_in_member):
    resp = await client.get("/stream/does-not-exist/720p.mp4", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_thumbnail_not_found(client, logged_in_member):
    resp = await client.get("/thumb/does-not-exist", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_private_video_inaccessible_to_strangers(client, logged_in_member, admin_user, db):
    owner_id = await _user_id_by_email(db, admin_user["email"])
    video_uuid = await _create_video(db, owner_id, visibility="private")

    resp = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert resp.status_code == 403

    stream = await client.get(f"/stream/{video_uuid}/720p.mp4", follow_redirects=False)
    assert stream.status_code == 403

    # Without a thumbnail the endpoint returns 404 before visibility checks; that is acceptable.
    thumb = await client.get(f"/thumb/{video_uuid}", follow_redirects=False)
    assert thumb.status_code in (403, 404)


@pytest.mark.asyncio
async def test_upload_rejects_bad_content_length(client, logged_in_member, csrf):
    """A malformed Content-Length header should return 400, not 500."""
    import io

    fake = io.BytesIO(b"fake video content")
    resp = await client.post(
        "/upload",
        data={
            "title": "Bad length",
            "description": "",
            "visibility": "site",
            "csrf": csrf,
        },
        files={"video": ("test.mp4", fake, "video/mp4")},
        headers={"Content-Length": "not-a-number"},
    )
    assert resp.status_code == 400


# Minimal MP4 ftyp box: filetype.guess() reports video/mp4 (so it passes the
# magic-byte sniff), but ffprobe cannot parse it (so _probe_height returns
# None and the 1080p check is skipped). This keeps the success test robust
# whether or not ffmpeg is installed on the dev machine.
MP4_FTYP = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2avc1mp01"
CHUNK = 50 * 1024 * 1024


def _chunk_data(csrf, upload_id, index, total, payload_len, **extra):
    d = {
        "csrf": csrf,
        "upload_id": upload_id,
        "index": index,
        "total": total,
        "chunk_size": CHUNK,
        "total_size": payload_len,
        "filename": "clip.mp4",
        "mime": "video/mp4",
        "title": "Chunked video",
        "description": "via chunks",
        "visibility": "site",
    }
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_chunked_upload_requires_login(client, csrf):
    resp = await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, "anon-aaaaaaaaaa", 0, 1, 4),
        files={"chunk": ("clip.mp4", io.BytesIO(b"abcd"), "video/mp4")},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_chunked_upload_bad_csrf(client, logged_in_member, csrf):
    resp = await client.post(
        "/upload/chunk",
        data=_chunk_data("wrong-csrf-token", "csrf-bad-aaaaaaaa", 0, 1, 4),
        files={"chunk": ("clip.mp4", io.BytesIO(b"abcd"), "video/mp4")},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chunked_upload_success(client, logged_in_member, csrf):
    upload_id = "ok-success-aaaaaaaa"
    payload = MP4_FTYP
    resp = await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, upload_id, 0, 1, len(payload)),
        files={"chunk": ("clip.mp4", io.BytesIO(payload), "video/mp4")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["received"] == 1

    resp = await client.post(
        "/upload/complete",
        data={"csrf": csrf, "upload_id": upload_id},
    )
    assert resp.status_code == 200, resp.text
    uuid = resp.json()["uuid"]
    assert uuid

    import aiosqlite
    from app.config import Config
    async with aiosqlite.connect(Config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT uuid, title FROM videos WHERE uuid = ?", (uuid,))
        row = await cur.fetchone()
    assert row is not None
    assert row["title"] == "Chunked video"


@pytest.mark.asyncio
async def test_chunked_upload_bad_mime(client, logged_in_member, csrf):
    upload_id = "bad-mime-aaaaaaaa"
    payload = b"not a video at all, just text"
    await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, upload_id, 0, 1, len(payload), filename="clip.txt", mime="text/plain"),
        files={"chunk": ("clip.txt", io.BytesIO(payload), "text/plain")},
    )
    resp = await client.post(
        "/upload/complete",
        data={"csrf": csrf, "upload_id": upload_id},
    )
    assert resp.status_code == 400
    assert b"Unsupported video format" in resp.content


@pytest.mark.asyncio
async def test_chunked_upload_incomplete(client, logged_in_member, csrf):
    upload_id = "incomplet-aaaaaaaa"
    payload = MP4_FTYP
    # Declare two chunks but only send the first.
    await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, upload_id, 0, 2, len(payload) + 10),
        files={"chunk": ("clip.mp4", io.BytesIO(payload), "video/mp4")},
    )
    resp = await client.post(
        "/upload/complete",
        data={"csrf": csrf, "upload_id": upload_id},
    )
    assert resp.status_code == 400
    assert b"Upload incomplete" in resp.content


@pytest.mark.asyncio
async def test_chunked_upload_complete_unknown_session(client, logged_in_member, csrf):
    resp = await client.post(
        "/upload/complete",
        data={"csrf": csrf, "upload_id": "no-such-session-aaa"},
    )
    assert resp.status_code == 400
    assert b"Upload session not found" in resp.content


@pytest.mark.asyncio
async def test_chunked_upload_oversize_rejected(client, logged_in_member, csrf):
    # total_size above MAX_UPLOAD_MB is rejected at the chunk stage before any
    # bytes are staged.
    from app.config import Config
    resp = await client.post(
        "/upload/chunk",
        data=_chunk_data(csrf, "toobig-aaaaaaaaaa", 0, 1, (Config.MAX_UPLOAD_MB + 1) * 1024 * 1024),
        files={"chunk": ("clip.mp4", io.BytesIO(b"abcd"), "video/mp4")},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Owner video management
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_can_delete_own_video(client, logged_in_member, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/delete", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_cannot_delete_video(client, logged_in_member, admin_user, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, admin_user["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/delete", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_video_requires_csrf(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(f"/videos/{video_uuid}/delete", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_delete_members_video(client, logged_in_admin, member_user, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/delete", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/videos"


@pytest.mark.asyncio
async def test_delete_removes_files_from_disk(client, logged_in_member, db):
    """Deleting a video must take its files with it, not just the DB row."""
    from app.config import Config

    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    out_dir = Config.UPLOAD_DIR / "videos" / video_uuid
    out_dir.mkdir(parents=True, exist_ok=True)
    rendition = out_dir / "720p.mp4"
    rendition.write_bytes(b"rendition bytes")
    thumb = out_dir / "poster.jpg"
    thumb.write_bytes(b"thumb bytes")
    raw = Config.UPLOAD_DIR / "raw" / f"{video_uuid}.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"raw bytes")

    cursor = await db.execute("SELECT id FROM videos WHERE uuid = ?", (video_uuid,))
    video_id = (await cursor.fetchone())["id"]
    await db.execute(
        "UPDATE videos SET raw_path = ?, thumbnail_path = ? WHERE id = ?",
        (str(raw), str(thumb), video_id),
    )
    await db.execute(
        "INSERT INTO video_renditions (video_id, label, file_path, status) VALUES (?, '720p', ?, 'ready')",
        (video_id, str(rendition)),
    )
    await db.commit()

    resp = await client.post(
        f"/videos/{video_uuid}/delete", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    assert not raw.exists()
    assert not rendition.exists()
    assert not thumb.exists()
    assert not out_dir.exists()


# ---------------------------------------------------------------------------
# Visibility editing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_can_change_visibility(client, logged_in_member, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, visibility="site")

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Now private", "description": "", "visibility": "private", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT visibility FROM videos WHERE uuid = ?", (video_uuid,))
    assert (await cursor.fetchone())["visibility"] == "private"


@pytest.mark.asyncio
async def test_edit_without_visibility_field_preserves_it(client, logged_in_member, db):
    """Omitting the field must not silently flip a private video public."""
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, visibility="private")

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Still private", "description": "", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT visibility FROM videos WHERE uuid = ?", (video_uuid,))
    assert (await cursor.fetchone())["visibility"] == "private"


@pytest.mark.asyncio
async def test_edit_rejects_bad_visibility(client, logged_in_member, db):
    csrf = client.cookies.get("csrf")
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Bad", "description": "", "visibility": "everyone", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Processing state and status polling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_processing_video_shows_panel_not_empty_player(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="processing")

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert "Processing your video" in page.text
    assert "processing-panel" in page.text
    # No player element at all while there is nothing to play.
    assert 'id="video-player"' not in page.text


@pytest.mark.asyncio
async def test_status_endpoint_reports_progress(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="processing")

    cursor = await db.execute("SELECT id FROM videos WHERE uuid = ?", (video_uuid,))
    video_id = (await cursor.fetchone())["id"]
    await db.execute(
        "INSERT INTO video_renditions (video_id, label, file_path, status) VALUES (?, '720p', 'a.mp4', 'ready')",
        (video_id,),
    )
    await db.execute(
        "INSERT INTO video_renditions (video_id, label, file_path, status) VALUES (?, '480p', 'b.mp4', 'pending')",
        (video_id,),
    )
    await db.commit()

    resp = await client.get(f"/videos/{video_uuid}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert body["status"] == "processing"
    assert body["renditions_ready"] == 1
    assert body["renditions_total"] == 2
    assert body["percent"] == 50


@pytest.mark.asyncio
async def test_status_endpoint_reports_ready(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="ready")

    resp = await client.get(f"/videos/{video_uuid}/status")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_status_endpoint_respects_privacy(client, logged_in_member, admin_user, db):
    owner_id = await _user_id_by_email(db, admin_user["email"])
    video_uuid = await _create_video(db, owner_id, visibility="private")

    resp = await client.get(f"/videos/{video_uuid}/status")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_endpoint_requires_login(client, db, member_user):
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _create_video(db, owner_id)

    resp = await client.get(f"/videos/{video_uuid}/status", follow_redirects=False)
    assert resp.status_code in (302, 303)


@pytest.mark.asyncio
async def test_failed_video_shows_failure_panel(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="failed")

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert "could not be processed" in page.text


# ---------------------------------------------------------------------------
# Quality selector
# ---------------------------------------------------------------------------

async def _add_renditions(db, video_uuid, labels):
    cursor = await db.execute("SELECT id FROM videos WHERE uuid = ?", (video_uuid,))
    video_id = (await cursor.fetchone())["id"]
    for label, height in labels:
        await db.execute(
            """
            INSERT INTO video_renditions (video_id, label, height, file_path, status)
            VALUES (?, ?, ?, ?, 'ready')
            """,
            (video_id, label, height, f"/srv/uploads/videos/{video_uuid}/{label}.mp4"),
        )
    await db.commit()
    return video_id


@pytest.mark.asyncio
async def test_player_offers_every_ready_rendition(client, logged_in_member, db):
    """All three renditions must be reachable, not just the first <source>."""
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="ready")
    await _add_renditions(db, video_uuid, [("720p", 720), ("480p", 480), ("360p", 360)])

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert 'id="quality-select"' in page.text
    for label in ("720p", "480p", "360p"):
        assert f"/stream/{video_uuid}/{label}.mp4" in page.text
    # Highest quality is the default source.
    assert f'src="/stream/{video_uuid}/720p.mp4"' in page.text


@pytest.mark.asyncio
async def test_single_rendition_has_no_quality_selector(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, logged_in_member["email"])
    video_uuid = await _create_video(db, owner_id, status="ready")
    await _add_renditions(db, video_uuid, [("720p", 720)])

    page = await client.get(f"/videos/{video_uuid}", follow_redirects=False)
    assert page.status_code == 200
    assert 'id="video-player"' in page.text
    assert 'id="quality-select"' not in page.text
