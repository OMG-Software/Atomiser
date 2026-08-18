from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app import jobs, mail
from app.config import Config
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
        "SELECT COUNT(*) AS c FROM invites WHERE used_count < max_uses AND expires_at > ? AND revoked_at IS NULL",
        (now_utc().isoformat(),),
    )
    stats["active_invites"] = (await cursor.fetchone())["c"]

    # Videos grouped by status, so a stuck or failed transcode is visible here
    # rather than only to whoever opens the SQLite file.
    cursor = await db.execute("SELECT status, COUNT(*) AS c FROM videos GROUP BY status")
    by_status = {row["status"]: row["c"] for row in await cursor.fetchall()}
    stats["by_status"] = by_status
    stats["processing"] = by_status.get("uploading", 0) + by_status.get("processing", 0)
    stats["failed"] = by_status.get("failed", 0)

    # Storage: rendition files plus any original still on disk. raw_path is
    # cleared when the original is purged, so this does not double-count.
    cursor = await db.execute(
        """
        SELECT
            (SELECT COALESCE(SUM(size_bytes), 0) FROM video_renditions) AS rendition_bytes,
            (SELECT COALESCE(SUM(raw_size_bytes), 0) FROM videos WHERE raw_path IS NOT NULL) AS raw_bytes
        """
    )
    row = await cursor.fetchone()
    stats["storage_bytes"] = (row["rendition_bytes"] or 0) + (row["raw_bytes"] or 0)

    cursor = await db.execute(
        """
        SELECT u.id, u.email, u.display_name,
               (SELECT COUNT(*) FROM videos v WHERE v.owner_id = u.id) AS video_count,
               (SELECT COALESCE(SUM(v.raw_size_bytes), 0) FROM videos v
                 WHERE v.owner_id = u.id AND v.raw_path IS NOT NULL) AS raw_bytes,
               (SELECT COALESCE(SUM(r.size_bytes), 0) FROM video_renditions r
                  JOIN videos v ON r.video_id = v.id WHERE v.owner_id = u.id) AS rendition_bytes
        FROM users u
        ORDER BY (raw_bytes + rendition_bytes) DESC, u.id ASC
        LIMIT 10
        """
    )
    storage_by_user = []
    for row in await cursor.fetchall():
        entry = dict(row)
        entry["total_bytes"] = (entry["raw_bytes"] or 0) + (entry["rendition_bytes"] or 0)
        storage_by_user.append(entry)

    cursor = await db.execute(
        "SELECT id, email, display_name, role, created_at FROM users ORDER BY id DESC LIMIT 5"
    )
    recent_users = [dict(r) for r in await cursor.fetchall()]

    # Failed jobs, so there is something to click "retry" on.
    cursor = await db.execute(
        """
        SELECT j.id, j.attempts, j.last_error, j.finished_at, v.uuid, v.title
        FROM transcode_jobs j
        JOIN videos v ON j.video_id = v.id
        WHERE j.status = 'failed'
        ORDER BY j.id DESC
        LIMIT 10
        """
    )
    failed_jobs = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM transcode_jobs WHERE status IN ('queued', 'running')"
    )
    stats["queued_jobs"] = (await cursor.fetchone())["c"]

    # Outbound email. Members are notified about new videos, so a silently
    # broken mail server is worth surfacing here rather than only in the log.
    cursor = await db.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('queued', 'sending') THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM email_queue
        """
    )
    row = await cursor.fetchone()
    stats["email_pending"] = row["pending"] or 0
    stats["email_sent"] = row["sent"] or 0
    stats["email_failed"] = row["failed"] or 0

    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE notify_new_videos = 1"
    )
    stats["email_subscribers"] = (await cursor.fetchone())["c"]

    cursor = await db.execute(
        """
        SELECT id, to_address, subject, attempts, last_error, created_at
        FROM email_queue WHERE status = 'failed' ORDER BY id DESC LIMIT 10
        """
    )
    failed_emails = [dict(r) for r in await cursor.fetchall()]

    mail_status = {
        "enabled": mail.mail_enabled(),
        "notifications_on": Config.NOTIFY_NEW_VIDEOS,
        # Every emailed link is built from SITE_URL. Without it, notifications
        # cannot be sent at all and password recovery is refused, because the
        # only other source of a hostname is the client-controlled Host header.
        "site_url_missing": bool(mail.mail_enabled() and not Config.SITE_URL),
    }

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "storage_by_user": storage_by_user,
            "recent_users": recent_users,
            "failed_jobs": failed_jobs,
            "failed_emails": failed_emails,
            "mail_status": mail_status,
            "site_title": await _site_title(db),
        },
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

AUDIT_PAGE_SIZE = 50


@router.get("/audit", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    action: str = Query(""),
    user_id: Optional[int] = Query(None),
    since: str = Query(""),
    until: str = Query(""),
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Read back the audit trail the app has always written but never shown."""
    require_role(user, Role.ADMIN)

    where = []
    params = []
    if action:
        where.append("a.action = ?")
        params.append(action)
    if user_id:
        where.append("a.user_id = ?")
        params.append(user_id)
    # Dates arrive as YYYY-MM-DD from <input type="date">. Comparing them as
    # strings works because every stored timestamp starts with the same format.
    if since:
        where.append("a.created_at >= ?")
        params.append(since)
    if until:
        where.append("a.created_at <= ?")
        params.append(until + " 23:59:59")

    clause = ("WHERE " + " AND ".join(where)) if where else ""

    cursor = await db.execute(f"SELECT COUNT(*) AS c FROM audit_log a {clause}", params)
    total = (await cursor.fetchone())["c"]

    cursor = await db.execute(
        f"""
        SELECT a.id, a.action, a.target_type, a.target_id, a.ip, a.user_agent, a.created_at,
               u.email AS user_email, u.id AS user_id
        FROM audit_log a
        LEFT JOIN users u ON a.user_id = u.id
        {clause}
        ORDER BY a.id DESC
        LIMIT ? OFFSET ?
        """,
        params + [AUDIT_PAGE_SIZE, (page - 1) * AUDIT_PAGE_SIZE],
    )
    entries = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
    actions = [r["action"] for r in await cursor.fetchall()]

    return templates.TemplateResponse(
        "admin/audit.html",
        {
            "request": request,
            "user": user,
            "entries": entries,
            "actions": actions,
            "filters": {"action": action, "user_id": user_id, "since": since, "until": until},
            "page": page,
            "total": total,
            "page_size": AUDIT_PAGE_SIZE,
            "has_next": page * AUDIT_PAGE_SIZE < total,
            "site_title": await _site_title(db),
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request, user=Depends(require_user), db=Depends(get_db)):
    require_role(user, Role.ADMIN)
    cursor = await db.execute(
        """
        SELECT u.id, u.email, u.display_name, u.role, u.created_at, u.totp_enabled, u.is_bootstrap,
               (SELECT COUNT(*) FROM sessions s
                 WHERE s.user_id = u.id AND s.purpose = 'session' AND s.expires_at > ?)
                   AS session_count
        FROM users u
        ORDER BY u.created_at DESC
        """,
        (now_utc().isoformat(),),
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
    await _audit(
        db, user["id"], "role_changed",
        target_type="user", target_id=f"{user_id}:{target_row['role']}->{role}", request=request,
    )
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

    # Remove the user's media before the cascade drops the rows that point at it.
    from app.videos import purge_video_files

    cursor = await db.execute(
        "SELECT id, uuid, raw_path, thumbnail_path FROM videos WHERE owner_id = ?", (user_id,)
    )
    for video in [dict(r) for r in await cursor.fetchall()]:
        await purge_video_files(db, video)

    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    await _audit(db, user["id"], "user_deleted", target_type="user", target_id=str(user_id), request=request)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/logout")
async def force_logout_user(
    user_id: int,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Revoke every session belonging to a user, signing them out everywhere."""
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    cursor = await db.execute("SELECT role, is_bootstrap FROM users WHERE id = ?", (user_id,))
    target = await cursor.fetchone()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Signing out a Configurator (or the bootstrap account) is a Configurator's
    # call, matching the password-reset rule above.
    if target["is_bootstrap"] or target["role"] == Role.CONFIGURATOR.value:
        require_role(user, Role.CONFIGURATOR)

    cursor = await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    await db.commit()
    await _audit(
        db, user["id"], "sessions_revoked",
        target_type="user", target_id=str(user_id), request=request,
    )
    return RedirectResponse(url=f"/admin/users?logged_out={cursor.rowcount or 0}", status_code=303)


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
async def list_videos(
    request: Request,
    video_status: str = Query("", alias="status"),
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    clause, params = "", []
    if video_status:
        clause = "WHERE v.status = ?"
        params.append(video_status)

    cursor = await db.execute(
        f"""
        SELECT v.uuid, v.title, v.visibility, v.status, v.created_at,
               u.email AS owner_email, u.id AS owner_id,
               (CASE WHEN v.raw_path IS NOT NULL THEN COALESCE(v.raw_size_bytes, 0) ELSE 0 END)
                 + (SELECT COALESCE(SUM(r.size_bytes), 0) FROM video_renditions r WHERE r.video_id = v.id)
                   AS total_bytes,
               (SELECT j.status FROM transcode_jobs j WHERE j.video_id = v.id ORDER BY j.id DESC LIMIT 1)
                   AS job_status,
               (SELECT j.last_error FROM transcode_jobs j WHERE j.video_id = v.id ORDER BY j.id DESC LIMIT 1)
                   AS job_error
        FROM videos v
        JOIN users u ON v.owner_id = u.id
        {clause}
        ORDER BY v.created_at DESC
        """,
        params,
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return templates.TemplateResponse(
        "admin/videos.html",
        {
            "request": request,
            "user": user,
            "videos": rows,
            "status_filter": video_status,
            "site_title": await _site_title(db),
        },
    )


@router.post("/videos/{video_uuid}/retry")
async def retry_video_transcode(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Requeue a video whose transcode failed."""
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    cursor = await db.execute("SELECT id, raw_path FROM videos WHERE uuid = ?", (video_uuid,))
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    # Once the original has been purged there is nothing left to transcode from.
    if not video["raw_path"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The original upload is no longer on disk, so this video cannot be reprocessed.",
        )

    if not await jobs.retry_video(db, video["id"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This video is already queued for processing.",
        )
    await db.commit()
    await _audit(db, user["id"], "transcode_retried", target_type="video", target_id=video_uuid, request=request)
    return RedirectResponse(url="/admin/videos?status=uploading", status_code=303)


@router.post("/videos/{video_uuid}/delete")
async def admin_delete_video(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    require_role(user, Role.ADMIN)
    await _require_csrf(request)

    from app.videos import purge_video_files

    cursor = await db.execute(
        "SELECT id, uuid, owner_id, raw_path, thumbnail_path FROM videos WHERE uuid = ?",
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    # Admins cannot delete videos owned by Configurators unless they are Configurator.
    if video["owner_id"] != user["id"]:
        cursor = await db.execute("SELECT role FROM users WHERE id = ?", (video["owner_id"],))
        owner = await cursor.fetchone()
        if owner and owner["role"] == Role.CONFIGURATOR.value:
            require_role(user, Role.CONFIGURATOR)

    # Remove the media too. Deleting only the row used to leave the original,
    # every rendition and the thumbnail on disk indefinitely.
    await purge_video_files(db, dict(video))
    await db.execute("DELETE FROM videos WHERE uuid = ?", (video_uuid,))
    await db.commit()
    await _audit(db, user["id"], "video_deleted", target_type="video", target_id=video_uuid, request=request)
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
    await _audit(db, user["id"], "settings_changed", target_type="config", target_id=title, request=request)
    return RedirectResponse(url="/admin/settings?success=1", status_code=303)
