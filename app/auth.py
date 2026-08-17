import base64
import binascii
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import pyotp
from fido2.server import Fido2Server
from fido2.webauthn import (
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
    AttestationConveyancePreference,
    UserVerificationRequirement,
)
from fido2.utils import websafe_decode, websafe_encode
from starlette.concurrency import run_in_threadpool

from app import mail, ratelimit
from app.config import Config
from app.db import get_db
from app.models import LoginForm, RegisterForm, TOTPChallengeForm
from app.roles import Role, require_role
from app.utils import generate_token, hash_token, new_video_uuid, now_utc, verify_csrf

router = APIRouter(prefix="/auth", tags=["auth"])

ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
PENDING_COOKIE = "pending_auth"

# WebAuthn server is created once per process.
# Set WEBAUTHN_RP_ID in production to the canonical origin host (e.g. example.com).
_rp = PublicKeyCredentialRpEntity(
    id=Config.WEBAUTHN_RP_ID,
    name="Atomiser",
)
fido_server = Fido2Server(_rp)


templates = None  # initialised in main.py


def _set_session_cookie(response: Response, token: str, max_age: int = 7 * 24 * 3600):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=Config.PRODUCTION,
        samesite="Lax",
        max_age=max_age,
        path="/",
    )


def _clear_session_cookie(response: Response):
    response.delete_cookie(key=SESSION_COOKIE, path="/")


def _set_pending_cookie(response: Response, token: str, max_age: int = 300):
    response.set_cookie(
        key=PENDING_COOKIE,
        value=token,
        httponly=True,
        secure=Config.PRODUCTION,
        samesite="Lax",
        max_age=max_age,
        path="/auth/totp/verify",
    )


def _clear_pending_cookie(response: Response):
    response.delete_cookie(key=PENDING_COOKIE, path="/auth/totp/verify")


def password_policy_error(pwd: str) -> Optional[str]:
    """Return a human-readable policy violation, or None if the password is acceptable.

    Used by registration, self-service change, and (indirectly, since generated
    passwords are built to satisfy it) the admin reset flow so the rule lives in
    one place.
    """
    pwd = pwd.strip()
    if len(pwd) < 12 or not any(c.isalpha() for c in pwd) or not any(c.isdigit() for c in pwd):
        return "Password must be at least 12 characters with letters and digits"
    return None


async def hash_password(plain: str) -> str:
    # Argon2 hashing is CPU-intensive; run it off the event loop.
    return await run_in_threadpool(ph.hash, plain)


async def verify_password(plain: str, hashed: str) -> bool:
    try:
        await run_in_threadpool(ph.verify, hashed, plain)
        return True
    except VerifyMismatchError:
        return False
    except (Argon2Error, InvalidHashError):
        # Malformed or otherwise invalid hash → treat as a failed login
        # rather than surfacing a 500 to the user.
        return False


