# Atomiser Implementation Plan

## Project goal
Build a small, invite-only community video-hosting webapp in Python.
Page title defaults to **"Atomiser Site"** and is editable in an admin UI.
All video content is protected: only authenticated users can upload or view anything.

## Tech stack
| Layer | Choice | Reason |
|---|---|---|
| Web framework | FastAPI + Jinja2 | async file I/O, streaming, built-in data validation |
| WSGI/ASGI server | uvicorn (unix socket) | simple, works behind nginx |
| Reverse proxy | nginx | handles TLS, rate limiting, static files, X-Accel-Redirect |
| Database | SQLite (via aiosqlite or SQLAlchemy async) | zero external service, single-file backup |
| Migrations | hand-written SQL scripts in `db/migrations/` | small app, no heavy ORM migration tooling needed |
| Password hashing | argon2 (passlib / argon2-cffi) | modern, slow, memory-hard |
| 2FA | TOTP (pyotp) | standard, QR-code enrolment |
| Passkeys | WebAuthn (python-fido2) | modern passwordless |
| Sessions | server-side sessions stored in DB, secure `HttpOnly` cookie | easy to revoke, no JWT leaks |
| Video transcoding | ffmpeg subprocess | produce 720p, 480p, 360p H.264 MP4 + thumbnail |
| Templates | Jinja2 with autoescape | XSS-safe HTML |
| CSS/JS | minimal vanilla, no build step | keeps deployment simple |

## Roles
1. **Configurator** — full access: site config, invites, users, content, raw storage.
2. **Admin** — manage users and content, generate invites, cannot change global site settings.
3. **Member** — upload and view videos, edit own profile.

Only Configurator can promote/demote admins or change roles.

## Project layout
```
Atomiser/
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # settings / env loader
│   ├── db.py                # connection, schema init
│   ├── models.py            # SQLAlchemy or dataclass schemas
│   ├── auth.py              # passwords, sessions, TOTP, WebAuthn
│   ├── roles.py             # role checks / dependencies
│   ├── users.py             # user/profile routes
│   ├── videos.py            # upload, transcode, feed, player
│   ├── admin.py             # admin + configurator routes
│   ├── invites.py           # invite generation & registration
│   ├── templates/           # Jinja2 templates
│   └── static/              # CSS, small JS, placeholder images
├── db/migrations/           # .sql migration files
├── scripts/
│   ├── bootstrap.py         # create configurator on first run
│   └── transcode_worker.py  # optional background transcoder
├── uploads/
│   ├── raw/                 # original uploads (not directly served)
│   └── videos/              # transcoded renditions + thumbnails
├── nginx/
│   └── atomiser.conf        # production nginx site config
├── requirements.txt
├── .env.example
└── README.md
```

## Database schema (SQLite)
- `config` — single-row table for `site_title`, etc.
- `users` — id, email (unique), password_hash, role, totp_secret, totp_enabled, created_at.
- `sessions` — session token hash, user_id, expires_at, ip, user_agent.
- `webauthn_credentials` — credential_id, public_key, sign_count, transports, user_id.
- `invites` — token_hash, created_by, used_by, max_uses (1 for one-time), used_count, expires_at.
- `videos` — id/uuid, owner_id, title, description, visibility (`site`/`private`), created_at, status (`uploading`/`processing`/`ready`/`failed`), original_path, thumbnail_path.
- `video_renditions` — video_id, label, width, height, file_path, size_bytes, status.
- `audit_log` — optional but recommended: who uploaded/viewed what.

## Authentication flow
1. **Invite-only registration**: user visits `/register?token=...`. Server checks token hash is unused and not expired. Token consumed only after successful registration.
2. **Login**: email + password. If TOTP enabled, redirect to TOTP challenge. If passkeys registered, offer WebAuthn sign-in as alternative.
3. **Sessions**: server-side session, rotated on login, bound to IP fingerprint loosely, expires after inactivity.
4. **Logout**: delete session from DB and client cookie.

## Security decisions
- All routes under `/video/*` and `/uploads/*` require authentication.
- Videos served via FastAPI auth check that sets `X-Accel-Redirect` for nginx to stream the file from an **internal** nginx location (so direct URL access to the filesystem is blocked).
- Uploaded files are validated by MIME magic + ffmpeg probe; stored outside web root; filenames are random UUIDs with original extension stripped.
- Strict CSP, HSTS, X-Frame-Options, etc. set by nginx.
- Rate limiting on login/register endpoints via nginx `limit_req`.
- Argon2id for passwords.
- All forms include CSRF tokens; FastAPI middleware verifies `SameSite=Lax` cookies + token.
- Thumbnail extraction; no EXIF leaks (sanitise if image metadata present).

## Video handling
1. Upload accepts MP4/WebM/MOV up to a configurable size.
2. Raw file saved to `uploads/raw/<uuid>.ext`.
3. Asynchronous transcoding (same process async task queue) to 720p, 480p, 360p.
4. Poster thumbnail extracted at midpoint.
5. Feed/player serves adaptive quality selector using `<video>` with multiple `<source>` tags.
6. Range requests supported by nginx via internal redirect.

## UI/UX
- Default landing page is the activity feed of latest site-visible videos.
- User profile page (`/u/<id>`) lists that user’s videos.
- Navbar shows upload button, admin link (if role >= Admin), and profile.
- Admin panel tabs: Users, Invites, Videos, Site Settings (Configurator only).
- Site title editable in Settings.

## Deployment (VPS / bare-metal)
- Python 3.12, venv.
- uvicorn bound to UNIX socket `/run/atomiser/atomiser.sock`.
- nginx reverse proxy to that socket, terminate TLS with Let’s Encrypt (certbot).
- systemd service for the app and another for transcoding worker (or one service with background tasks).
- Environment variables in `/etc/atomiser.env`: `SECRET_KEY`, `DATABASE_PATH`, `UPLOAD_DIR`, `MAX_UPLOAD_MB`, `ENV=production`.
- Dedicated unprivileged user `atomiser` owning uploads and DB.

## First-run bootstrap
`python scripts/bootstrap.py --email admin@example.com --name "First Configurator"` creates the first Configurator and prints a one-time password. It does **not** use an invite.

## Verification plan
- Unit tests for auth (argon2, TOTP, role checks).
- Manual smoke tests: upload → transcode → view in feed → view profile → admin user list.
- Security smoke tests: unauthenticated `/video/*` returns 302/403; direct nginx static path returns 404; expired invite rejected; role escalation blocked for Admin.

## Open decisions for approval
1. **Visibility granularity**: I propose two modes — `site` (visible to any logged-in user in feed/profile) and `private` (visible only to owner/admins). Default `site`. Is that sufficient?
2. **Email**: You said in-app. I’ll skip SMTP entirely; password reset will require a Configurator/Admin to generate a manual reset link or use passkey/TOTP recovery via another admin. Acceptable?
3. **Chunked uploads**: For simplicity I’ll use single POST upload. If you expect >2 GB files, say so and I’ll add resumable chunking.

If you approve the above, I’ll implement in the following order:
1. Project skeleton, config, DB schema, bootstrap.
2. Auth (registration via invite, login, TOTP, WebAuthn passkeys, sessions).
3. Roles & admin UI.
4. Video upload, transcoding, internal streaming.
5. Feed and profile pages.
6. nginx config, systemd unit, README.
