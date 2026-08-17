# Agent Guide for Atomiser

This document helps coding assistants (Claude, Copilot, etc.) work effectively on the Atomiser codebase.

## Project overview

Atomiser is a small, invite-only community video-hosting web application. The default site title is **Atomiser Site**. It is built with Python and FastAPI, uses server-side sessions, stores data in SQLite, and transcodes uploaded videos into multiple H.264 MP4 renditions.

All video content is authenticated: only logged-in users can upload, view, or browse videos. Videos are served through FastAPI authorisation endpoints, then streamed by nginx via `X-Accel-Redirect` from an internal location so the filesystem is never exposed directly.

## Tech stack

| Layer | Choice |
|---|---|
| Web framework | FastAPI + Jinja2 |
| Server | uvicorn (development binds to `HOST:PORT`; production uses a unix socket behind nginx) |
| Reverse proxy | nginx (TLS, rate limiting, static files, X-Accel-Redirect) |
| Database | SQLite via `aiosqlite` |
| Migrations | Hand-written SQL files in `db/migrations/` |
| Password hashing | Argon2id (`argon2-cffi`) |
| 2FA / passkeys | TOTP (`pyotp`) and WebAuthn (`fido2`) |
| Sessions | Server-side SQLite sessions; `HttpOnly`, `Secure` (in production), `SameSite=Lax` cookie |
| CSRF | Double-submit cookie pattern |
| Video transcoding | `ffmpeg` subprocess invoked from async code |
| CSS/JS | Vanilla; no build step |

## Project layout

```
Atomiser/
├── app/
│   ├── main.py              # FastAPI app factory, routers, error handlers, lifespan
│   ├── config.py            # Settings loaded from .env; BASE_DIR helper
│   ├── db.py                # aiosqlite connection + migration runner
│   ├── models.py            # Pydantic forms and request models
│   ├── auth.py              # Passwords, sessions, TOTP, WebAuthn, login, password reset
│   ├── roles.py             # Role enum and role-check helpers
│   ├── users.py             # Profile and session-management routes
│   ├── videos.py            # Upload, transcode, feed, player, streaming, file cleanup
│   ├── jobs.py              # Durable transcode queue and worker pool
│   ├── mail.py              # Optional SMTP delivery (no-op when unconfigured)
│   ├── ratelimit.py         # Sliding-window auth throttling and account lockout
│   ├── admin.py             # Admin + Configurator dashboard, audit viewer
│   ├── invites.py           # Invite generation, revocation
│   ├── utils.py             # Tokens, CSRF, UUID helpers
│   ├── templates/           # Jinja2 templates
│   └── static/              # CSS, JS, images
├── db/migrations/           # Ordered .sql migration files (new tables only;
│                            # column additions live in db.py's _ADDED_COLUMNS)
├── scripts/
│   ├── bootstrap.py         # Create the first (immutable) Configurator
│   ├── migrate_bootstrap.py # Idempotent is_bootstrap column helper
│   └── ...
├── nginx/
│   ├── atomiser.conf        # Production nginx site config
│   └── atomiser.service     # systemd unit example
├── uploads/                 # raw/ and videos/ subdirs (created at runtime)
├── data/                    # SQLite database directory
├── tests/                   # pytest suite covering auth, TOTP, passkeys, roles,
│                            # invites, bootstrap, videos, jobs, settings
├── requirements.txt
├── .env.example
├── docs/
│   ├── DEPLOYMENT.md        # Full production deployment guide
│   └── PLAN.md              # Original implementation plan
├── README.md                # Quick start and feature overview
├── LICENSE                  # GNU AGPL v3.0
├── AGENTS.md                # This guide (CLAUDE.md imports it)
└── CLAUDE.md                # Agent entry point; imports AGENTS.md
```

## Roles and permissions

Roles are ordered by rank:

1. `member` — upload and view videos; edit own profile.
2. `admin` — manage users, generate invites, delete any content.
3. `configurator` — full access; can change site title and manage admin roles.

Use `require_role(user, Role.ADMIN)` or `Role.CONFIGURATOR` in routes. `require_user` from `auth.py` provides the current user dict (or raises `LoginRequiredException`, which redirects to `/auth/login`).

