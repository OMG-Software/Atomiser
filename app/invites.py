from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_user, CSRF_COOKIE, verify_csrf
from app.db import get_db
from app.roles import Role, require_role
from app.utils import generate_token, hash_token, now_utc

router = APIRouter(prefix="/invites", tags=["invites"])

templates = None  # initialised in main.py


@router.get("/", response_class=HTMLResponse)
async def invites_list(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
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
    for r in await cursor.fetchall():
        inv = dict(r)
        expired = datetime.fromisoformat(inv["expires_at"]) < now_utc()
        inv["status_label"] = "Used / expired" if inv["used_count"] >= inv["max_uses"] or expired else "Active"
        invites.append(inv)
    return templates.TemplateResponse(
        "admin/invites.html",
        {"request": request, "user": user, "invites": invites},
    )


@router.post("/create")
async def invite_create(
    request: Request,
    response: Response,
    expires_hours: int = Form(48),
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    token = generate_token()
    token_hash = hash_token(token)
    expires = now_utc() + timedelta(hours=max(1, min(expires_hours, 168)))
    await db.execute(
        "INSERT INTO invites (token_hash, created_by, max_uses, expires_at) VALUES (?, ?, 1, ?)",
        (token_hash, user["id"], expires.isoformat()),
    )
    await db.commit()

    # Build absolute registration URL
    invite_url = f"{request.base_url}auth/register?token={token}"
    return templates.TemplateResponse(
        "admin/invites.html",
        {
            "request": request,
            "user": user,
            "new_invite_url": invite_url,
            "expires_hours": expires_hours,
        },
    )