async def create_session(
    db,
    user_id: int,
    ip: Optional[str],
    user_agent: Optional[str],
    ttl_hours: int = 7 * 24,
) -> str:
    token = generate_token()
    token_hash = hash_token(token)
    expires = now_utc() + timedelta(hours=ttl_hours)
    await db.execute(
        """
        INSERT INTO sessions (token_hash, user_id, expires_at, ip, user_agent)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token_hash, user_id, expires.isoformat(), ip, user_agent),
    )
    await db.commit()
    return token


async def delete_session(db, token: str):
    token_hash = hash_token(token)
    await db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    await db.commit()


async def get_user_by_session(db, token: str) -> Optional[dict]:
    if not token:
        return None
    token_hash = hash_token(token)
    cursor = await db.execute(
        """
        SELECT s.id AS session_id, s.expires_at, s.webauthn_challenge, s.webauthn_state,
               u.id, u.email, u.display_name, u.role, u.totp_enabled, u.is_bootstrap
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token_hash = ?
        """,
        (token_hash,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if now_utc() > expires:
        await db.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
        await db.commit()
        return None
    return {
        "session_id": row["session_id"],
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "totp_enabled": bool(row["totp_enabled"]),
        "is_bootstrap": bool(row["is_bootstrap"]),
        "webauthn_challenge": row["webauthn_challenge"],
        "webauthn_state": row["webauthn_state"],
    }


async def get_current_user(request: Request, db=Depends(get_db)) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    user = await get_user_by_session(db, token)
    if user:
        await db.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?",
            (now_utc().isoformat(), user["session_id"]),
        )
        await db.commit()
    return user


class LoginRequiredException(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


async def require_user(request: Request, user=Depends(get_current_user)):
    if not user:
        raise LoginRequiredException(str(request.url.path))
    return user


async def _get_user_by_email(db, email: str) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT id, email, password_hash, display_name, role, totp_enabled, totp_secret FROM users WHERE email = ?",
        (email.lower().strip(),),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return dict(row)


async def _client_host(request: Request) -> Optional[str]:
    """Best-effort client IP. Uvicorn on a unix socket leaves request.client as None,
    so fall back to the headers set by nginx."""
    if request.client:
        return request.client.host
    return request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")


def _safe_next_url(next_url: str) -> str:
    """Only allow local relative redirects; reject absolute and protocol-relative URLs."""
    if not next_url:
        return "/"
    next_url = next_url.strip()
    # Reject anything that looks like an absolute URL or protocol-relative URL.
    if "//" in next_url or ":" in next_url or not next_url.startswith("/"):
        return "/"
    return next_url


async def _audit(db, user_id: Optional[int], action: str, target_type: str = None, target_id: str = None, request: Request = None):
    ip = await _client_host(request) if request else None
    ua = request.headers.get("user-agent") if request else None
    await db.execute(
        "INSERT INTO audit_log (user_id, action, target_type, target_id, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, target_type, target_id, ip, ua),
    )
    await db.commit()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", db=Depends(get_db)):
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "next": _safe_next_url(next),
            "site_title": await _site_title(db),
            "mail_enabled": mail.mail_enabled(),
        },
    )


async def _login_error(request, db, message: str, next_url: str, status_code: int):
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "error": message,
            "next": next_url,
            "site_title": await _site_title(db),
            "mail_enabled": mail.mail_enabled(),
        },
        status_code=status_code,
    )


@router.post("/login")
async def login_post(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(""),
    next: str = Form("/"),
    csrf: str = Form(...),
    db=Depends(get_db),
):
    cookie_csrf = request.cookies.get(CSRF_COOKIE)
    if not verify_csrf(csrf, cookie_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    client_ip = await _client_host(request)

    # Throttle before touching the password hash: Argon2 verification is
    # deliberately expensive, so an unthrottled endpoint is also a cheap way to
    # burn the server's CPU.
    wait_minutes = await ratelimit.retry_after_minutes(db, ratelimit.SCOPE_LOGIN, email=email, ip=client_ip)
    if wait_minutes:
        await _audit(db, None, "login_throttled", target_type="email", target_id=email, request=request)
        return await _login_error(
            request, db, ratelimit.throttle_message(wait_minutes), next,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = await _get_user_by_email(db, email)
    if not user or not await verify_password(password, user["password_hash"]):
        await _audit(db, user["id"] if user else None, "login_failed", request=request)
        await ratelimit.record_failure(db, ratelimit.SCOPE_LOGIN, email=email, ip=client_ip)
        await ratelimit.apply_lockout(db, email)
        return await _login_error(
            request, db, "Invalid email or password", next, status.HTTP_401_UNAUTHORIZED,
        )

    if user["totp_enabled"]:
        totp = pyotp.TOTP(user.get("totp_secret") or "")
        if not totp.verify(totp_code.strip(), valid_window=1):
            # A wrong second factor counts as a failed attempt too, otherwise
            # the code itself could be brute-forced once a password is known.
            await ratelimit.record_failure(db, ratelimit.SCOPE_LOGIN, email=email, ip=client_ip)
            await ratelimit.apply_lockout(db, email)
            return await _login_error(
                request, db, "Invalid two-factor code", next, status.HTTP_401_UNAUTHORIZED,
            )

    await ratelimit.clear_failures(db, ratelimit.SCOPE_LOGIN, email=email)
    await ratelimit.unlock(db, email)
    await ratelimit.prune(db)

    token = await create_session(db, user["id"], client_ip, request.headers.get("user-agent"))
    await _audit(db, user["id"], "login", request=request)
    resp = RedirectResponse(url=_safe_next_url(next), status_code=303)
    _set_session_cookie(resp, token)
    return resp


@router.get("/logout")
async def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await delete_session(db, token)
    resp = RedirectResponse(url="/", status_code=303)
    _clear_session_cookie(resp)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, token: str = ""):
    return templates.TemplateResponse("auth/register.html", {"request": request, "token": token})


@router.post("/register")
async def register_post(
    request: Request,
    response: Response,
    token: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    csrf: str = Form(...),
    db=Depends(get_db),
):
    cookie_csrf = request.cookies.get(CSRF_COOKIE)
    if not verify_csrf(csrf, cookie_csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    # Throttle invite-token guessing from a single source.
    client_ip = await _client_host(request)
    wait_minutes = await ratelimit.retry_after_minutes(db, ratelimit.SCOPE_REGISTER, ip=client_ip)
    if wait_minutes:
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": ratelimit.throttle_message(wait_minutes), "token": token},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    token_hash = hash_token(token.strip())
    cursor = await db.execute(
        "SELECT id, max_uses, used_count, expires_at, revoked_at FROM invites WHERE token_hash = ?",
        (token_hash,),
    )
    invite = await cursor.fetchone()
    now = now_utc()
    if (
        not invite
        or invite["revoked_at"]
        or invite["used_count"] >= invite["max_uses"]
        or datetime.fromisoformat(invite["expires_at"]) < now
    ):
        await ratelimit.record_failure(db, ratelimit.SCOPE_REGISTER, ip=client_ip)
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": "Invite link is invalid, already used, or expired", "token": token},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    email = email.lower().strip()
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    if await cursor.fetchone():
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": "An account with this email already exists", "token": token},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Minimum password policy handled by form length; additional server-side check.
    policy_error = password_policy_error(password)
    if policy_error:
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": policy_error, "token": token},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Atomically claim a use of the invite. This conditional UPDATE is the
    # authoritative concurrency gate: it prevents two concurrent registrations
    # sharing a multi-use invite from both consuming the last use. The pre-check
    # above only gives a friendly error message; this is what enforces max_uses.
    cursor = await db.execute(
        "UPDATE invites SET used_count = used_count + 1 "
        "WHERE id = ? AND used_count < max_uses AND revoked_at IS NULL",
        (invite["id"],),
    )
    if cursor.rowcount != 1:
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": "Invite link is invalid, already used, or expired", "token": token},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    password_hash = await hash_password(password.strip())
    cursor = await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        (email, password_hash, display_name.strip(), Role.MEMBER.value),
    )
    user_id = cursor.lastrowid
    await db.execute(
        "UPDATE invites SET used_by = ? WHERE id = ?",
        (user_id, invite["id"]),
    )
    await db.commit()

    session_token = await create_session(db, user_id, await _client_host(request), request.headers.get("user-agent"))
    await _audit(db, user_id, "registered", target_type="invite", target_id=str(invite["id"]), request=request)
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, session_token)
    return resp


# ---------------------------------------------------------------------------
# Password reset (self-service, requires SMTP)
# ---------------------------------------------------------------------------
#
# Without SMTP configured these pages explain that an admin has to reset the
# password instead — the behaviour the site had before. With SMTP configured a
# user can recover on their own using a single-use, time-limited token. Only the
# token's hash is stored, and the response never reveals whether an address is
# registered.

@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request, db=Depends(get_db)):
    return templates.TemplateResponse(
        "auth/forgot.html",
        {
            "request": request,
            "mail_enabled": mail.mail_enabled(),
            "site_title": await _site_title(db),
        },
    )


@router.post("/forgot")
async def forgot_post(
    request: Request,
    email: str = Form(...),
    db=Depends(get_db),
):
    # CSRF is read from the form body rather than declared as a Form param so a
    # missing token returns 403 like the other state-changing routes, not a 422.
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    site_title = await _site_title(db)
    context = {"request": request, "mail_enabled": mail.mail_enabled(), "site_title": site_title}

    if not mail.mail_enabled():
        return templates.TemplateResponse(
            "auth/forgot.html",
            {**context, "error": "Password recovery by email is not available on this site. Please ask an admin to reset your password."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    client_ip = await _client_host(request)
    wait_minutes = await ratelimit.retry_after_minutes(db, ratelimit.SCOPE_FORGOT, email=email, ip=client_ip)
    if wait_minutes:
        return templates.TemplateResponse(
            "auth/forgot.html",
            {**context, "error": ratelimit.throttle_message(wait_minutes)},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # Every attempt is counted, whether or not the address exists, so the
    # throttle cannot be used to probe for valid accounts either.
    await ratelimit.record_failure(db, ratelimit.SCOPE_FORGOT, email=email, ip=client_ip)

    user = await _get_user_by_email(db, email)
    if user:
        token = generate_token()
        expires = now_utc() + timedelta(minutes=Config.PASSWORD_RESET_TTL_MINUTES)
        # Invalidate any earlier outstanding link for this account.
        await db.execute(
            "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now_utc().isoformat(), user["id"]),
        )
        await db.execute(
            """
            INSERT INTO password_resets (token_hash, user_id, expires_at, requested_ip)
            VALUES (?, ?, ?, ?)
            """,
            (hash_token(token), user["id"], expires.isoformat(), client_ip),
        )
        await db.commit()
        await _audit(db, user["id"], "password_reset_requested", request=request)

        await mail.send_password_reset(
            user["email"],
            mail.absolute_url(request, f"/auth/reset?token={token}"),
            site_title,
            Config.PASSWORD_RESET_TTL_MINUTES,
        )

    # Identical response either way.
    return templates.TemplateResponse(
        "auth/forgot.html",
        {**context, "sent": True},
    )


async def _reset_row(db, token: str):
    """Return a usable reset row for a raw token, or None."""
    if not token:
        return None
    cursor = await db.execute(
        """
        SELECT r.id, r.user_id, r.expires_at, r.used_at, u.email
        FROM password_resets r JOIN users u ON r.user_id = u.id
        WHERE r.token_hash = ?
        """,
        (hash_token(token.strip()),),
    )
    row = await cursor.fetchone()
    if not row or row["used_at"]:
        return None
    if datetime.fromisoformat(row["expires_at"]) < now_utc():
        return None
    return row


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = "", db=Depends(get_db)):
    row = await _reset_row(db, token)
    return templates.TemplateResponse(
        "auth/reset.html",
        {
            "request": request,
            "token": token,
            "valid": row is not None,
            "site_title": await _site_title(db),
        },
        status_code=status.HTTP_200_OK if row else status.HTTP_400_BAD_REQUEST,
    )


@router.post("/reset")
async def reset_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db=Depends(get_db),
):
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    site_title = await _site_title(db)
    row = await _reset_row(db, token)
    if not row:
        return templates.TemplateResponse(
            "auth/reset.html",
            {"request": request, "token": token, "valid": False, "site_title": site_title},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    password = password.strip()
    error = None
    if password != confirm_password.strip():
        error = "The two passwords do not match."
    else:
        error = password_policy_error(password)

    if error:
        return templates.TemplateResponse(
            "auth/reset.html",
            {"request": request, "token": token, "valid": True, "error": error, "site_title": site_title},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await db.execute(
        "UPDATE users SET password_hash = ?, locked_until = NULL WHERE id = ?",
        (await hash_password(password), row["user_id"]),
    )
    await db.execute(
        "UPDATE password_resets SET used_at = ? WHERE id = ?", (now_utc().isoformat(), row["id"])
    )
    # Anyone already signed in as this user is signed out: a password reset is
    # exactly the moment you want existing sessions invalidated.
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    await db.commit()
    await ratelimit.clear_failures(db, ratelimit.SCOPE_LOGIN, email=row["email"])
    await _audit(db, row["user_id"], "password_reset_completed", request=request)

    return RedirectResponse(url="/auth/login?reset=1", status_code=303)


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

@router.get("/totp/setup", response_class=HTMLResponse)
async def totp_setup_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT totp_secret, totp_enabled FROM users WHERE id = ?", (user["id"],))
    row = await cursor.fetchone()
    secret = row["totp_secret"] or pyotp.random_base32()
    if not row["totp_secret"]:
        await db.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user["id"]))
        await db.commit()

    site_title = await _site_title(db)
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user["email"], issuer_name=site_title)

    import io
    import qrcode
    buf = io.BytesIO()
    qr = qrcode.make(uri)
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return templates.TemplateResponse(
        "auth/totp_setup.html",
        {
            "request": request,
            "secret": secret,
            "qr": qr_b64,
            "enabled": bool(row["totp_enabled"]),
        },
    )


@router.post("/totp/setup")
async def totp_setup_post(
    request: Request,
    code: str = Form(...),
    csrf: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    if not verify_csrf(csrf, request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    cursor = await db.execute("SELECT totp_secret FROM users WHERE id = ?", (user["id"],))
    row = await cursor.fetchone()
    secret = row["totp_secret"]
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        return templates.TemplateResponse(
            "auth/totp_setup.html",
            {"request": request, "error": "Invalid code. Please try again.", "secret": secret},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    await db.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user["id"],))
    await db.commit()
    await _audit(db, user["id"], "totp_enabled", request=request)
    return RedirectResponse(url="/auth/totp/setup?success=1", status_code=303)


# ---------------------------------------------------------------------------
# Passkeys (WebAuthn)
# ---------------------------------------------------------------------------

async def _webauthn_credentials_for_user(db, user_id: int):
    cursor = await db.execute(
        "SELECT id, credential_id, public_key, sign_count, transports FROM webauthn_credentials WHERE user_id = ?",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


def _serialize_webauthn_state(state) -> str:
    return json.dumps({
        "challenge": state["challenge"],  # already a base64url string
        "user_verification": state["user_verification"].value if state.get("user_verification") else None,
    })


def _deserialize_webauthn_state(stored: str):
    d = json.loads(stored or "{}")
    if d.get("user_verification"):
        d["user_verification"] = UserVerificationRequirement(d["user_verification"])
    return d


def _webauthn_options_to_json(options):
    """Convert fido2 WebAuthn options to a JSON-serializable dict with base64url bytes."""
    if isinstance(options, bytes):
        return websafe_encode(options)
    if isinstance(options, Mapping):
        return {k: _webauthn_options_to_json(v) for k, v in options.items()}
    if isinstance(options, list):
        return [_webauthn_options_to_json(v) for v in options]
    return options


@router.get("/passkeys", response_class=HTMLResponse)
async def passkeys_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
    creds = await _webauthn_credentials_for_user(db, user["id"])
    return templates.TemplateResponse(
        "auth/passkeys.html", {"request": request, "credentials": creds}
    )


@router.post("/passkey/register/options")
async def passkey_register_options(request: Request, user=Depends(require_user), db=Depends(get_db)):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    body = await request.json()
    if not verify_csrf(body.get("csrf", ""), csrf_cookie):
        return _json_error("Invalid CSRF token", status.HTTP_403_FORBIDDEN)

    existing = await _webauthn_credentials_for_user(db, user["id"])
    exclude_credentials = [
        {"type": "public-key", "id": base64.urlsafe_b64encode(r["credential_id"]).decode("ascii").rstrip("=")}
        for r in existing
    ]

    user_entity = PublicKeyCredentialUserEntity(
        id=str(user["id"]).encode("utf-8"),
        name=user["email"],
        display_name=user["display_name"] or user["email"],
    )
    options, state = fido_server.register_begin(
        user_entity,
        credentials=exclude_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    token = request.cookies.get(SESSION_COOKIE)
    token_hash = hash_token(token)
    await db.execute(
        "UPDATE sessions SET webauthn_challenge = ?, webauthn_state = ? WHERE token_hash = ?",
        (state["challenge"], _serialize_webauthn_state(state), token_hash),
    )
    await db.commit()
    return JSONResponse(_webauthn_options_to_json(options))


def _json_error(message: str, status_code: int = 400):
    return JSONResponse({"success": False, "error": message}, status_code=status_code)


@router.post("/passkey/register")
async def passkey_register(request: Request, user=Depends(require_user), db=Depends(get_db)):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    body = await request.json()
    if not verify_csrf(body.get("csrf", ""), csrf_cookie):
        return _json_error("Invalid CSRF token", status.HTTP_403_FORBIDDEN)

    token = request.cookies.get(SESSION_COOKIE)
    session = await get_user_by_session(db, token)
    if not session or not session.get("webauthn_state"):
        return _json_error("No pending passkey registration")

    state = _deserialize_webauthn_state(session["webauthn_state"])

    # WebAuthn response expects transports inside the response object.
    if "transports" in body:
        body["response"]["transports"] = body.pop("transports")

    try:
        auth_data = fido_server.register_complete(state, body)
    except ValueError as exc:
        return _json_error(f"Invalid WebAuthn response: {exc}")
    credential = auth_data.credential_data

    await db.execute(
        """
        INSERT INTO webauthn_credentials (credential_id, user_id, public_key, sign_count, transports)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            credential.credential_id,
            user["id"],
            credential.public_key,
            credential.sign_count,
            ",".join(body["response"].get("transports", [])) or None,
        ),
    )
    await db.execute(
        "UPDATE sessions SET webauthn_challenge = NULL, webauthn_state = NULL WHERE token_hash = ?",
        (hash_token(token),),
    )
    await db.commit()
    await _audit(db, user["id"], "passkey_registered", request=request)
    return {"success": True}


