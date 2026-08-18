from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app import mail
from app.auth import require_user, CSRF_COOKIE, verify_csrf, _audit
from app.config import Config
from app.db import get_db
from app.roles import Role, require_role
from app.utils import generate_token, hash_token, now_utc

router = APIRouter(prefix="/invites", tags=["invites"])

templates = None  # initialised in main.py

MAX_USES_LIMIT = 25


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


async def _invite_rows(db) -> list:
    cursor = await db.execute(
        """
        SELECT i.*, creator.email AS creator_email, u.email AS used_by_email
        FROM invites i
        LEFT JOIN users creator ON i.created_by = creator.id
        LEFT JOIN users u ON i.used_by = u.id
        ORDER BY i.created_at DESC
        """
    )
    invites = []
    now = now_utc()
    for row in await cursor.fetchall():
        inv = dict(row)
        expired = datetime.fromisoformat(inv["expires_at"]) < now
        exhausted = inv["used_count"] >= inv["max_uses"]
        if inv.get("revoked_at"):
            inv["status_label"] = "Revoked"
        elif exhausted:
            inv["status_label"] = "Fully used"
        elif expired:
            inv["status_label"] = "Expired"
        else:
            inv["status_label"] = "Active"
        inv["is_active"] = not (inv.get("revoked_at") or exhausted or expired)
        invites.append(inv)
    return invites


async def _invites_context(db, user, request, **extra) -> dict:
    return {
        "request": request,
        "user": user,
        "invites": await _invite_rows(db),
        "site_title": await _site_title(db),
        "mail_enabled": mail.mail_enabled(),
        "max_uses_limit": MAX_USES_LIMIT,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def invites_list(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
    return templates.TemplateResponse(
        "admin/invites.html", await _invites_context(db, user, request)
    )


@router.post("/create")
async def invite_create(
    request: Request,
    response: Response,
    expires_hours: int = Form(48),
    max_uses: int = Form(1),
    note: str = Form(""),
    send_to: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    expires_hours = max(1, min(expires_hours, 168))
    max_uses = max(1, min(max_uses, MAX_USES_LIMIT))
    note = note.strip()[:200] or None
    send_to = send_to.strip().lower()

    token = generate_token()
    token_hash = hash_token(token)
    expires = now_utc() + timedelta(hours=expires_hours)
    cursor = await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at, note) VALUES (?, ?, ?, ?, ?)",
        (token_hash, user["id"], max_uses, expires.isoformat(), note),
    )
    await db.commit()
    invite_id = cursor.lastrowid
    await _audit(db, user["id"], "invite_created", target_type="invite", target_id=str(invite_id), request=request)

    # The raw token is never stored, so this is the only time it can be shown.
    # display_url may fall back to the request host, which is safe because the
    # result is rendered straight back to the admin who asked for it.
    invite_url = mail.display_url(request, f"/auth/register?token={token}")

    email_status = None
    if send_to:
        if not mail.mail_enabled():
            email_status = ("error", "Email is not configured on this server, so the link was not sent.")
        elif not mail.email_links_available():
            # Never put a Host-header-derived URL in an email.
            email_status = (
                "error",
                "SITE_URL is not set on this server, so an invite link cannot be emailed safely. "
                "Copy the link below instead.",
            )
        else:
            sent = await mail.send_invite(
                send_to,
                mail.email_link(f"/auth/register?token={token}"),
                await _site_title(db),
                user.get("display_name") or user["email"],
                expires_hours,
            )
            if sent:
                email_status = ("success", f"Invite emailed to {send_to}.")
                await _audit(
                    db, user["id"], "invite_emailed",
                    target_type="invite", target_id=str(invite_id), request=request,
                )
            else:
                email_status = ("error", "The invite was created, but the email could not be sent. Copy the link below instead.")

    return templates.TemplateResponse(
        "admin/invites.html",
        await _invites_context(
            db, user, request,
            new_invite_url=invite_url,
            expires_hours=expires_hours,
            email_status=email_status,
        ),
    )


@router.post("/{invite_id}/revoke")
async def invite_revoke(
    invite_id: int,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Revoke an invite that has not been fully used yet."""
    require_role(user, Role.ADMIN)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    cursor = await db.execute("SELECT id, revoked_at FROM invites WHERE id = ?", (invite_id,))
    invite = await cursor.fetchone()
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")

    await db.execute(
        "UPDATE invites SET revoked_at = ? WHERE id = ?", (now_utc().isoformat(), invite_id)
    )
    await db.commit()
    await _audit(db, user["id"], "invite_revoked", target_type="invite", target_id=str(invite_id), request=request)
    return RedirectResponse(url="/invites/?revoked=1", status_code=303)
