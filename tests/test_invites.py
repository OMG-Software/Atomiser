import pytest


@pytest.mark.asyncio
async def test_admin_can_create_invite(client, logged_in_admin):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/invites/create",
        data={"expires_hours": 24, "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "http://testserver/auth/register?token=" in resp.text


@pytest.mark.asyncio
async def test_member_cannot_create_invite(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/invites/create",
        data={"expires_hours": 24, "csrf": csrf},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_invite_missing_csrf(client, logged_in_admin):
    resp = await client.post(
        "/invites/create",
        data={"expires_hours": 24},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_invite_invalid_expiry(client, logged_in_admin):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/invites/create",
        data={"expires_hours": 999, "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    # The form clamps to 168 hours and still creates an invite.
    assert "http://testserver/auth/register?token=" in resp.text


@pytest.mark.asyncio
async def test_admin_can_list_invites(client, logged_in_admin):
    resp = await client.get("/invites/", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_member_cannot_list_invites(client, logged_in_member):
    resp = await client.get("/invites/", follow_redirects=False)
    assert resp.status_code in (403, 307, 302)


@pytest.mark.asyncio
async def test_invite_exhausted_uses(client, db, csrf):
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone

    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, used_count, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token_hash, 1, 1, 1, expires.isoformat()),
    )
    await db.commit()

    resp = await client.post(
        "/auth/register",
        data={
            "token": token,
            "email": "newmember@example.com",
            "password": "NewMemberPass123",
            "display_name": "New Member",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# max_uses, notes and revocation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_invite_with_multiple_uses_and_note(client, logged_in_admin, db):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/invites/create",
        data={"expires_hours": 24, "max_uses": 5, "note": "film club", "csrf": csrf},
    )
    assert resp.status_code == 200

    cursor = await db.execute("SELECT max_uses, note FROM invites ORDER BY id DESC LIMIT 1")
    row = await cursor.fetchone()
    assert row["max_uses"] == 5
    assert row["note"] == "film club"
    assert "film club" in resp.text


@pytest.mark.asyncio
async def test_max_uses_is_clamped(client, logged_in_admin, db):
    csrf = client.cookies.get("csrf")
    await client.post(
        "/invites/create",
        data={"expires_hours": 24, "max_uses": 9999, "csrf": csrf},
    )
    cursor = await db.execute("SELECT max_uses FROM invites ORDER BY id DESC LIMIT 1")
    assert (await cursor.fetchone())["max_uses"] == 25


@pytest.mark.asyncio
async def test_invite_list_shows_existing_invites_after_create(client, logged_in_admin):
    """Creating an invite re-renders the page; the table must not come back empty."""
    csrf = client.cookies.get("csrf")
    await client.post("/invites/create", data={"expires_hours": 24, "note": "first", "csrf": csrf})
    resp = await client.post("/invites/create", data={"expires_hours": 24, "note": "second", "csrf": csrf})
    assert resp.status_code == 200
    assert "first" in resp.text
    assert "second" in resp.text


@pytest.mark.asyncio
async def test_admin_can_revoke_invite(client, logged_in_admin, db):
    csrf = client.cookies.get("csrf")
    await client.post("/invites/create", data={"expires_hours": 24, "csrf": csrf})
    cursor = await db.execute("SELECT id FROM invites ORDER BY id DESC LIMIT 1")
    invite_id = (await cursor.fetchone())["id"]

    resp = await client.post(
        f"/invites/{invite_id}/revoke", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT revoked_at FROM invites WHERE id = ?", (invite_id,))
    assert (await cursor.fetchone())["revoked_at"] is not None


@pytest.mark.asyncio
async def test_revoked_invite_cannot_be_used_to_register(client, logged_in_admin, db, csrf):
    from datetime import datetime, timedelta, timezone

    from app.utils import generate_token, hash_token

    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    await db.execute(
        """
        INSERT INTO invites (token_hash, created_by, max_uses, expires_at, revoked_at)
        VALUES (?, 1, 1, ?, ?)
        """,
        (hash_token(token), expires.isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()

    resp = await client.post(
        "/auth/register",
        data={
            "token": token,
            "email": "revoked@example.com",
            "password": "RevokedPass12345",
            "display_name": "Revoked",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower()


@pytest.mark.asyncio
async def test_revoke_requires_csrf(client, logged_in_admin, db):
    csrf = client.cookies.get("csrf")
    await client.post("/invites/create", data={"expires_hours": 24, "csrf": csrf})
    cursor = await db.execute("SELECT id FROM invites ORDER BY id DESC LIMIT 1")
    invite_id = (await cursor.fetchone())["id"]

    resp = await client.post(f"/invites/{invite_id}/revoke", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_revoke_invite(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post("/invites/1/revoke", data={"csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_email_field_hidden_when_smtp_unconfigured(client, logged_in_admin):
    resp = await client.get("/invites/")
    assert resp.status_code == 200
    assert 'name="send_to"' not in resp.text
    assert "Email delivery is not configured" in resp.text
