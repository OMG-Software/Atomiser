import asyncio
import filetype
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth import require_user, CSRF_COOKIE, verify_csrf, get_current_user, _audit
from app.config import Config
from app.db import get_db
from app import jobs
from app.roles import Role, has_role, require_role
from app.utils import generate_token, hash_token, new_video_uuid, now_utc

router = APIRouter(tags=["videos"])

templates = None

logger = logging.getLogger(__name__)

# Allowed video MIME types and extensions. We are strict about what ffmpeg will touch.
ALLOWED_TYPES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}

RENDITIONS = [
    {"label": "720p", "height": 720, "video_bitrate": "2500k", "audio_bitrate": "128k"},
    {"label": "480p", "height": 480, "video_bitrate": "1200k", "audio_bitrate": "96k"},
    {"label": "360p", "height": 360, "video_bitrate": "700k", "audio_bitrate": "64k"},
]


def _raw_dir() -> Path:
    return Config.UPLOAD_DIR / "raw"


def _videos_dir() -> Path:
    return Config.UPLOAD_DIR / "videos"


async def _site_title(db) -> str:
    cursor = await db.execute("SELECT site_title FROM config WHERE id = 1")
    row = await cursor.fetchone()
    return row["site_title"] if row else "Atomiser"


def _readable_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _can_moderate(user) -> bool:
    """True if the user holds Admin or above."""
    return bool(user) and has_role(user["role"], Role.ADMIN)


# ---------------------------------------------------------------------------
# Feed and player
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db=Depends(get_db),
):
    per_page = 12
    offset = (page - 1) * per_page
    cursor = await db.execute(
        """
        SELECT v.uuid, v.title, v.description, v.status, v.created_at, v.thumbnail_path,
               v.duration_seconds, u.id AS owner_id, u.display_name AS owner_name
        FROM videos v
        JOIN users u ON v.owner_id = u.id
        WHERE v.visibility = 'site' AND v.status = 'ready'
        ORDER BY v.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (per_page, offset),
    )
    videos = [dict(r) for r in await cursor.fetchall()]
    return templates.TemplateResponse(
        "videos/feed.html",
        {
            "request": request,
            "user": user,
            "videos": videos,
            "page": page,
            "site_title": await _site_title(db),
        },
    )


@router.get("/videos/{video_uuid}", response_class=HTMLResponse)
async def video_page(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    cursor = await db.execute(
        """
        SELECT v.*, u.id AS owner_id, u.display_name AS owner_name, u.email AS owner_email
        FROM videos v
        JOIN users u ON v.owner_id = u.id
        WHERE v.uuid = ?
        """,
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    video = dict(video)
    is_owner = user["id"] == video["owner_id"]
    is_admin = _can_moderate(user)

    if video["visibility"] == "private" and not (is_owner or is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Private video")

    ready_renditions = await _ready_renditions(db, video["id"])

    return templates.TemplateResponse(
        "videos/player.html",
        {
            "request": request,
            "user": user,
            "video": video,
            "renditions": ready_renditions,
            "site_title": await _site_title(db),
            "is_owner": is_owner,
            "is_admin": is_admin,
            "progress": await _processing_progress(db, video["id"]),
        },
    )


async def _ready_renditions(db, video_id: int) -> list:
    """Ready renditions, best quality first, with a URL-safe filename.

    The filename is derived here rather than in the template: file_path is an
    OS-native path, so splitting on "/" in Jinja produces the whole path on
    Windows and breaks every source URL.
    """
    cursor = await db.execute(
        """
        SELECT label, file_path, width, height, size_bytes, status
        FROM video_renditions
        WHERE video_id = ? AND status = 'ready'
        ORDER BY height DESC
        """,
        (video_id,),
    )
    renditions = []
    for row in await cursor.fetchall():
        rendition = dict(row)
        rendition["filename"] = Path(rendition["file_path"]).name
        renditions.append(rendition)
    return renditions


async def _processing_progress(db, video_id: int) -> dict:
    """Rendition counts and the last error, for the processing panel."""
    cursor = await db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM video_renditions WHERE video_id = ?
        """,
        (video_id,),
    )
    row = await cursor.fetchone()
    total = row["total"] or 0
    ready = row["ready"] or 0

    cursor = await db.execute(
        "SELECT status, attempts, last_error FROM transcode_jobs WHERE video_id = ? ORDER BY id DESC LIMIT 1",
        (video_id,),
    )
    job = await cursor.fetchone()

    return {
        "total": total,
        "ready": ready,
        "failed": row["failed"] or 0,
        # Before the rendition rows exist there is nothing to measure, so the
        # template shows an indeterminate state rather than a misleading 0%.
        "percent": int(ready * 100 / total) if total else 0,
        "job_status": job["status"] if job else None,
        "attempts": job["attempts"] if job else 0,
        "last_error": job["last_error"] if job else None,
    }


