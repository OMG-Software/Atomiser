from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import (
    require_user,
    CSRF_COOKIE,
    SESSION_COOKIE,
    verify_csrf,
    get_current_user,
    hash_password,
    password_policy_error,
    verify_password,
    _audit,
)
from app.db import get_db
from app.roles import Role, has_role
from app.utils import now_utc

router = APIRouter(tags=["users"])

templates = None


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


async def _own_videos(db, user_id: int) -> list:
    cursor = await db.execute(
        """
        SELECT v.uuid, v.title, v.description, v.visibility, v.status, v.created_at, v.thumbnail_path,
               v.duration_seconds
        FROM videos v
        WHERE v.owner_id = ?
        ORDER BY v.created_at DESC
        """,
        (user_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _active_sessions(db, user_id: int) -> list:
    """Live login sessions for a user, most recently used first.

    WebAuthn challenge rows share the sessions table but are not logins, so
    they are filtered out by purpose.
    """
    cursor = await db.execute(
        """
        SELECT id, created_at, last_used_at, expires_at, ip, user_agent
        FROM sessions
        WHERE user_id = ? AND purpose = 'session' AND expires_at > ?
        ORDER BY last_used_at DESC, id DESC
        """,
        (user_id, now_utc().isoformat()),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _profile_context(db, user, **extra) -> dict:
    """Context for rendering the current user's own profile page."""
    return {
        "user": user,
        "profile_user": user,
        "videos": await _own_videos(db, user["id"]),
        "sessions": await _active_sessions(db, user["id"]),
        "current_session_id": user.get("session_id"),
        "site_title": await _site_title(db),
        **extra,
    }


@router.get("/profile", response_class=HTMLResponse)
async def my_profile(request: Request, user=Depends(require_user), db=Depends(get_db)):
    return templates.TemplateResponse(
        "users/profile.html",
        {"request": request, **await _profile_context(db, user)},
    )


@router.post("/profile/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    # CSRF is read from the form body (not declared as a Form param) so a
    # missing token returns 403 like the other state-changing routes rather
    # than a 422 validation error.
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    new_password = new_password.strip()

    error = None
    cursor = await db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
    row = await cursor.fetchone()
    if not row or not await verify_password(current_password, row["password_hash"]):
        error = "Current password is incorrect."
    elif new_password != confirm_password.strip():
        error = "New password and confirmation do not match."
    elif new_password == current_password.strip():
        error = "New password must be different from your current password."
    else:
        policy = password_policy_error(new_password)
        if policy:
            error = policy

    if error:
        return templates.TemplateResponse(
            "users/profile.html",
            {"request": request, **await _profile_context(db, user, password_error=error)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (await hash_password(new_password), user["id"]),
    )
    await db.commit()
    await _audit(db, user["id"], "password_changed", request=request)
    return RedirectResponse(url="/profile?password_changed=1", status_code=303)


@router.post("/profile")
async def update_profile(
    request: Request,
    display_name: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    await db.execute(
        "UPDATE users SET display_name = ? WHERE id = ?",
        (display_name.strip(), user["id"]),
    )
    await db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/profile/sessions/{session_id}/revoke")
async def revoke_session(
    session_id: int,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Sign out one of your own sessions."""
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    # Scoping the DELETE by user_id is what stops one user revoking another's
    # session by guessing an id.
    cursor = await db.execute(
        "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    await _audit(db, user["id"], "session_revoked", target_type="session", target_id=str(session_id), request=request)

    # Revoking the session you are using is a logout.
    if session_id == user.get("session_id"):
        resp = RedirectResponse(url="/auth/login", status_code=303)
        resp.delete_cookie(key=SESSION_COOKIE, path="/")
        return resp
    return RedirectResponse(url="/profile?sessions_revoked=1", status_code=303)


@router.post("/profile/sessions/revoke-others")
async def revoke_other_sessions(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Sign out every session except the one making this request."""
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    cursor = await db.execute(
        "DELETE FROM sessions WHERE user_id = ? AND id != ?",
        (user["id"], user.get("session_id") or -1),
    )
    await db.commit()
    await _audit(db, user["id"], "sessions_revoked", target_type="user", target_id=str(user["id"]), request=request)
    return RedirectResponse(url=f"/profile?sessions_revoked={cursor.rowcount or 0}", status_code=303)


@router.get("/u/{user_id}", response_class=HTMLResponse)
async def user_profile(
    user_id: int,
    request: Request,
    current=Depends(require_user),
    db=Depends(get_db),
):
    cursor = await db.execute(
        "SELECT id, email, display_name, role, created_at FROM users WHERE id = ?",
        (user_id,),
    )
    profile_user = await cursor.fetchone()
    if not profile_user:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    profile_user = dict(profile_user)

    # Visibility rules
    is_owner = current and current["id"] == user_id
    is_admin = current and has_role(current["role"], Role.ADMIN)

    if is_owner or is_admin:
        visibility_clause = ""
        params = (user_id,)
    else:
        visibility_clause = "AND v.visibility = 'site'"
        params = (user_id,)

    cursor = await db.execute(
        f"""
        SELECT v.uuid, v.title, v.description, v.visibility, v.status, v.created_at, v.thumbnail_path,
               v.duration_seconds
        FROM videos v
        WHERE v.owner_id = ? {visibility_clause}
        ORDER BY v.created_at DESC
        """,
        params,
    )
    videos = [dict(r) for r in await cursor.fetchall()]

    return templates.TemplateResponse(
        "users/profile.html",
        {
            "request": request,
            "user": current,
            "profile_user": profile_user,
            "videos": videos,
            "site_title": await _site_title(db),
        },
    )
