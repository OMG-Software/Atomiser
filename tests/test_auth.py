import re

import pyotp
import pytest


@pytest.mark.asyncio
async def test_login_page_loads(client):
    resp = await client.get("/auth/login")
    assert resp.status_code == 200
    assert "csrf" in resp.cookies


@pytest.mark.asyncio
async def test_login_missing_fields(client, csrf):
    """FastAPI rejects missing required form fields with 422 before the route runs."""
    resp = await client.post(
        "/auth/login",
        data={"email": "", "password": "", "csrf": csrf},
    )
    assert resp.status_code in (401, 422)
    assert "session" not in resp.cookies


@pytest.mark.asyncio
async def test_login_unknown_email(client, csrf):
    resp = await client.post(
        "/auth/login",
        data={"email": "nobody@example.com", "password": "doesnotmatter", "csrf": csrf},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_success(client, admin_user, csrf):
    resp = await client.post(
        "/auth/login",
        data={
            "email": admin_user["email"],
            "password": admin_user["password"],
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session" in resp.cookies


@pytest.mark.asyncio
async def test_login_bad_password(client, admin_user, csrf):
    resp = await client.post(
        "/auth/login",
        data={
            "email": admin_user["email"],
            "password": "wrongpassword",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 401
    assert "session" not in resp.cookies


@pytest.mark.asyncio
async def test_login_invalid_csrf(client, admin_user):
    resp = await client.post(
        "/auth/login",
        data={
            "email": admin_user["email"],
            "password": admin_user["password"],
            "csrf": "invalid-token",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_session(client, admin_user, csrf):
    login_resp = await client.post(
        "/auth/login",
        data={
            "email": admin_user["email"],
            "password": admin_user["password"],
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert "session" in login_resp.cookies

    logout_resp = await client.get("/auth/logout", follow_redirects=False)
    assert logout_resp.status_code == 303
    # httpx may not expose cleared cookies directly; check follow-up request is anonymous.
    home = await client.get("/")
    assert home.status_code in (302, 303)
    assert "/auth/login" in home.headers["location"]


@pytest.mark.asyncio
async def test_register_via_invite(client, db, csrf):
    # Create an invite
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone
    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 1, expires.isoformat()),
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
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "session" in resp.cookies

    # Invite is consumed
    cur = await db.execute("SELECT used_count FROM invites WHERE token_hash = ?", (token_hash,))
    row = await cur.fetchone()
    assert row["used_count"] == 1


@pytest.mark.asyncio
async def test_register_expired_invite(client, db, csrf):
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone
    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) - timedelta(hours=1)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 1, expires.isoformat()),
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


@pytest.mark.asyncio
async def test_register_weak_password(client, db, csrf):
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone
    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 1, expires.isoformat()),
    )
    await db.commit()

    resp = await client.post(
        "/auth/register",
        data={
            "token": token,
            "email": "newmember@example.com",
            "password": "short",
            "display_name": "New Member",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_requires_csrf(client, db, csrf):
    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone
    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 1, expires.isoformat()),
    )
    await db.commit()

    resp = await client.post(
        "/auth/register",
        data={
            "token": token,
            "email": "newmember@example.com",
            "password": "NewMemberPass123",
            "display_name": "New Member",
        },
    )
    # FastAPI validates required form fields before CSRF is checked, so 422 is acceptable.
    assert resp.status_code in (403, 422)


@pytest.mark.asyncio
async def test_register_duplicate_email(client, db, csrf):
    # Insert an existing user directly so the duplicate-email check triggers.
    from app.auth import hash_password
    pwd_hash = await hash_password("ExistingPass123")
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        ("existing@example.com", pwd_hash, "Existing User", "member"),
    )
    await db.commit()

    from app.utils import generate_token, hash_token
    from datetime import timedelta, timezone
    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 1, expires.isoformat()),
    )
    await db.commit()

    resp = await client.post(
        "/auth/register",
        data={
            "token": token,
            "email": "existing@example.com",
            "password": "NewMemberPass123",
            "display_name": "New Member",
            "csrf": csrf,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_used_invite(client, db, csrf):
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


@pytest.mark.asyncio
async def test_totp_setup_requires_login(client):
    resp = await client.get("/auth/totp/setup")
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_totp_setup_and_login(client, logged_in_member):
    # Fetch the TOTP secret from the setup page.
    setup_resp = await client.get("/auth/totp/setup")
    assert setup_resp.status_code == 200
    html = setup_resp.text
    match = re.search(r"Manual secret: <code>([A-Z2-7]+)</code>", html)
    assert match, "TOTP secret not found in setup page"
    secret = match.group(1)

    csrf = client.cookies.get("csrf")
    code = pyotp.TOTP(secret).now()
    post_resp = await client.post(
        "/auth/totp/setup",
        data={"code": code, "csrf": csrf},
        follow_redirects=False,
    )
    assert post_resp.status_code == 303
    assert "success=1" in post_resp.headers["location"]

    # Logging in now requires the current TOTP code.
    logout_resp = await client.get("/auth/logout", follow_redirects=False)
    assert logout_resp.status_code == 303

    csrf = client.cookies.get("csrf")
    totp_code = pyotp.TOTP(secret).now()
    login_resp = await client.post(
        "/auth/login",
        data={
            "email": logged_in_member["email"],
            "password": logged_in_member["password"],
            "totp_code": totp_code,
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/"
    assert "session" in login_resp.cookies

    # Login with the right password but missing TOTP code is rejected.
    await client.get("/auth/logout", follow_redirects=False)
    csrf = client.cookies.get("csrf")
    bad_login = await client.post(
        "/auth/login",
        data={
            "email": logged_in_member["email"],
            "password": logged_in_member["password"],
            "totp_code": "000000",
            "csrf": csrf,
        },
    )
    assert bad_login.status_code == 401


@pytest.mark.asyncio
async def test_totp_setup_bad_code(client, logged_in_member):
    setup_resp = await client.get("/auth/totp/setup")
    assert setup_resp.status_code == 200
    html = setup_resp.text
    match = re.search(r"Manual secret: <code>([A-Z2-7]+)</code>", html)
    assert match
    secret = match.group(1)

    csrf = client.cookies.get("csrf")
    bad_code = str((int(pyotp.TOTP(secret).now()) + 1) % 1000000).zfill(6)
    post_resp = await client.post(
        "/auth/totp/setup",
        data={"code": bad_code, "csrf": csrf},
    )
    assert post_resp.status_code == 400


@pytest.mark.asyncio
async def test_totp_setup_requires_csrf(client, logged_in_member):
    resp = await client.post(
        "/auth/totp/setup",
        data={"code": "000000", "csrf": "bad-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_passkeys_page_requires_login(client):
    resp = await client.get("/auth/passkeys")
    assert resp.status_code in (302, 303)
    assert "/auth/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_passkey_register_options_requires_login(client):
    resp = await client.post("/auth/passkey/register/options", json={"csrf": "x"})
    # Unauthenticated users are redirected to login before the route runs.
    assert resp.status_code in (302, 303)


@pytest.mark.asyncio
async def test_passkey_register_options_returns_json(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/auth/passkey/register/options",
        json={"csrf": csrf},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "publicKey" in data
    pk = data["publicKey"]
    assert "rp" in pk
    assert "user" in pk
    assert "challenge" in pk


@pytest.mark.asyncio
async def test_passkey_login_options_returns_json(client, member_user):
    resp = await client.post(
        "/auth/passkey/login/options",
        json={"email": member_user["email"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "options" in data
    assert "temp_token" in data
    pk = data["options"]["publicKey"]
    assert "challenge" in pk


@pytest.mark.asyncio
async def test_passkey_register_options_invalid_csrf(client, logged_in_member):
    resp = await client.post(
        "/auth/passkey/register/options",
        json={"csrf": "bad-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_passkey_register_invalid_csrf(client, logged_in_member):
    resp = await client.post(
        "/auth/passkey/register",
        json={"csrf": "bad-token", "id": "x"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_login_rejects_open_redirect(client, admin_user, csrf):
    """Malicious 'next' values should be rejected and the user redirected to '/' instead."""
    for malicious_next in ["https://evil.com", "//evil.com", "javascript:alert(1)", "data:text/html,foo"]:
        resp = await client.post(
            "/auth/login",
            data={
                "email": admin_user["email"],
                "password": admin_user["password"],
                "csrf": csrf,
                "next": malicious_next,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"unexpected status for next={malicious_next!r}"
        assert resp.headers["location"] == "/", f"open redirect allowed for next={malicious_next!r}"


@pytest.mark.asyncio
async def test_login_allows_local_redirect(client, admin_user, csrf):
    """Relative local paths are still allowed as post-login redirects."""
    resp = await client.post(
        "/auth/login",
        data={
            "email": admin_user["email"],
            "password": admin_user["password"],
            "csrf": csrf,
            "next": "/videos/upload",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/videos/upload"


@pytest.mark.asyncio
async def test_login_malformed_password_hash(client, db, csrf):
    """A corrupt password_hash must fail login cleanly (401), not raise a 500."""
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("corrupt@example.com", "not-a-valid-argon2-hash", "Corrupt User", "member", 0),
    )
    await db.commit()
    resp = await client.post(
        "/auth/login",
        data={"email": "corrupt@example.com", "password": "whatever123", "csrf": csrf},
    )
    assert resp.status_code == 401
    assert "session" not in resp.cookies


@pytest.mark.asyncio
async def test_register_concurrent_invite_not_exceeding_max_uses(client, db, csrf):
    """Concurrent registrations against a multi-use invite must not exceed max_uses.

    The conditional UPDATE in register_post is the authoritative gate; the
    pre-check SELECT only provides a friendly error and is not the source of
    truth, so a race between the SELECT and the increment cannot over-consume.
    """
    import asyncio
    from datetime import timedelta, timezone
    from app.utils import generate_token, hash_token

    token = generate_token()
    token_hash = hash_token(token)
    expires = __import__("datetime").datetime.now(timezone.utc) + timedelta(hours=48)
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, 1, 3, expires.isoformat()),
    )
    await db.commit()

    async def register(i):
        return await client.post(
            "/auth/register",
            data={
                "token": token,
                "email": f"race{i}@example.com",
                "password": "RacePass12345",
                "display_name": f"Racer {i}",
                "csrf": csrf,
            },
            follow_redirects=False,
        )

    resps = await asyncio.gather(*(register(i) for i in range(5)))
    successes = sum(1 for r in resps if r.status_code == 303)
    assert successes == 3, [r.status_code for r in resps]
    cur = await db.execute("SELECT used_count FROM invites WHERE token_hash = ?", (token_hash,))
    row = await cur.fetchone()
    assert row["used_count"] == 3