@router.post("/passkey/login/options")
async def passkey_login_options(request: Request, db=Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").lower().strip()
    if not email:
        return _json_error("Email required")

    client_ip = await _client_host(request)
    wait_minutes = await ratelimit.retry_after_minutes(db, ratelimit.SCOPE_PASSKEY, email=email, ip=client_ip)
    if wait_minutes:
        return _json_error(ratelimit.throttle_message(wait_minutes), status.HTTP_429_TOO_MANY_REQUESTS)
    await ratelimit.record_failure(db, ratelimit.SCOPE_PASSKEY, email=email, ip=client_ip)

    user = await _get_user_by_email(db, email)
    allow_credentials = []
    if user:
        creds = await _webauthn_credentials_for_user(db, user["id"])
        allow_credentials = [
            {"type": "public-key", "id": base64.urlsafe_b64encode(c["credential_id"]).decode("ascii").rstrip("=")}
            for c in creds
        ]

    options, state = fido_server.authenticate_begin(
        credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    temp_token = generate_token()
    token_hash = hash_token(temp_token)
    expires = now_utc() + timedelta(minutes=5)
    # purpose='webauthn' keeps these short-lived challenge rows out of the
    # session list on /profile and out of the admin session counts.
    await db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, purpose) VALUES (?, ?, ?, 'webauthn')",
        (token_hash, user["id"] if user else -1, expires.isoformat()),
    )
    await db.execute(
        "UPDATE sessions SET webauthn_challenge = ?, webauthn_state = ? WHERE token_hash = ?",
        (state["challenge"], _serialize_webauthn_state(state), token_hash),
    )
    await db.commit()
    return JSONResponse({"options": _webauthn_options_to_json(options), "temp_token": temp_token})


