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
