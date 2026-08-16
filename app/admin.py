from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_user, get_current_user, CSRF_COOKIE, verify_csrf, hash_password, _audit
from app.db import get_db
from app.models import UserEditForm, SiteSettingsForm
from app.roles import Role, require_role
from app.utils import generate_password, now_utc

router = APIRouter(prefix="/admin", tags=["admin"])

templates = None


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


async def _require_csrf(request: Request):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
    stats = {}
    cursor = await db.execute("SELECT COUNT(*) AS c FROM users")
    stats["users"] = (await cursor.fetchone())["c"]
    cursor = await db.execute("SELECT COUNT(*) AS c FROM videos")
    stats["videos"] = (await cursor.fetchone())["c"]
    # Compare expires_at against a bound UTC timestamp in the same ISO8601
    # format we store (with a "T" separator and +00:00 offset). Using SQLite's
    # datetime('now') here would mix string formats and miscompare same-date
    # boundaries, over-counting active invites.
    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM invites WHERE used_count < max_uses AND expires_at > ?",
        (now_utc().isoformat(),),
    )
    stats["active_invites"] = (await cursor.fetchone())["c"]
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {"request": request, "user": user, "stats": stats, "site_title": await _site_title(db)},
    )


@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
    cursor = await db.execute(
        """
        SELECT id, email, display_name, role, created_at, totp_enabled, is_bootstrap
        FROM users
        ORDER BY created_at DESC
        """
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return templates.TemplateResponse(
        "admin/users.html",
        {"request": request, "user": user, "users": rows, "site_title": await _site_title(db)},
    )


async def _is_bootstrap_user(db, user_id: int) -> bool:
    cursor = await db.execute("SELECT is_bootstrap FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    return bool(row and row["is_bootstrap"])


@router.post("/users/{user_id}/role")
async def change_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    # Only Configurator may change roles.
    require_role(user, Role.CONFIGURATOR)
    await _require_csrf(request)

    if role not in {Role.ADMIN.value, Role.MEMBER.value}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    # The bootstrap user is immutable; their Configurator role (and Admin permissions) cannot be removed.
    if await _is_bootstrap_user(db, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The bootstrap user's role cannot be changed")

    # Configurator accounts cannot have their role changed (only the bootstrap account is full admin).
    target = await db.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    target_row = await target.fetchone()
    if not target_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target_row["role"] == Role.CONFIGURATOR.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A Configurator's role cannot be changed")

    await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    cursor = await db.execute("SELECT role, is_bootstrap FROM users WHERE id = ?", (user_id,))
    target = await cursor.fetchone()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # The bootstrap account is protected from deletion.
    if target["is_bootstrap"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The bootstrap user cannot be deleted")

    # Admins may not delete Configurators or themselves.
    if target["role"] == Role.CONFIGURATOR.value:
        require_role(user, Role.CONFIGURATOR)
    if user_id == user["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete yourself")

    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    # Admins may reset a member's password to a freshly generated value, which
    # is shown once here so it can be delivered to the user out-of-band. The
    # plaintext is never stored or placed in a URL.
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    cursor = await db.execute("SELECT role, is_bootstrap FROM users WHERE id = ?", (user_id,))
    target = await cursor.fetchone()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # The bootstrap account is protected from admin-driven password resets;
    # its owner changes the password from their own profile.
    if target["is_bootstrap"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The bootstrap user's password cannot be reset by an admin")
    # A plain admin learning a Configurator's password would be a privilege
    # escalation, so only a Configurator may reset another Configurator.
    if target["role"] == Role.CONFIGURATOR.value:
        require_role(user, Role.CONFIGURATOR)
    # Self-reset must go through the profile page, which requires the current
    # password — this path bypasses that proof, so refuse it.
    if user_id == user["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use your profile to change your own password")

    new_password = generate_password()
    await db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (await hash_password(new_password), user_id),
    )
    await db.commit()
    await _audit(db, user["id"], "password_reset", target_type="user", target_id=str(user_id), request=request)

    return templates.TemplateResponse(
        "admin/reset_password_result.html",
        {"request": request, "user": user, "new_password": new_password, "site_title": await _site_title(db)},
        status_code=status.HTTP_200_OK,
    )


@router.get("/videos", response_class=HTMLResponse)
async def list_videos(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
    cursor = await db.execute(
        """
        SELECT v.uuid, v.title, v.visibility, v.status, v.created_at,
               u.email AS owner_email, u.id AS owner_id
        FROM videos v
        JOIN users u ON v.owner_id = u.id
        ORDER BY v.created_at DESC
        """
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return templates.TemplateResponse(
        "admin/videos.html",
        {"request": request, "user": user, "videos": rows, "site_title": await _site_title(db)},
    )


@router.post("/videos/{video_uuid}/delete")
async def admin_delete_video(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    cursor = await db.execute("SELECT id, owner_id FROM videos WHERE uuid = ?", (video_uuid,))
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    # Admins cannot delete videos owned by Configurators unless they are Configurator.
    if video["owner_id"] != user["id"]:
        cursor = await db.execute("SELECT role FROM users WHERE id = ?", (video["owner_id"],))
        owner = await cursor.fetchone()
        if owner and owner["role"] == Role.CONFIGURATOR.value:
            require_role(user, Role.CONFIGURATOR)

    await db.execute("DELETE FROM videos WHERE uuid = ?", (video_uuid,))
    await db.commit()
    # Files are left on disk; a cleanup job would normally purge them.
    return RedirectResponse(url="/admin/videos", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.CONFIGURATOR)
    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "user": user,
            "site_title": await _site_title(db),
        },
    )


@router.post("/settings")
async def settings_post(
    request: Request,
    site_title: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.CONFIGURATOR)
    await _require_csrf(request)

    title = site_title.strip()
    if not title or len(title) > 120:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid site title")

    await db.execute(
        "UPDATE config SET site_title = ?, updated_at = ? WHERE id = 1",
        (title, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings?success=1", status_code=303)