@router.get("/videos/{video_uuid}/status")
async def video_status(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """JSON status for the player page to poll while a video is transcoding."""
    cursor = await db.execute(
        "SELECT id, owner_id, visibility, status FROM videos WHERE uuid = ?",
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    is_owner = user["id"] == video["owner_id"]
    if video["visibility"] == "private" and not (is_owner or _can_moderate(user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Private video")

    progress = await _processing_progress(db, video["id"])
    return JSONResponse({
        "status": video["status"],
        "ready": video["status"] == "ready",
        "renditions_ready": progress["ready"],
        "renditions_total": progress["total"],
        "percent": progress["percent"],
    })


@router.post("/videos/{video_uuid}/edit")
async def edit_video(
    video_uuid: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    visibility: str = Form(None),
    user=Depends(require_user),
    db=Depends(get_db),
):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    title = title.strip()
    description = description.strip()
    if not title or len(title) > 200 or len(description) > 5000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid title or description")

    cursor = await db.execute(
        "SELECT id, owner_id, visibility FROM videos WHERE uuid = ?",
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    is_owner = user["id"] == video["owner_id"]
    is_admin = _can_moderate(user)

    if not (is_owner or is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot edit this video")

    # Visibility is optional so older clients (and the tests) can post just the
    # title and description without silently flipping a private video public.
    new_visibility = video["visibility"]
    if visibility is not None:
        if visibility not in ("site", "private"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid visibility")
        new_visibility = visibility

    await db.execute(
        "UPDATE videos SET title = ?, description = ?, visibility = ? WHERE id = ?",
        (title, description, new_visibility, video["id"]),
    )
    await db.commit()
    if new_visibility != video["visibility"]:
        await _audit(
            db, user["id"], "video_visibility_changed",
            target_type="video", target_id=video_uuid, request=request,
        )
        # A video published after the fact still deserves its announcement.
        # queue_new_video_notifications() is a no-op unless it is now ready,
        # site-visible, and has never been announced.
        if new_visibility == "site":
            from app import notifications

            try:
                await notifications.queue_new_video_notifications(db, video["id"])
            except Exception:  # noqa: BLE001 - the edit itself succeeded
                logger.exception("Could not queue notifications for video %s", video_uuid)
    return RedirectResponse(url=f"/videos/{video_uuid}?success=1", status_code=303)


@router.post("/videos/{video_uuid}/delete")
async def delete_own_video(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Delete a video you own (admins may delete any, subject to the same
    Configurator protection the admin panel applies)."""
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    cursor = await db.execute(
        "SELECT id, uuid, owner_id, raw_path, thumbnail_path FROM videos WHERE uuid = ?",
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    is_owner = user["id"] == video["owner_id"]
    if not (is_owner or _can_moderate(user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete this video")

    if not is_owner:
        # An admin may not delete a Configurator's video; only a Configurator may.
        cursor = await db.execute("SELECT role FROM users WHERE id = ?", (video["owner_id"],))
        owner = await cursor.fetchone()
        if owner and owner["role"] == Role.CONFIGURATOR.value:
            require_role(user, Role.CONFIGURATOR)

    await purge_video_files(db, dict(video))
    await db.execute("DELETE FROM videos WHERE id = ?", (video["id"],))
    await db.commit()
    await _audit(db, user["id"], "video_deleted", target_type="video", target_id=video_uuid, request=request)

    return RedirectResponse(url="/profile" if is_owner else "/admin/videos", status_code=303)


# ---------------------------------------------------------------------------
# File cleanup
# ---------------------------------------------------------------------------

async def purge_video_files(db, video: dict) -> None:
    """Remove every file belonging to a video: raw upload, renditions, thumbnail.

    Deleting the row alone used to leave all of it on disk, so a deleted 500 MB
    upload still occupied roughly a gigabyte forever.
    """
    paths = []
    if video.get("raw_path"):
        paths.append(Path(video["raw_path"]))
    if video.get("thumbnail_path"):
        paths.append(Path(video["thumbnail_path"]))

    cursor = await db.execute(
        "SELECT file_path FROM video_renditions WHERE video_id = ?", (video["id"],)
    )
    paths.extend(Path(r["file_path"]) for r in await cursor.fetchall())

    await run_in_threadpool(_unlink_all, paths)
    # The per-video rendition directory is ours, so remove it once emptied.
    await run_in_threadpool(_remove_video_dir, video.get("uuid"))


def _unlink_all(paths) -> None:
    for path in paths:
        try:
            if path and _is_within_upload_dir(path):
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)


def _remove_video_dir(video_uuid) -> None:
    if not video_uuid:
        return
    out_dir = _videos_dir() / video_uuid
    try:
        if out_dir.is_dir() and _is_within_upload_dir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
    except OSError as exc:
        logger.warning("Could not remove %s: %s", out_dir, exc)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
    return templates.TemplateResponse(
        "videos/upload.html",
        {"request": request, "user": user, "max_upload_mb": Config.MAX_UPLOAD_MB, "site_title": await _site_title(db)},
    )


@router.post("/upload")
async def upload_video(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("site"),
    video: UploadFile = File(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    if not verify_csrf(form.get("csrf", ""), csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    if visibility not in ("site", "private"):
        visibility = "site"

    # File size guard: trust Content-Length, but also read in chunks later.
    content_length = request.headers.get("content-length")
    max_bytes = Config.MAX_UPLOAD_MB * 1024 * 1024
    if content_length:
        try:
            declared_len = int(content_length)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length")
        if declared_len > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Upload too large")

    # Read a chunk to inspect magic bytes, then stream the rest to disk.
    chunk = await video.read(8192)
    kind = filetype.guess(chunk)
    if not kind or kind.mime not in ALLOWED_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported video format")

    ext = ALLOWED_TYPES[kind.mime]
    video_uuid = new_video_uuid()
    raw_path = _raw_dir() / f"{video_uuid}{ext}"

    # Stream the upload to disk in a thread so the event loop stays free and
    # memory usage stays bounded regardless of file size.
    await run_in_threadpool(_write_upload, raw_path, video.file, chunk)

    # Verify actual file size after write.
    size = raw_path.stat().st_size
    if size > max_bytes:
        raw_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Upload too large")

    # Reject uploads whose resolution exceeds 1080p before any transcoding work begins.
    height = await _probe_height(raw_path)
    if height is not None and height > 1080:
        raw_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video resolution exceeds 1080p. Please upload a 1080p or smaller file.",
        )

    cursor = await db.execute(
        """
        INSERT INTO videos (uuid, owner_id, title, description, visibility, status, raw_path, raw_size_bytes)
        VALUES (?, ?, ?, ?, ?, 'uploading', ?, ?)
        """,
        (video_uuid, user["id"], title.strip(), description.strip(), visibility, str(raw_path), size),
    )
    await db.commit()
    video_id = cursor.lastrowid

    # Queue transcoding. A job row outlives this process, so a restart between
    # here and the transcode finishing no longer strands the video.
    await jobs.enqueue(db, video_id)

    return RedirectResponse(url=f"/videos/{video_uuid}", status_code=303)


def _write_upload(path: Path, file_obj, initial_chunk: bytes):
    """Stream an upload to disk without holding the entire payload in memory."""
    with open(path, "wb") as f:
        f.write(initial_chunk)
        shutil.copyfileobj(file_obj, f)


# ---------------------------------------------------------------------------
# Chunked upload (for files larger than Cloudflare's edge body limit)
# ---------------------------------------------------------------------------
#
# The browser splits the file into <100 MB chunks and POSTs each to
# /upload/chunk; the server stages them in uploads/raw/.staging/<id>.part.
# A final /upload/complete validates the assembled file (magic bytes, size,
# resolution), moves it into place, and kicks off transcoding — exactly as
# the single-shot /upload route does. Each chunk request carries the CSRF
# token and the session cookie, so auth/CSRF are enforced per request.
#
# The single-shot /upload route above is kept as the no-JS / small-file path.

_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
STAGING_MAX_AGE_SECONDS = 6 * 3600


def _staging_dir() -> Path:
    d = Config.UPLOAD_DIR / "raw" / ".staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _staging_paths(upload_id: str):
    base = _staging_dir() / upload_id
    return base.with_suffix(".part"), base.with_suffix(".meta")


def _read_meta(meta_path: Path) -> dict:
    try:
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pass
    return {}


def _write_meta(meta_path: Path, meta: dict) -> None:
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _write_chunk_at(part_path: Path, offset: int, data: bytes) -> None:
    mode = "r+b" if part_path.exists() else "w+b"
    with open(part_path, mode) as f:
        f.seek(offset)
        f.write(data)


def _delete_staging(part_path: Path, meta_path: Path) -> None:
    part_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)


def _cleanup_staging() -> None:
    """Remove staging files older than STAGING_MAX_AGE_SECONDS (abandoned uploads)."""
    now = time.time()
    for meta_path in _staging_dir().glob("*.meta"):
        try:
            if now - meta_path.stat().st_mtime > STAGING_MAX_AGE_SECONDS:
                meta_path.with_suffix(".part").unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        except OSError:
            continue


def _validate_upload_id(upload_id: str) -> None:
    if not _UPLOAD_ID_RE.match(upload_id or ""):
        raise HTTPException(status_code=400, detail="Invalid upload id")


@router.post("/upload/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Form(...),
    index: int = Form(...),
    total: int = Form(...),
    chunk_size: int = Form(...),
    total_size: int = Form(...),
    filename: str = Form(""),
    mime: str = Form(""),
    title: str = Form(""),
    description: str = Form(""),
    visibility: str = Form("site"),
    csrf: str = Form(...),
    chunk: UploadFile = File(...),
    user=Depends(require_user),
):
    if not verify_csrf(csrf, request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _validate_upload_id(upload_id)
    if index < 0 or total <= 0 or index >= total:
        raise HTTPException(status_code=400, detail="Invalid chunk index")
    if visibility not in ("site", "private"):
        visibility = "site"

    max_bytes = Config.MAX_UPLOAD_MB * 1024 * 1024
    if total_size > max_bytes:
        raise HTTPException(status_code=413, detail="Upload too large")

    # Opportunistic GC of abandoned staging files.
    await run_in_threadpool(_cleanup_staging)

    part_path, meta_path = _staging_paths(upload_id)
    meta = await run_in_threadpool(_read_meta, meta_path)
    if not meta:
        meta = {
            "total": total,
            "chunk_size": chunk_size,
            "total_size": total_size,
            "received": [],
            "title": title.strip(),
            "description": description.strip(),
            "visibility": visibility,
            "filename": filename,
            "mime": mime,
            "owner_id": user["id"],
        }
    else:
        meta["total"] = total
        meta["chunk_size"] = chunk_size
        meta["total_size"] = total_size
        meta.setdefault("received", [])
        if title.strip():
            meta["title"] = title.strip()
        if description.strip():
            meta["description"] = description.strip()
        meta["visibility"] = visibility
        if filename:
            meta["filename"] = filename
        if mime:
            meta["mime"] = mime
        meta["owner_id"] = user["id"]

    chunk_bytes = await chunk.read()
    offset = index * int(meta["chunk_size"])
    if offset + len(chunk_bytes) > int(meta["total_size"]):
        raise HTTPException(status_code=400, detail="Chunk exceeds declared file size")
    await run_in_threadpool(_write_chunk_at, part_path, offset, chunk_bytes)

    if index not in meta["received"]:
        meta["received"].append(index)
    await run_in_threadpool(_write_meta, meta_path, meta)

    return JSONResponse({"ok": True, "index": index, "received": len(meta["received"])})


@router.post("/upload/complete")
async def upload_complete(
    request: Request,
    upload_id: str = Form(...),
    csrf: str = Form(...),
    user=Depends(require_user),
    db=Depends(get_db),
):
    if not verify_csrf(csrf, request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    _validate_upload_id(upload_id)

    part_path, meta_path = _staging_paths(upload_id)
    meta = await run_in_threadpool(_read_meta, meta_path)
    if not meta:
        raise HTTPException(status_code=400, detail="Upload session not found")

    received = set(meta.get("received", []))
    if received != set(range(int(meta["total"]))):
        raise HTTPException(status_code=400, detail="Upload incomplete")
    if not part_path.exists():
        raise HTTPException(status_code=400, detail="Upload data missing")

    # Magic-byte sniff on the assembled file.
    with open(part_path, "rb") as f:
        head = f.read(8192)
    kind = filetype.guess(head)
    if not kind or kind.mime not in ALLOWED_TYPES:
        await run_in_threadpool(_delete_staging, part_path, meta_path)
        raise HTTPException(status_code=400, detail="Unsupported video format")

    size = part_path.stat().st_size
    max_bytes = Config.MAX_UPLOAD_MB * 1024 * 1024
    if size > max_bytes:
        await run_in_threadpool(_delete_staging, part_path, meta_path)
        raise HTTPException(status_code=413, detail="Upload too large")

    height = await _probe_height(part_path)
    if height is not None and height > 1080:
        await run_in_threadpool(_delete_staging, part_path, meta_path)
        raise HTTPException(
            status_code=400,
            detail="Video resolution exceeds 1080p. Please upload a 1080p or smaller file.",
        )

    ext = ALLOWED_TYPES[kind.mime]
    video_uuid = new_video_uuid()
    raw_path = _raw_dir() / f"{video_uuid}{ext}"
    await run_in_threadpool(shutil.move, str(part_path), str(raw_path))
    await run_in_threadpool(_delete_staging, part_path, meta_path)

    cursor = await db.execute(
        """
        INSERT INTO videos (uuid, owner_id, title, description, visibility, status, raw_path, raw_size_bytes)
        VALUES (?, ?, ?, ?, ?, 'uploading', ?, ?)
        """,
        (
            video_uuid,
            user["id"],
            (meta.get("title") or "").strip(),
            (meta.get("description") or "").strip(),
            meta.get("visibility", "site"),
            str(raw_path),
            size,
        ),
    )
    await db.commit()
    video_id = cursor.lastrowid

    await jobs.enqueue(db, video_id)

    return JSONResponse({"uuid": video_uuid})


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

@router.get("/stream/{video_uuid}/{filename}")
async def stream_video(
    video_uuid: str,
    filename: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    cursor = await db.execute(
        """
        SELECT v.id, v.visibility, v.owner_id, v.status FROM videos v WHERE v.uuid = ?
        """,
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video or video["status"] != "ready":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not ready")

    is_owner = user["id"] == video["owner_id"]
    is_admin = _can_moderate(user)

    if video["visibility"] == "private" and not (is_owner or is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Private video")

    cursor = await db.execute(
        "SELECT file_path FROM video_renditions WHERE video_id = ? AND status = 'ready' AND file_path LIKE ?",
        (video["id"], f"%{filename}"),
    )
    rendition = await cursor.fetchone()
    if not rendition:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rendition not found")

    file_path = Path(rendition["file_path"])
    if not file_path.exists() or not _is_within_upload_dir(file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing")

    if Config.PRODUCTION:
        # Nginx will serve from an internal alias pointing at Config.UPLOAD_DIR.
        internal_path = "/internal/" + file_path.relative_to(Config.UPLOAD_DIR).as_posix()
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": internal_path,
                "Content-Type": "video/mp4",
                "Accept-Ranges": "bytes",
            },
        )

    return FileResponse(file_path, media_type="video/mp4", filename=filename)


@router.get("/thumb/{video_uuid}")
async def thumbnail(
    video_uuid: str,
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    cursor = await db.execute(
        "SELECT id, visibility, owner_id, thumbnail_path, status FROM videos WHERE uuid = ?",
        (video_uuid,),
    )
    video = await cursor.fetchone()
    if not video or not video["thumbnail_path"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found")

    is_owner = user["id"] == video["owner_id"]
    is_admin = _can_moderate(user)

    if video["visibility"] == "private" and not (is_owner or is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Private video")

    file_path = Path(video["thumbnail_path"])
    if not file_path.exists() or not _is_within_upload_dir(file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing")

    if Config.PRODUCTION:
        internal_path = "/internal/" + file_path.relative_to(Config.UPLOAD_DIR).as_posix()
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": internal_path,
                "Content-Type": "image/jpeg",
            },
        )
    return FileResponse(file_path, media_type="image/jpeg")


def _is_within_upload_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(Config.UPLOAD_DIR.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------

async def transcode_video(video_id: int, raw_path: str, video_uuid: str):
    """Background task: probe, thumbnail, and optionally transcode.
    Uses a single DB connection so cursors remain valid while the connection is open.
    """
    import aiosqlite

    async with aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 30000")

        out_dir = _videos_dir() / video_uuid
        await run_in_threadpool(out_dir.mkdir, exist_ok=True, parents=True)

        raw = Path(raw_path)
        if not raw.exists():
            await _update_video_status_db(db, video_id, "failed")
            await db.commit()
            return

        # Tell the player page that real work has started. Without this the
        # schema's 'processing' state was never written and the viewer sat on
        # an empty player with no explanation.
        await _update_video_status_db(db, video_id, "processing")
        await db.commit()

        duration = await _probe_duration(raw)
        if duration is None:
            duration = 0

        height = await _probe_height(raw)

        # Thumbnail at the midpoint.
        thumb_path = out_dir / "poster.jpg"
        thumb_ok = await _extract_thumbnail(raw, thumb_path, duration / 2 if duration > 0 else 1)
        if thumb_ok:
            await _set_thumbnail_db(db, video_id, str(thumb_path))
            await db.commit()

        if not Config.TRANSCODE_VIDEOS:
            # Serve the original file as-is. Create a single ready rendition row.
            label = f"{height}p" if height else "original"
            await db.execute(
                """
                INSERT INTO video_renditions (video_id, label, height, file_path, status, size_bytes)
                VALUES (?, ?, ?, ?, 'ready', ?)
                """,
                (video_id, label, height, str(raw), raw.stat().st_size),
            )
            await _update_video_status_db(db, video_id, "ready", duration)
            await db.commit()
            return

        # Create rendition rows in pending state.
        rendition_rows = []
        for spec in RENDITIONS:
            out_file = out_dir / f"{spec['label']}.mp4"
            cursor = await db.execute(
                """
                INSERT INTO video_renditions (video_id, label, file_path, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (video_id, spec["label"], str(out_file)),
            )
            rendition_rows.append((cursor.lastrowid, out_file, spec))
        await db.commit()

        # Transcode each rendition sequentially; keeps load predictable.
        for rendition_id, out_file, spec in rendition_rows:
            ok = await _ffmpeg_transcode(raw, out_file, spec)
            size = out_file.stat().st_size if out_file.exists() else 0
            await db.execute(
                "UPDATE video_renditions SET status = ?, size_bytes = ? WHERE id = ?",
                ("ready" if ok else "failed", size, rendition_id),
            )
            await db.commit()

        # Mark video ready if at least one rendition succeeded.
        cursor = await db.execute(
            "SELECT COUNT(*) AS c FROM video_renditions WHERE video_id = ? AND status = 'ready'",
            (video_id,),
        )
        row = await cursor.fetchone()
        succeeded = row["c"] > 0
        await _update_video_status_db(
            db, video_id, "ready" if succeeded else "failed", duration
        )
        await db.commit()

        if succeeded:
            await _purge_raw_original(db, video_id, raw)


async def _purge_raw_original(db, video_id: int, raw: Path) -> None:
    """Delete the original upload once renditions exist.

    The raw file is roughly the size of all renditions combined and is never
    served, so keeping it doubles storage per video for no benefit. Set
    KEEP_RAW_UPLOADS=true to retain it.
    """
    if Config.KEEP_RAW_UPLOADS:
        return
    try:
        if raw.exists() and _is_within_upload_dir(raw):
            await run_in_threadpool(raw.unlink, True)
            await db.execute("UPDATE videos SET raw_path = NULL WHERE id = ?", (video_id,))
            await db.commit()
    except OSError as exc:
        logger.warning("Could not purge raw upload %s: %s", raw, exc)


async def _probe_duration(path: Path) -> Optional[int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffprobe duration failed for %s (rc=%s): %s", path, proc.returncode, stderr.decode().strip())
        return int(float(stdout.decode().strip()))
    except Exception as exc:
        logger.warning("ffprobe duration raised for %s: %s", path, exc)
        return None


async def _probe_height(path: Path) -> Optional[int]:
    """Return the video stream height in pixels, or None if ffprobe fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffprobe height failed for %s (rc=%s): %s", path, proc.returncode, stderr.decode().strip())
        return int(stdout.decode().strip())
    except Exception as exc:
        logger.warning("ffprobe height raised for %s: %s", path, exc)
        return None


async def _extract_thumbnail(src: Path, dest: Path, at_seconds: float) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss", str(at_seconds),
            "-i", str(src),
            "-frames:v", "1",
            "-q:v", "2",
            str(dest),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg thumbnail failed for %s (rc=%s): %s", src, proc.returncode, stderr.decode().strip())
        return proc.returncode == 0 and dest.exists()
    except Exception as exc:
        logger.warning("ffmpeg thumbnail raised for %s: %s", src, exc)
        return False


async def _ffmpeg_transcode(src: Path, dest: Path, spec: dict) -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-vf", f"scale=-2:{spec['height']},format=yuv420p",
        "-c:v", "libopenh264",
        "-b:v", spec["video_bitrate"],
        "-c:a", "aac",
        "-b:a", spec["audio_bitrate"],
        "-movflags", "+faststart",
        str(dest),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg transcode failed for %s -> %s (rc=%s): %s", src, dest, proc.returncode, stderr.decode().strip())
        return proc.returncode == 0 and dest.exists()
    except Exception as exc:
        logger.warning("ffmpeg transcode raised for %s -> %s: %s", src, dest, exc)
        return False


async def _update_video_status_db(db, video_id: int, status: str, duration: Optional[int] = None):
    if duration is not None:
        await db.execute(
            "UPDATE videos SET status = ?, duration_seconds = ? WHERE id = ?",
            (status, duration, video_id),
        )
    else:
        await db.execute(
            "UPDATE videos SET status = ? WHERE id = ?",
            (status, video_id),
        )


async def _set_thumbnail_db(db, video_id: int, thumb_path: str):
    await db.execute(
        "UPDATE videos SET thumbnail_path = ? WHERE id = ?",
        (thumb_path, video_id),
    )