@router.post("/passkey/login")
async def passkey_login(request: Request, response: Response, db=Depends(get_db)):
    body = await request.json()
    temp_token = body.get("temp_token", "")
    token_hash = hash_token(temp_token)
    cursor = await db.execute(
        """
        SELECT id, user_id, expires_at, webauthn_state
        FROM sessions WHERE token_hash = ?
        """,
        (token_hash,),
    )
    row = await cursor.fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) < now_utc():
        return _json_error("Login challenge expired")

    state = _deserialize_webauthn_state(row["webauthn_state"])

    from fido2.webauthn import AuthenticationResponse
    from fido2.cose import CoseKey

    try:
        authentication = AuthenticationResponse.from_dict(body)
    except ValueError as exc:
        return _json_error(f"Invalid WebAuthn response: {exc}")
    cred_id = authentication.raw_id

    cursor = await db.execute(
        "SELECT id, user_id, public_key, sign_count FROM webauthn_credentials WHERE credential_id = ?",
        (cred_id,),
    )
    credential_row = await cursor.fetchone()
    if not credential_row:
        return _json_error("Credential not found")

    class Cred:
        credential_id = credential_row["credential_id"]
        public_key = CoseKey.parse(credential_row["public_key"])

    try:
        fido_server.authenticate_complete(state, [Cred], body)
    except ValueError as exc:
        return _json_error(f"Invalid WebAuthn signature: {exc}")

    sign_count = authentication.response.authenticator_data.sign_count
    await db.execute(
        "UPDATE webauthn_credentials SET sign_count = ? WHERE id = ?",
        (sign_count, credential_row["id"]),
    )
    await db.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
    await db.commit()

    session_token = await create_session(db, credential_row["user_id"], await _client_host(request), request.headers.get("user-agent"))
    _set_session_cookie(response, session_token)
    await _audit(db, credential_row["user_id"], "login_passkey", request=request)

    # A completed passkey login clears the throttle counters for that account.
    cursor = await db.execute("SELECT email FROM users WHERE id = ?", (credential_row["user_id"],))
    row = await cursor.fetchone()
    if row:
        await ratelimit.clear_failures(db, ratelimit.SCOPE_PASSKEY, email=row["email"])
        await ratelimit.clear_failures(db, ratelimit.SCOPE_LOGIN, email=row["email"])
        await ratelimit.unlock(db, row["email"])

    return {"success": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
