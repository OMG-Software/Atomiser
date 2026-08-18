import pytest

from app.db import get_db


@pytest.mark.asyncio
async def test_member_can_view_public_profile(client, logged_in_member, member_user):
    """Non-admin members can view each other's public profiles."""
    # Create a second member via invite for a distinct profile.
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone

    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)

    async for db in get_db():
        await db.execute(
            "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, 1, 1, expires.isoformat()),
        )
        await db.commit()

        csrf = client.cookies.get("csrf")
        await client.post(
            "/auth/register",
            data={
                "token": token,
                "email": "othermember@example.com",
                "password": "OtherMemberPass123",
                "display_name": "Other Member",
                "csrf": csrf,
            },
            follow_redirects=False,
        )
        cur = await db.execute("SELECT id FROM users WHERE email = ?", ("othermember@example.com",))
        other_id = (await cur.fetchone())["id"]
        break

    resp = await client.get(f"/u/{other_id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Other Member" in resp.text


@pytest.mark.asyncio
async def test_update_profile_requires_csrf(client, logged_in_member):
    resp = await client.post(
        "/profile",
        data={"display_name": "Hacker"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_profile_blank_name(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile",
        data={"display_name": "   ", "csrf": csrf},
        follow_redirects=False,
    )
    # The route strips and writes whatever it gets; the UI validates via HTML required attribute.
    # Server side should at least accept the request without error.
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_public_profile_not_found(client, logged_in_member):
    resp = await client.get("/u/99999", follow_redirects=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_profile_hides_private_videos(client, logged_in_member, admin_user, db):
    from tests.test_videos import _create_video, _user_id_by_email

    owner_id = await _user_id_by_email(db, admin_user["email"])
    public_uuid = await _create_video(db, owner_id, title="Public Video", visibility="site")
    private_uuid = await _create_video(db, owner_id, title="Private Video", visibility="private")

    resp = await client.get(f"/u/{owner_id}", follow_redirects=False)
    assert resp.status_code == 200
    assert "Public Video" in resp.text
    assert "Private Video" not in resp.text


@pytest.mark.asyncio
async def test_change_password_success(client, logged_in_member, db):
    """Changing password updates the hash and the new password logs you in."""
    csrf = client.cookies.get("csrf")
    new_password = "NewMemberPass123456"

    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "MemberPass123456",
            "new_password": new_password,
            "confirm_password": new_password,
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile?password_changed=1"

    # The stored hash no longer matches the old password.
    from app.auth import verify_password
    cur = await db.execute("SELECT password_hash FROM users WHERE email = ?", ("member@example.com",))
    row = await cur.fetchone()
    assert await verify_password(new_password, row["password_hash"])
    assert not await verify_password("MemberPass123456", row["password_hash"])

    # Log out, then log back in with the new password.
    await client.get("/auth/logout", follow_redirects=False)
    resp = await client.post(
        "/auth/login",
        data={"email": "member@example.com", "password": new_password, "csrf": csrf, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_change_password_wrong_current(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "totally-wrong",
            "new_password": "NewMemberPass123456",
            "confirm_password": "NewMemberPass123456",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Current password is incorrect" in resp.text


@pytest.mark.asyncio
async def test_change_password_weak_new(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "MemberPass123456",
            "new_password": "short1",  # < 12 chars
            "confirm_password": "short1",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "at least 12 characters" in resp.text


@pytest.mark.asyncio
async def test_change_password_mismatch(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "MemberPass123456",
            "new_password": "NewMemberPass123456",
            "confirm_password": "DifferentPass123456",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "do not match" in resp.text


@pytest.mark.asyncio
async def test_change_password_same_as_current(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "MemberPass123456",
            "new_password": "MemberPass123456",
            "confirm_password": "MemberPass123456",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "different from your current" in resp.text


@pytest.mark.asyncio
async def test_change_password_requires_csrf(client, logged_in_member):
    resp = await client.post(
        "/profile/password",
        data={
            "current_password": "MemberPass123456",
            "new_password": "NewMemberPass123456",
            "confirm_password": "NewMemberPass123456",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

async def _user_id(db, email):
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    return row["id"]


@pytest.mark.asyncio
async def test_profile_lists_active_sessions(client, logged_in_member):
    resp = await client.get("/profile")
    assert resp.status_code == 200
    assert "Active sessions" in resp.text
    assert "this device" in resp.text


@pytest.mark.asyncio
async def test_webauthn_challenge_rows_are_not_listed_as_sessions(client, logged_in_member, db):
    """Passkey challenge rows share the sessions table but are not logins."""
    from app.utils import generate_token, hash_token, now_utc
    from datetime import timedelta

    user_id = await _user_id(db, logged_in_member["email"])
    await db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, purpose) VALUES (?, ?, ?, 'webauthn')",
        (hash_token(generate_token()), user_id, (now_utc() + timedelta(minutes=5)).isoformat()),
    )
    await db.commit()

    resp = await client.get("/profile")
    assert resp.status_code == 200
    # Only the real login session is offered for revocation.
    assert resp.text.count("/profile/sessions/") == 1


@pytest.mark.asyncio
async def test_revoke_other_session(client, logged_in_member, db):
    from app.auth import create_session

    user_id = await _user_id(db, logged_in_member["email"])
    await create_session(db, user_id, "10.0.0.2", "other device")

    cursor = await db.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND ip = '10.0.0.2'", (user_id,)
    )
    other_id = (await cursor.fetchone())["id"]

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/profile/sessions/{other_id}/revoke", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT COUNT(*) AS c FROM sessions WHERE id = ?", (other_id,))
    assert (await cursor.fetchone())["c"] == 0

    # The caller stays signed in.
    assert (await client.get("/profile", follow_redirects=False)).status_code == 200


@pytest.mark.asyncio
async def test_cannot_revoke_another_users_session(client, logged_in_member, admin_user, db):
    """Scoping the delete by user id is what stops cross-account revocation."""
    from app.auth import create_session

    victim_id = await _user_id(db, admin_user["email"])
    await create_session(db, victim_id, "10.0.0.3", "victim device")
    cursor = await db.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND ip = '10.0.0.3'", (victim_id,)
    )
    victim_session = (await cursor.fetchone())["id"]

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/profile/sessions/{victim_session}/revoke", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 404

    cursor = await db.execute("SELECT COUNT(*) AS c FROM sessions WHERE id = ?", (victim_session,))
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_revoking_current_session_signs_you_out(client, logged_in_member, db):
    user_id = await _user_id(db, logged_in_member["email"])
    cursor = await db.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND purpose = 'session'", (user_id,)
    )
    current = (await cursor.fetchone())["id"]

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/profile/sessions/{current}/revoke", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    follow = await client.get("/profile", follow_redirects=False)
    assert follow.status_code in (302, 303)


@pytest.mark.asyncio
async def test_revoke_others_keeps_current_session(client, logged_in_member, db):
    from app.auth import create_session

    user_id = await _user_id(db, logged_in_member["email"])
    await create_session(db, user_id, "10.0.0.4", "device a")
    await create_session(db, user_id, "10.0.0.5", "device b")

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/profile/sessions/revoke-others", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,))
    assert (await cursor.fetchone())["c"] == 1
    assert (await client.get("/profile", follow_redirects=False)).status_code == 200


@pytest.mark.asyncio
async def test_revoke_session_requires_csrf(client, logged_in_member, db):
    user_id = await _user_id(db, logged_in_member["email"])
    cursor = await db.execute("SELECT id FROM sessions WHERE user_id = ?", (user_id,))
    session_id = (await cursor.fetchone())["id"]

    resp = await client.post(f"/profile/sessions/{session_id}/revoke", follow_redirects=False)
    assert resp.status_code == 403