## Key conventions

### Database

- Use `get_db()` as a FastAPI dependency. It yields an `aiosqlite` connection with `row_factory = aiosqlite.Row`.
- Migrations are plain `.sql` files in `db/migrations/` applied in lexical order by `init_db()` on startup. Idempotent column changes that SQLite cannot express in SQL (e.g., `is_bootstrap`) are handled by a Python helper that runs after the SQL scripts.
- Always use `PRAGMA foreign_keys = ON;` and `PRAGMA journal_mode = WAL;`.
- Store token hashes with `hash_token()`; never store the raw token except for temporary workflow state.

### Routes and templates

- Each module exposes a FastAPI `APIRouter`. The routers are wired in `app/main.py`.
- Templates are initialised in `main.py` and assigned as module-level `templates` globals. Route handlers call `templates.TemplateResponse(...)`.
- A compatibility shim in `main.py` allows both the old `(name, context, status_code)` signature and the newer `(request, name, context)` signature.
- Base context helpers such as `site_title` are fetched per route (there is no global template context processor).

### Authentication and security

- Session cookie name is `session`. CSRF cookie name is `csrf`. A pending-auth cookie `pending_auth` is used during TOTP/WebAuthn flows.
- All POST/DELETE/state-changing forms must include a CSRF token and verify it against the `csrf` cookie using `verify_csrf()`.
- Cookies are `Secure` only when `Config.PRODUCTION` is true; in local development they are not marked Secure.
- `argon2-cffi` parameters are fixed in `auth.py` (`time_cost=3`, `memory_cost=65536`, etc.).
- `hash_password()` and `verify_password()` are async and run the CPU-intensive Argon2 work in a threadpool (`run_in_threadpool`) so the event loop is not blocked on login/register. File I/O in `videos.py` also uses `run_in_threadpool`.

### Video handling

- Accepted MIME types: `video/mp4`, `video/webm`, `video/quicktime`, `video/x-matroska`.
- Uploaded files are saved to `uploads/raw/<uuid>.<ext>`. Original extension is preserved only on the raw file.
- Transcoding produces 720p, 480p, and 360p H.264 MP4s with a midpoint thumbnail. Renditions are stored in `uploads/videos/<video_uuid>/`.
- `videos.py` has `RENDITIONS` and `ALLOWED_TYPES` constants for quick changes.
- Streaming is done via `/videos/<uuid>` (HTML player) and `/stream/<uuid>/<filename>` (the actual file). The stream endpoint sets `X-Accel-Redirect`; nginx handles the byte range serving.
- **Never emit several same-type `<source>` elements.** A browser plays the first source it *supports*, so three `video/mp4` sources always meant only 720p was ever served. The player ships one `src` plus a quality selector in `static/js/player.js`.
- Rendition filenames are derived server-side with `Path(...).name`, not by splitting `file_path` on `/` in a template — `file_path` is OS-native and the template form breaks on Windows.
- `purge_video_files()` in `videos.py` removes the original, every rendition, the thumbnail and the per-video directory. Call it from anything that deletes a video; deleting only the DB row leaks the media forever.
- The original upload is deleted once a rendition succeeds unless `KEEP_RAW_UPLOADS=true`, and `videos.raw_path` is set to NULL so storage totals stay accurate.

### Background jobs

- Transcoding is **not** a `BackgroundTasks` job. Uploads insert a row into `transcode_jobs` and a worker pool started in `main.py`'s lifespan picks it up.
- `jobs.requeue_orphans()` runs at startup: it resets jobs left `running` by a crash and queues any video stuck in a pending status with no job row.
- A job is claimed with a conditional `UPDATE ... WHERE status = 'queued'`; the `rowcount == 1` check is the concurrency gate. Do not replace it with a plain SELECT-then-UPDATE.
- Failures retry up to `TRANSCODE_MAX_ATTEMPTS`, then mark the video `failed`.
- Video status flows `uploading` → `processing` → `ready`/`failed`. The player polls `/videos/<uuid>/status` while it is not ready.

### Email and rate limiting

