import re
from datetime import datetime, timedelta, timezone

import pytest


async def _user_id_by_email(db, email):
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    return row["id"] if row else None


@pytest.mark.asyncio
async def test_configurator_can_change_role(client, logged_in_configurator, member_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, member_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "admin", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"

    cursor = await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    assert row["role"] == "admin"

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "member", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cursor = await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    assert row["role"] == "member"


@pytest.mark.asyncio
async def test_configurator_cannot_change_bootstrap_role(client, logged_in_configurator, bootstrap_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, bootstrap_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "member", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_change_roles(client, logged_in_admin, member_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, member_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "admin", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_role_change_requires_csrf(client, logged_in_configurator, member_user, db):
    user_id = await _user_id_by_email(db, member_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_role_change_invalid_role(client, logged_in_configurator, member_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, member_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "configurator", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_configurator_cannot_demote_another_configurator(client, logged_in_configurator, configurator_user, db):
    """A Configurator cannot change another Configurator's role."""
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, configurator_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "member", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_user_self(client, logged_in_admin, db):
    """A user cannot delete their own account."""
    csrf = client.cookies.get("csrf")
    cur = await db.execute("SELECT id FROM users WHERE email = ?", ("admin@example.com",))
    user_id = (await cur.fetchone())["id"]

    resp = await client.post(
        f"/admin/users/{user_id}/delete",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_cannot_delete_configurator(client, logged_in_admin, configurator_user, db):
    """A non-Configurator admin cannot delete a Configurator."""
    csrf = client.cookies.get("csrf")
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (configurator_user["email"],))
    user_id = (await cur.fetchone())["id"]

    resp = await client.post(
        f"/admin/users/{user_id}/delete",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_user_missing_csrf(client, logged_in_admin, member_user, db):
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (member_user["email"],))
    user_id = (await cur.fetchone())["id"]

    resp = await client.post(
        f"/admin/users/{user_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_nonexistent_video(client, logged_in_admin):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/videos/does-not-exist/delete",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_video_missing_csrf(client, logged_in_admin, member_user, db):
    from tests.test_videos import _create_video

    cur = await db.execute("SELECT id FROM users WHERE email = ?", (member_user["email"],))
    owner_id = (await cur.fetchone())["id"]
    video_uuid = await _create_video(db, owner_id)

    resp = await client.post(
        f"/admin/videos/{video_uuid}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_dashboard_active_invites_excludes_recently_expired(client, db, logged_in_admin):
    """The active-invites stat must not count an invite that just expired.

    Regression guard for the date-comparison bug: stored expires_at uses ISO8601
    with a 'T' separator and +00:00 offset, while SQLite's datetime('now')
    produces 'YYYY-MM-DD HH:MM:SS'. Comparing those mixed formats misclassifies
    same-date expiries as active. The fix compares against a bound UTC timestamp
    in the same format.
    """
    from app.utils import generate_token, hash_token

    # Expired ~1 minute ago (same UTC date as now in the common case).
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(generate_token()), 1, 1, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()),
    )
    # One genuinely active invite.
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(generate_token()), 1, 1, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()),
    )
    await db.commit()

    resp = await client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 200
    # Exactly one active invite; the expired one must not be counted.
    assert re.search(
        r"<span class=\"stat-value\">1</span>\s*<span class=\"stat-label\">Active invites</span>",
        resp.text,
    )


async def _user_id(db, email):
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    return (await cur.fetchone())["id"]


@pytest.mark.asyncio
async def test_admin_reset_password_shows_password(client, logged_in_admin, member_user, db):
    """Resetting a member's password renders a one-time plaintext that then works to log in."""
    csrf = client.cookies.get("csrf")
    user_id = await _user_id(db, member_user["email"])

    resp = await client.post(
        f"/admin/users/{user_id}/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Password reset" in resp.text
    # A 16-char policy-compliant password is rendered inside the result page.
    m = re.search(r'<code id="new-password"[^>]*>([A-Za-z0-9]{16})</code>', resp.text)
    assert m, resp.text
    new_password = m.group(1)

    # The stored hash verifies against the generated plaintext, not the old one.
    from app.auth import verify_password
    cur = await db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    row = await cur.fetchone()
    assert await verify_password(new_password, row["password_hash"])
    assert not await verify_password(member_user["password"], row["password_hash"])

    # The generated password logs the member in.
    await client.get("/auth/logout", follow_redirects=False)
    login_resp = await client.post(
        "/auth/login",
        data={"email": member_user["email"], "password": new_password, "csrf": csrf, "next": "/"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303


@pytest.mark.asyncio
async def test_admin_cannot_reset_bootstrap(client, logged_in_admin, bootstrap_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id(db, bootstrap_user["email"])
    resp = await client.post(
        f"/admin/users/{user_id}/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_reset_configurator(client, logged_in_admin, configurator_user, db):
    """A plain admin must not obtain a Configurator's password (privilege escalation)."""
    csrf = client.cookies.get("csrf")
    user_id = await _user_id(db, configurator_user["email"])
    resp = await client.post(
        f"/admin/users/{user_id}/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_configurator_can_reset_configurator(client, logged_in_configurator, configurator_user, db):
    """A Configurator resetting their own kind is allowed — need a second configurator to avoid self-reset."""
    from app.auth import hash_password
    pwd_hash = await hash_password("OtherConfig123456")
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("otherconfig@example.com", pwd_hash, "Other Config", "configurator", 0),
    )
    await db.commit()
    other_id = await _user_id(db, "otherconfig@example.com")

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/admin/users/{other_id}/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Password reset" in resp.text


@pytest.mark.asyncio
async def test_admin_cannot_reset_self(client, logged_in_admin, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id(db, "admin@example.com")
    resp = await client.post(
        f"/admin/users/{user_id}/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_requires_csrf(client, logged_in_admin, member_user, db):
    user_id = await _user_id(db, member_user["email"])
    resp = await client.post(
        f"/admin/users/{user_id}/reset-password",
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reset_password_unknown_user(client, logged_in_admin):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/users/99999/reset-password",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit log viewer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_page_requires_admin(client, logged_in_member):
    resp = await client.get("/admin/audit", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_audit_page_shows_login_entries(client, logged_in_admin):
    """Logging in writes an audit row, so the viewer must show it."""
    resp = await client.get("/admin/audit")
    assert resp.status_code == 200
    assert "login" in resp.text
    assert "admin@example.com" in resp.text


@pytest.mark.asyncio
async def test_audit_filter_by_action(client, logged_in_admin, db):
    await db.execute(
        "INSERT INTO audit_log (user_id, action, target_type, target_id) VALUES (?, 'video_deleted', 'video', 'abc')",
        (await _user_id_by_email(db, "admin@example.com"),),
    )
    await db.commit()

    resp = await client.get("/admin/audit?action=video_deleted")
    assert resp.status_code == 200
    assert "video_deleted" in resp.text
    # The unrelated login entry must be filtered out of the table body.
    assert resp.text.count("badge-success\">login<") == 0


@pytest.mark.asyncio
async def test_audit_filter_by_unknown_action_shows_empty_state(client, logged_in_admin):
    resp = await client.get("/admin/audit?action=nothing_matches_this")
    assert resp.status_code == 200
    assert "Nothing to show" in resp.text


@pytest.mark.asyncio
async def test_role_change_is_audited(client, logged_in_configurator, member_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, member_user["email"])
    await client.post(
        f"/admin/users/{user_id}/role",
        data={"role": "admin", "csrf": csrf},
        follow_redirects=False,
    )

    cursor = await db.execute("SELECT action, target_id FROM audit_log WHERE action = 'role_changed'")
    row = await cursor.fetchone()
    assert row is not None
    assert str(user_id) in row["target_id"]


# ---------------------------------------------------------------------------
# Force logout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_can_force_logout_user(client, logged_in_admin, member_user, db):
    from app.auth import create_session

    user_id = await _user_id_by_email(db, member_user["email"])
    await create_session(db, user_id, "10.0.0.1", "pytest")

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/admin/users/{user_id}/logout", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,))
    assert (await cursor.fetchone())["c"] == 0


@pytest.mark.asyncio
async def test_force_logout_requires_csrf(client, logged_in_admin, member_user, db):
    user_id = await _user_id_by_email(db, member_user["email"])
    resp = await client.post(f"/admin/users/{user_id}/logout", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_force_logout(client, logged_in_member, admin_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, admin_user["email"])
    resp = await client.post(
        f"/admin/users/{user_id}/logout", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_force_logout_configurator(client, logged_in_admin, configurator_user, db):
    csrf = client.cookies.get("csrf")
    user_id = await _user_id_by_email(db, configurator_user["email"])
    resp = await client.post(
        f"/admin/users/{user_id}/logout", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Transcode retry
# ---------------------------------------------------------------------------

async def _failed_video(db, owner_id, raw_path="/tmp/raw.mp4"):
    import uuid as _uuid

    video_uuid = str(_uuid.uuid4())
    await db.execute(
        "INSERT INTO videos (uuid, owner_id, title, status, raw_path) VALUES (?, ?, ?, 'failed', ?)",
        (video_uuid, owner_id, "Broken video", raw_path),
    )
    await db.commit()
    return video_uuid


@pytest.mark.asyncio
async def test_admin_can_retry_failed_transcode(client, logged_in_admin, member_user, db):
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _failed_video(db, owner_id)

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/admin/videos/{video_uuid}/retry", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    cursor = await db.execute(
        """
        SELECT j.status FROM transcode_jobs j JOIN videos v ON j.video_id = v.id WHERE v.uuid = ?
        """,
        (video_uuid,),
    )
    assert (await cursor.fetchone())["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_refused_when_original_purged(client, logged_in_admin, member_user, db):
    """With the raw upload gone there is nothing left to transcode from."""
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _failed_video(db, owner_id, raw_path=None)

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/admin/videos/{video_uuid}/retry", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_member_cannot_retry(client, logged_in_member, db):
    owner_id = await _user_id_by_email(db, "member@example.com")
    video_uuid = await _failed_video(db, owner_id)

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/admin/videos/{video_uuid}/retry", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_reports_storage(client, logged_in_admin, member_user, db):
    import uuid as _uuid

    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = str(_uuid.uuid4())
    cursor = await db.execute(
        """
        INSERT INTO videos (uuid, owner_id, title, status, raw_path, raw_size_bytes)
        VALUES (?, ?, 'Sized', 'ready', '/tmp/raw.mp4', ?)
        """,
        (video_uuid, owner_id, 1024 * 1024),
    )
    await db.commit()
    await db.execute(
        "INSERT INTO video_renditions (video_id, label, file_path, status, size_bytes) VALUES (?, '720p', 'a.mp4', 'ready', ?)",
        (cursor.lastrowid, 2 * 1024 * 1024),
    )
    await db.commit()

    resp = await client.get("/admin/")
    assert resp.status_code == 200
    assert "Storage used" in resp.text
    assert "3.0 MiB" in resp.text


@pytest.mark.asyncio
async def test_dashboard_lists_failed_jobs(client, logged_in_admin, member_user, db):
    owner_id = await _user_id_by_email(db, member_user["email"])
    video_uuid = await _failed_video(db, owner_id)
    cursor = await db.execute("SELECT id FROM videos WHERE uuid = ?", (video_uuid,))
    video_id = (await cursor.fetchone())["id"]
    await db.execute(
        """
        INSERT INTO transcode_jobs (video_id, status, attempts, last_error)
        VALUES (?, 'failed', 3, 'ffmpeg exploded')
        """,
        (video_id,),
    )
    await db.commit()

    resp = await client.get("/admin/")
    assert resp.status_code == 200
    assert "Failed transcodes" in resp.text
    assert "ffmpeg exploded" in resp.text


@pytest.mark.asyncio
async def test_admin_videos_status_filter(client, logged_in_admin, member_user, db):
    owner_id = await _user_id_by_email(db, member_user["email"])
    await _failed_video(db, owner_id)

    resp = await client.get("/admin/videos?status=failed")
    assert resp.status_code == 200
    assert "Broken video" in resp.text

    resp = await client.get("/admin/videos?status=ready")
    assert resp.status_code == 200
    assert "Broken video" not in resp.text
