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
    assert re.search(r"<strong>1</strong>\s*<div class=\"meta\">Active invites</div>", resp.text)


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