- `mail.py` is optional by design. `Config.mail_enabled()` is false unless both `SMTP_HOST` and `SMTP_FROM` are set, and every feature that uses mail must still work without it. `send_mail()` returns False on failure rather than raising.
- Use `mail.absolute_url(request, path)` for links that leave the app; it prefers `SITE_URL` over the request's `Host` header.
- `ratelimit.py` counts failures against the **submitted** email string, not a resolved user id, so an unknown address is throttled exactly like a real one. Keep it that way — the difference would be an account-enumeration oracle.
- Throttle checks belong *before* the Argon2 verification, which is deliberately expensive.

## Environment and running locally

1. Install Python 3.12+ and ffmpeg.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and set `SECRET_KEY`.
4. Bootstrap the first Configurator:
   ```bash
   python scripts/bootstrap.py --email you@example.com --name "Your Name"
   ```
5. Run:
   ```bash
   uvicorn app.main:app --reload
   ```

The production setup is documented in `README.md` and uses the nginx/systemd files in `nginx/`.

## Common gotchas

- **Git repository.** Tracked with git and hosted publicly at
  <https://github.com/OMG-Software/Atomiser> (`origin`, default branch `main`).
- **Never commit secrets or runtime data.** `.gitignore` excludes `.env`, `data/`
  (real user accounts, Argon2 hashes, TOTP secrets), `uploads/`, `venv/`, deploy
  zips and debug cookie files. The repo is public — check `git status` before
  committing and never use `git add -f` on those paths.
- **Tests exist.** Run the suite with `venv\Scripts\python -m pytest tests\` (Windows) or `venv/bin/python -m pytest tests/`. Add tests for new features in the existing module files.
- **Template signature shim.** `TemplateResponse` accepts either signature; prefer the newer `(request, name, context)` form for new code, but existing routes use the old form.
- **Module-level `templates` variable.** It is `None` until `main.py` assigns `auth.templates = app_templates`, etc. Do not try to render templates at import time.
- **Database schema changes.** Add a new `.sql` file in `db/migrations/` rather than editing existing migration files; `init_db()` runs all scripts on every startup. Some migrations (such as adding `is_bootstrap`) are also handled by Python helpers in `app/db.py` because SQLite cannot express conditional `ALTER TABLE`.
- **CSRF on every state-changing form.** Missing CSRF checks are a security bug, not a style issue. Read the token from the form body (`form.get("csrf", "")`) rather than declaring `csrf: str = Form(...)`, so a missing token returns 403 instead of a 422 validation error.
- **No inline event handlers.** The CSP is `script-src 'self'` with no `'unsafe-inline'`, so `onsubmit="return confirm(...)"` silently does nothing in production — the form just submits. Use `data-confirm="…"` on the form; `static/js/confirm.js` handles it globally.
- **Adding a column?** SQLite has no `ADD COLUMN IF NOT EXISTS`. Add it to `_ADDED_COLUMNS` in `app/db.py` rather than to a `.sql` file, which would fail on the second startup. New *tables* still go in a migration with `CREATE TABLE IF NOT EXISTS`.
- **File paths.** Always use `Config.UPLOAD_DIR`, `Config.DATABASE_PATH`, or `BASE_DIR` from `config.py`; never hard-code filesystem paths.
- **Role comparisons.** Always compare roles via `ROLE_RANK` / `has_role()`; do not compare role strings directly.

## How to extend

- New route modules: create a new file in `app/`, add a router, and include it in `app/main.py`. Reuse `require_user`, `require_role`, and `get_db`.
- New forms: add a Pydantic model in `app/models.py`.
- New DB tables: add a migration script in `db/migrations/`. Keep scripts idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).
- New pages: add Jinja2 templates under `app/templates/` and extend `base.html`.
- New static assets: place in `app/static/` and reference with `/static/...`.

## Deployment notes

Production uses nginx as a reverse proxy to a uvicorn unix socket. Static files are served directly by nginx. Video and thumbnail files are served through the internal `/internal/` location after FastAPI authorisation returns `X-Accel-Redirect`. Do not expose `/internal/` directly to the internet.
