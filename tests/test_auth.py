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


# ---------------------------------------------------------------------------
# Rate limiting and account lockout
# ---------------------------------------------------------------------------

@pytest.fixture
def low_login_threshold(monkeypatch):
    """Shrink the failure threshold so a test does not need dozens of requests."""
    from app.config import Config

    monkeypatch.setattr(Config, "LOGIN_MAX_FAILURES_PER_EMAIL", 3)
    monkeypatch.setattr(Config, "LOGIN_MAX_FAILURES_PER_IP", 100)
    return Config


async def _bad_login(client, csrf, email="member@example.com"):
    return await client.post(
        "/auth/login",
        data={"email": email, "password": "WrongPassword123", "csrf": csrf, "next": "/"},
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_repeated_failures_lock_the_account(client, csrf, member_user, low_login_threshold):
    for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL):
        resp = await _bad_login(client, csrf)
        assert resp.status_code == 401

    # The next attempt is throttled rather than checked.
    resp = await _bad_login(client, csrf)
    assert resp.status_code == 429
    assert "Too many attempts" in resp.text


@pytest.mark.asyncio
async def test_lockout_blocks_the_correct_password_too(client, csrf, member_user, low_login_threshold):
    """A lockout that the real password walks straight through is no lockout."""
    for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL):
        await _bad_login(client, csrf)

    resp = await client.post(
        "/auth/login",
        data={
            "email": member_user["email"],
            "password": member_user["password"],
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 429
    assert "session" not in resp.cookies


@pytest.mark.asyncio
async def test_unknown_email_is_throttled_identically(client, csrf, low_login_threshold):
    """Otherwise the throttle response itself reveals which accounts exist."""
    for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL):
        resp = await _bad_login(client, csrf, email="nobody@example.com")
        assert resp.status_code == 401

    resp = await _bad_login(client, csrf, email="nobody@example.com")
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_successful_login_clears_the_counter(client, csrf, member_user, low_login_threshold):
    # Stay one below the threshold, then sign in successfully.
    for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL - 1):
        await _bad_login(client, csrf)

    resp = await client.post(
        "/auth/login",
        data={
            "email": member_user["email"],
            "password": member_user["password"],
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async for db in __import__("app.db", fromlist=["get_db"]).get_db():
        cursor = await db.execute(
            "SELECT COUNT(*) AS c FROM login_attempts WHERE key_kind = 'email' AND key_value = ?",
            (member_user["email"],),
        )
        assert (await cursor.fetchone())["c"] == 0
        break


@pytest.mark.asyncio
async def test_rate_limiting_can_be_disabled(client, csrf, member_user, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(Config, "LOGIN_MAX_FAILURES_PER_EMAIL", 2)

    for _ in range(4):
        resp = await _bad_login(client, csrf)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forgot_page_explains_when_mail_is_off(client):
    resp = await client.get("/auth/forgot")
    assert resp.status_code == 200
    assert "does not send email" in resp.text
    assert 'name="email"' not in resp.text


@pytest.mark.asyncio
async def test_forgot_post_refused_when_mail_is_off(client, csrf, member_user):
    resp = await client.post(
        "/auth/forgot", data={"email": member_user["email"], "csrf": csrf}
    )
    assert resp.status_code == 400
    assert "not available" in resp.text


@pytest.fixture
def mail_configured(monkeypatch):
    """Pretend SMTP is set up, but capture messages instead of sending them."""
    from app import mail
    from app.config import Config

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(Config, "SMTP_FROM", "atomiser@example.com")
    # Required: emailed links are built from SITE_URL, never from the request.
    monkeypatch.setattr(Config, "SITE_URL", "https://videos.example.com")

    sent = []

    async def _capture(to_address, subject, body, site_title="Atomiser"):
        sent.append({"to": to_address, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(mail, "send_mail", _capture)
    return sent


@pytest.mark.asyncio
async def test_forgot_sends_a_reset_link(client, csrf, member_user, mail_configured, db):
    resp = await client.post(
        "/auth/forgot", data={"email": member_user["email"], "csrf": csrf}
    )
    assert resp.status_code == 200
    assert len(mail_configured) == 1
    assert "/auth/reset?token=" in mail_configured[0]["body"]

    cursor = await db.execute("SELECT COUNT(*) AS c FROM password_resets")
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_forgot_response_is_identical_for_unknown_email(client, csrf, mail_configured, db):
    resp = await client.post(
        "/auth/forgot", data={"email": "nobody@example.com", "csrf": csrf}
    )
    assert resp.status_code == 200
    assert "on its way" in resp.text
    assert mail_configured == []

    cursor = await db.execute("SELECT COUNT(*) AS c FROM password_resets")
    assert (await cursor.fetchone())["c"] == 0


def _token_from(body):
    return body.split("/auth/reset?token=")[1].split()[0].strip()


@pytest.mark.asyncio
async def test_reset_token_sets_new_password(client, csrf, member_user, mail_configured, db):
    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    page = await client.get(f"/auth/reset?token={token}")
    assert page.status_code == 200

    resp = await client.post(
        "/auth/reset",
        data={
            "token": token,
            "password": "BrandNewPass12345",
            "confirm_password": "BrandNewPass12345",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    login = await client.post(
        "/auth/login",
        data={
            "email": member_user["email"],
            "password": "BrandNewPass12345",
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303


@pytest.mark.asyncio
async def test_reset_token_is_single_use(client, csrf, member_user, mail_configured):
    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    payload = {
        "token": token,
        "password": "BrandNewPass12345",
        "confirm_password": "BrandNewPass12345",
        "csrf": csrf,
    }
    first = await client.post("/auth/reset", data=payload, follow_redirects=False)
    assert first.status_code == 303

    second = await client.post("/auth/reset", data=payload, follow_redirects=False)
    assert second.status_code == 400
    assert "invalid" in second.text.lower()


@pytest.mark.asyncio
async def test_expired_reset_token_is_rejected(client, csrf, member_user, mail_configured, db):
    from datetime import timedelta

    from app.utils import now_utc

    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    await db.execute(
        "UPDATE password_resets SET expires_at = ?",
        ((now_utc() - timedelta(minutes=1)).isoformat(),),
    )
    await db.commit()

    resp = await client.post(
        "/auth/reset",
        data={
            "token": token,
            "password": "BrandNewPass12345",
            "confirm_password": "BrandNewPass12345",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_rejects_weak_password(client, csrf, member_user, mail_configured):
    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    resp = await client.post(
        "/auth/reset",
        data={"token": token, "password": "short", "confirm_password": "short", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "12 characters" in resp.text


@pytest.mark.asyncio
async def test_reset_rejects_mismatched_confirmation(client, csrf, member_user, mail_configured):
    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    resp = await client.post(
        "/auth/reset",
        data={
            "token": token,
            "password": "BrandNewPass12345",
            "confirm_password": "DifferentPass12345",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "do not match" in resp.text


@pytest.mark.asyncio
async def test_reset_revokes_existing_sessions(client, csrf, member_user, mail_configured, db):
    """A reset is exactly when you want other sessions killed."""
    from app.auth import create_session

    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (member_user["email"],))
    user_id = (await cursor.fetchone())["id"]
    await create_session(db, user_id, "10.0.0.9", "stale device")

    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])
    await client.post(
        "/auth/reset",
        data={
            "token": token,
            "password": "BrandNewPass12345",
            "confirm_password": "BrandNewPass12345",
            "csrf": csrf,
        },
        follow_redirects=False,
    )

    cursor = await db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,))
    assert (await cursor.fetchone())["c"] == 0


@pytest.mark.asyncio
async def test_reset_requires_csrf(client, member_user, mail_configured, csrf):
    await client.post("/auth/forgot", data={"email": member_user["email"], "csrf": csrf})
    token = _token_from(mail_configured[0]["body"])

    resp = await client.post(
        "/auth/reset",
        data={
            "token": token,
            "password": "BrandNewPass12345",
            "confirm_password": "BrandNewPass12345",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Host-header injection on emailed links
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_link_ignores_a_spoofed_host_header(client, csrf, member_user, mail_configured):
    """The emailed link must come from SITE_URL, never the request's Host.

    nginx passes the client Host through verbatim, so deriving the link from it
    would let an attacker request a reset for someone else and have the genuine
    token emailed to the victim under a domain the attacker controls.
    """
    resp = await client.post(
        "/auth/forgot",
        data={"email": member_user["email"], "csrf": csrf},
        headers={"Host": "evil.test"},
    )
    assert resp.status_code == 200

    body = mail_configured[0]["body"]
    assert "https://videos.example.com/auth/reset?token=" in body
    assert "evil.test" not in body


@pytest.mark.asyncio
async def test_reset_is_refused_when_site_url_is_unset(client, csrf, member_user, monkeypatch, db):
    """Rather than fall back to the Host header, recovery turns itself off."""
    from app.config import Config

    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(Config, "SMTP_FROM", "atomiser@example.com")
    monkeypatch.setattr(Config, "SITE_URL", "")

    resp = await client.post(
        "/auth/forgot",
        data={"email": member_user["email"], "csrf": csrf},
        headers={"Host": "evil.test"},
    )
    assert resp.status_code == 400
    assert "not available" in resp.text

    # No token was minted, so nothing can leak.
    cursor = await db.execute("SELECT COUNT(*) AS c FROM password_resets")
    assert (await cursor.fetchone())["c"] == 0


def test_email_link_refuses_without_site_url(monkeypatch):
    from app import mail as mail_module
    from app.config import Config

    monkeypatch.setattr(Config, "SITE_URL", "")
    assert mail_module.email_links_available() is False
    with pytest.raises(RuntimeError):
        mail_module.email_link("/auth/reset?token=abc")


# ---------------------------------------------------------------------------
# Throttling must not distinguish real from unknown addresses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mixed_case_address_is_throttled_like_a_real_one(
    client, csrf, member_user, low_login_threshold
):
    """Regression: failures were recorded normalized but counted raw, so the
    same mixed-case input answered 429 for a real account and 401 for an
    unknown one - an enumeration oracle."""
    real = "  Member@Example.COM "
    ghost = "  Ghost@Example.COM "

    for address in (real, ghost):
        for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL):
            resp = await _bad_login(client, csrf, email=address)
            assert resp.status_code == 401

    real_resp = await _bad_login(client, csrf, email=real)
    ghost_resp = await _bad_login(client, csrf, email=ghost)

    assert real_resp.status_code == ghost_resp.status_code == 429


@pytest.mark.asyncio
async def test_success_clears_failures_recorded_under_mixed_case(
    client, csrf, member_user, low_login_threshold
):
    for _ in range(low_login_threshold.LOGIN_MAX_FAILURES_PER_EMAIL - 1):
        await _bad_login(client, csrf, email="MEMBER@example.com")

    resp = await client.post(
        "/auth/login",
        data={
            "email": member_user["email"],
            "password": member_user["password"],
            "csrf": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
