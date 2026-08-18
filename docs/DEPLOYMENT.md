# Atomiser Deployment Guide — Fedora 44

This guide covers a production deployment of Atomiser on **Fedora 44** using **nginx** as the reverse proxy and **systemd** to run the uvicorn service.

The example domain used below is `example.com`. Replace it with your own domain wherever it appears.

---

## What you need

- A Fedora 44 server (Server or Cloud edition) with root or sudo access.
- Python 3.13 or newer (Fedora 44 ships with this).
- `ffmpeg` and `ffprobe` installed.
- `nginx`.
- `certbot` and the nginx plugin.
- The `sqlite3` command-line tool, used by the backup, upgrade and
  troubleshooting steps below. The Python bindings are built in, but the CLI is
  a separate package and a minimal server may not have it.
- A domain name pointed at your server.

Install the base dependencies with `dnf`:

```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip python3-venv ffmpeg nginx sqlite
sudo dnf install -y certbot python3-certbot-nginx
```

If you plan to use `firewalld`, keep it enabled; the firewall section below opens ports 80 and 443.

---

## 1. Create the application user

Atomiser should run as an unprivileged user.

```bash
sudo useradd --system --no-create-home --home-dir /opt/atomiser atomiser
```

---

## 2. Deploy the code

Place the code under `/opt/atomiser` and give ownership to the `atomiser` user.

```bash
sudo mkdir -p /opt/atomiser
sudo chown atomiser:atomiser /opt/atomiser
```

### Build a deploy zip on your development machine

Atomiser ships with a build script that produces a clean production archive containing only what is needed to run the app. From the project root on your **development** machine:

```bash
python scripts/make_deploy_zip.py            # writes atomiser-deploy-<timestamp>.zip
# Preview what will be included without writing anything:
python scripts/make_deploy_zip.py --list
```

The zip includes `app/`, `db/migrations/`, `scripts/`, `nginx/`, and `requirements.txt` only. It deliberately **excludes** secrets and runtime data so unzipping over an install never clobbers them:

- `.env` (secrets live in `/etc/atomiser/atomiser.env`, never in the code tree)
- `data/` (the live SQLite database) and `uploads/` (user video content)
- `tests/`, `venv/`, `__pycache__`/`*.pyc`, `.pytest_cache`
- documentation (`README.md`, `DEPLOYMENT.md`, `PLAN.md`, `AGENTS.md`, etc.)
- debug artifacts (`cookies.txt`, `test_video.mp4`)

### Transfer and extract on the server

Copy the zip to the server and extract it as the `atomiser` user:

```bash
scp atomiser-deploy-*.zip root@server:/tmp/
```

On the server:

```bash
sudo -u atomiser unzip -o /tmp/atomiser-deploy-*.zip -d /opt/atomiser
sudo -u atomiser python3 -m venv /opt/atomiser/venv   # first deploy only
sudo -u atomiser /opt/atomiser/venv/bin/pip install --upgrade pip
sudo -u atomiser /opt/atomiser/venv/bin/pip install -r /opt/atomiser/requirements.txt
```

> Unlike `rsync --delete`, `unzip -o` only overwrites the paths present in the archive. It will **not** delete `app/__init__.py`, and it will never touch `/opt/atomiser/data`, `/opt/atomiser/uploads`, or `/etc/atomiser/atomiser.env`.

Create the runtime directories:

```bash
sudo mkdir -p /opt/atomiser/data /opt/atomiser/uploads/raw /opt/atomiser/uploads/videos /run/atomiser
sudo chown -R atomiser:atomiser /opt/atomiser /run/atomiser
```

---

## 3. Configure the environment

Create the production environment file. Do **not** put it inside `/opt/atomiser` if that tree is owned by a non-privileged deployment account; `/etc/atomiser/atomiser.env` keeps secrets separate from the code.

```bash
sudo mkdir -p /etc/atomiser
sudo cp /opt/atomiser/.env.example /etc/atomiser/atomiser.env
sudo chmod 600 /etc/atomiser/atomiser.env
sudo nano /etc/atomiser/atomiser.env
```

Minimum required values:

```ini
# Generate a strong secret before editing:
# python3 -c "import secrets; print(secrets.token_urlsafe(48))"
SECRET_KEY=<long-random-secret>

ENV=production
DATABASE_PATH=/opt/atomiser/data/atomiser.db
UPLOAD_DIR=/opt/atomiser/uploads
MAX_UPLOAD_MB=500

# Must match your public domain, or passkeys will not work.
WEBAUTHN_RP_ID=example.com

# Required as soon as SMTP is enabled: every emailed link is built from this.
SITE_URL=https://example.com

# Not used in production because nginx talks to the unix socket, but keep sensible defaults.
HOST=127.0.0.1
PORT=8000
```

`SECRET_KEY`, `WEBAUTHN_RP_ID`, and the database/upload paths are the most important changes from the development defaults.

### Transcoding

Transcoding runs as a durable job queue rather than an in-process background
task, so a restart mid-transcode resumes on the next start instead of stranding
the video. Relevant settings:

```ini
# Videos transcoded at once. 1 keeps ffmpeg from starving the web worker.
TRANSCODE_CONCURRENCY=1

# Retries before a video is marked failed for good.
TRANSCODE_MAX_ATTEMPTS=3

# Delete the original upload once a rendition succeeds. The original is roughly
# as large as all renditions combined and is never served, so keeping it about
# doubles storage per video.
KEEP_RAW_UPLOADS=false
```

On a busy site you can move transcoding off the web service entirely: set
`RUN_TRANSCODE_WORKER=false` in the web service's environment and run a second
unit with it set to `true` that imports `app.jobs` and calls `start_workers()`.
Both processes share the same SQLite database, and the conditional-UPDATE job
claim means two workers never take the same job.

Overlapping processes are safe. A claimed job carries a lease
(`TRANSCODE_LEASE_SECONDS`, default 120) that its worker renews every third of
that period while ffmpeg runs, and startup recovery reclaims only jobs whose
lease has lapsed. So a rolling restart, or a worker unit running alongside the
web app, cannot hand the same video to two ffmpeg processes. Raise the lease if
a worker can be paused long enough — heavy swapping, a suspended VM — to miss
several renewals; the only cost of a longer lease is a slower pickup after a
genuine crash. The email queue uses the same scheme via `EMAIL_LEASE_SECONDS`.

> **Note:** with `KEEP_RAW_UPLOADS=false` a failed transcode can only be retried
> while the original is still on disk — which it is, because the original is
> deleted only after a rendition succeeds. Once a video is `ready` there is
> nothing to reprocess, and the admin retry button reports that.

### Rate limiting

The nginx `limit_req` zones remain the first line of defence, but the app now
also throttles the auth endpoints itself, so protection survives a proxy
misconfiguration or a move to a different front end:

```ini
RATE_LIMIT_ENABLED=true
RATE_LIMIT_WINDOW_SECONDS=900
LOGIN_MAX_FAILURES_PER_EMAIL=8
LOGIN_MAX_FAILURES_PER_IP=25
LOCKOUT_MINUTES=15
```

The IP threshold is deliberately much higher than the email one, because a
household or office behind one NAT address shares it.

> **Important:** the IP-keyed limit is only meaningful if the app sees the real
> client address. nginx must forward `X-Forwarded-For`/`X-Real-IP` (the shipped
> config does), otherwise every request looks like it comes from one address.

### Email (optional)

Leave `SMTP_HOST` blank to run with no mail at all — invites are copy-paste
links and passwords are reset by an admin, exactly as before. Setting both
`SMTP_HOST` and `SMTP_FROM` additionally enables emailed invites and
self-service password reset:

```ini
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=atomiser@example.com
SMTP_PASSWORD=<app-password>
SMTP_FROM=Atomiser <atomiser@example.com>
SMTP_STARTTLS=true
SMTP_SSL=false
PASSWORD_RESET_TTL_MINUTES=60
```

Use port 465 with `SMTP_SSL=true` and `SMTP_STARTTLS=false` for implicit TLS.

> **`SITE_URL` is required once SMTP is on.** Every link that goes into an email
> is built from it. The only other source of a hostname is the request's `Host`
> header, which the client controls and the shipped nginx config forwards
> verbatim — a password reset link built that way is a host-header injection:
> an attacker requests a reset for someone else with `Host: evil.test`, and the
> victim is emailed a genuine token pointing at the attacker's domain. With SMTP
> enabled and `SITE_URL` unset, password recovery is refused, invites cannot be
> emailed, notifications are skipped, and the admin dashboard shows a warning.

Send yourself a test invite from `/invites/` after enabling this; delivery
failures are logged to the journal and never lose the invite, since the link is
still shown on screen.

> **Password reset emails go through the queue worker.** `/auth/forgot` writes
> the message to `email_queue` and returns immediately rather than waiting on
> SMTP — an inline send makes the response measurably slower for a registered
> address than an unknown one, which is enough to enumerate accounts. The
> practical consequence is that `RUN_EMAIL_WORKER=false` with no separate worker
> process means reset emails are written but never delivered. Leave the worker
> enabled somewhere. Transactional mail is sent ahead of bulk notifications, so
> a large fan-out cannot delay a reset link.

### New-video notifications

With SMTP configured, members are emailed whenever a video finishes processing
and is visible to the site. They are subscribed by default and can opt out from
their profile or from the unsubscribe link in any notification.

```ini
NOTIFY_NEW_VIDEOS=true
EMAIL_BATCH_SIZE=20
EMAIL_POLL_SECONDS=10
EMAIL_MAX_ATTEMPTS=3
EMAIL_RETRY_MINUTES=5
EMAIL_RETENTION_DAYS=30
```

> **`SITE_URL` is required for this feature, not optional.** The sending worker
> runs outside any HTTP request, so there is no `Host` header to fall back on.
> With it unset, notifications are skipped rather than sent with broken links,
> an error is logged, and the admin dashboard shows a warning.

Messages go through the `email_queue` table rather than being sent inline, so a
fan-out to a large membership never blocks a transcode, a failed send retries
with an exponential backoff, and a restart mid-send resumes on the next start.
`EMAIL_BATCH_SIZE` bounds how many messages go out per pass over one SMTP
connection — lower it if your provider rate-limits you.

The admin dashboard reports subscriber count and queued/sent/failed totals, and
lists undeliverable messages with the SMTP error. To inspect the queue directly:

```bash
sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db     "SELECT id, to_address, status, attempts, last_error FROM email_queue ORDER BY id DESC LIMIT 20;"
```

Like transcoding, the email worker can run in its own process: set
`RUN_EMAIL_WORKER=false` on the web service and `true` on a second unit. The
conditional-UPDATE claim means two workers never send the same message twice.

---

## 4. Bootstrap the first Configurator

The bootstrap script creates the first administrator. It can only be run once; if a Configurator already exists the script exits without making changes.

```bash
cd /opt/atomiser
sudo -u atomiser venv/bin/python scripts/bootstrap.py \
    --email admin@example.com \
    --name "Site Admin" \
    --password "ChangeThisPasswordImmediately123"
```

The bootstrap account has the Configurator role, is marked as the bootstrap user, and cannot be demoted or deleted through the admin UI. Log in as soon as the site is live and change the password.

---

## 5. Install the systemd service

Copy the provided unit file:

```bash
sudo cp /opt/atomiser/nginx/atomiser.service /etc/systemd/system/atomiser.service
sudo systemctl daemon-reload
sudo systemctl enable --now atomiser
```

Check that it started cleanly:

```bash
sudo systemctl status atomiser
sudo journalctl -u atomiser -n 50 --no-pager
```

The service uses `Type=notify`, so make sure the installed version of uvicorn supports systemd notification, or change `Type=notify` to `Type=simple` if you see start-timeout errors.

---

## 6. Configure nginx

Fedora uses `/etc/nginx/conf.d/` for site snippets rather than Debian's `sites-available/sites-enabled` pattern. Copy the config and enable nginx:

```bash
sudo cp /opt/atomiser/nginx/atomiser.conf /etc/nginx/conf.d/atomiser.conf
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

Review `/etc/nginx/conf.d/atomiser.conf` before enabling it. The most important details to verify are:

- `server_name` matches your domain.
- `client_max_body_size` matches or exceeds `MAX_UPLOAD_MB`. Note: if the site is behind **Cloudflare**, Cloudflare's own per-request body cap (100 MB on Free/Pro, 200 MB Business, 500 MB+ Enterprise) is enforced *before* nginx — nginx's limit only matters once traffic is past Cloudflare. Large uploads use chunked upload (see below), so each request is well under any of these limits.
- The `/upload` config has **two** locations: `location = /upload` (the page + single-shot fallback, rate-limited) and `location /upload/` (the chunked endpoints `/upload/chunk` and `/upload/complete`, *not* rate-limited because a large file is many sequential chunks). Don't collapse them back into one `location /upload` with the tight rate limit, or chunked uploads will be throttled to 10 r/m and stall.
- The `/internal/` location is marked `internal` and is **not** reachable from the internet.
- The unix socket path matches the one in the systemd unit.
- The `ssl_certificate` and `ssl_certificate_key` paths exist.

### Cloudflare and large uploads

If the domain is proxied through Cloudflare (orange-cloud DNS), Cloudflare caps each HTTP request body at **100 MB** on the Free/Pro plan — well below a typical video. Atomiser handles this with **chunked upload**: the browser splits the file into 50 MB chunks and POSTs each to `/upload/chunk`, then calls `/upload/complete` to validate and transcode (see `app/static/js/upload.js` and the `/upload/chunk` / `/upload/complete` routes in `app/videos.py`). This works through Cloudflare Free with no plan upgrade and no origin-IP exposure. The single-shot `/upload` route is kept as the no-JS / small-file fallback.

### WebAuthn / passkey origin

Passkeys validate the origin. nginx must forward the real host and protocol so the app sees `https://example.com`:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_pass_request_headers on;
```

The provided config already does this.

---

## 7. Obtain TLS certificates

Use certbot with the nginx plugin. The plugin is already installed via `python3-certbot-nginx`.

```bash
sudo certbot --nginx -d example.com
```

Follow the prompts. Certbot will modify `/etc/nginx/conf.d/atomiser.conf` automatically and reload nginx.

---

## 8. Open the firewall

If `firewalld` is running, allow HTTP and HTTPS traffic:

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

Verify with:

```bash
sudo firewall-cmd --list-services
```

---

## 9. SELinux

Fedora 44 defaults to enforcing SELinux. nginx must be allowed to proxy to a local unix socket and read the static/video directories. The simplest approach is to ensure the correct SELinux contexts are set:

```bash
# Restore default contexts on the app directories
sudo restorecon -Rv /opt/atomiser /run/atomiser

# Allow nginx to connect to the local unix socket
sudo setsebool -P httpd_can_network_connect 1
```

If you see SELinux denials in `/var/log/audit/audit.log`, use `ausearch -m avc -ts recent` to identify the missing permission and create a targeted module only if needed:

```bash
sudo ausearch -m avc -ts recent | audit2allow -a -M atomiser_local
sudo semodule -i atomiser_local.pp
```

---

## 10. Final checks

Open the site in a browser:

```
https://example.com/
```

Confirm:

- The login page loads over HTTPS.
- You can log in with the bootstrap account.
- You can create an invite and register a second account.
- Uploading a video succeeds and the feed/player work.

Test the health endpoint:

```bash
curl https://example.com/healthz
```

---

## 11. Maintenance

### Update the application

Build a fresh deploy zip on your development machine and transfer it to the server:

```bash
# dev machine
python scripts/make_deploy_zip.py
scp atomiser-deploy-*.zip root@server:/tmp/
```

```bash
# server
sudo -u atomiser unzip -o /tmp/atomiser-deploy-*.zip -d /opt/atomiser
sudo -u atomiser /opt/atomiser/venv/bin/pip install -r /opt/atomiser/requirements.txt
sudo systemctl restart atomiser
```

Always back up the database before a major update (see Backups below). `init_db()` applies any new migration scripts from `db/migrations/` on the restart.

That is the routine case. Upgrading an existing site **across a feature release**
needs a few decisions first — see the next section.

### Upgrading an existing site

The schema upgrade itself is automatic and non-destructive: `init_db()` applies
every migration on startup, all of them idempotent, so a restart (or three)
changes nothing beyond adding the new tables and columns. Existing rows are
preserved exactly, including password hashes, TOTP secrets and passkey
credentials. Nobody is signed out and no invite is invalidated.

What does need attention is the handful of settings that change *behaviour*, and
one surprise involving email.

#### 1. Back up, with the service stopped

```bash
sudo systemctl stop atomiser
sudo tar czf /root/atomiser-backup-$(date +%F).tar.gz     /opt/atomiser/data /opt/atomiser/uploads /etc/atomiser/atomiser.env
```

Stopping first matters: the database runs in WAL mode, so copying it hot can
capture a torn state. This archive is the only rollback path for the media
files, so do not skip it.

#### 2. Decide three settings before the first start

Edit `/etc/atomiser/atomiser.env` while the service is still stopped.

| Setting | Why it matters when upgrading |
|---|---|
| `KEEP_RAW_UPLOADS` | Defaults to `false`. Originals already on disk are left alone, but every *future* transcode deletes its original once a rendition succeeds. Set `true` to keep them. Once an original is purged the admin "retry" button cannot reprocess that video — there is nothing left to encode from. |
| `SITE_URL` | **Required** as soon as `SMTP_HOST` is set. If you already run SMTP without it, password recovery is refused after the upgrade until you set it. See the Email section above for why the request's `Host` header is not an acceptable substitute. |
| `NOTIFY_NEW_VIDEOS` | Set `false` for the first boot only. See step 4. |

#### 3. Deploy and start

Follow *Update the application* above, then watch the journal as it comes up:

```bash
sudo journalctl -u atomiser -f
```

You should see the migrations apply, then `Started N transcode worker(s)` and
`Started email queue worker`.

#### 4. Drain the transcode backlog before enabling notifications

Older versions ran transcoding as an in-process background task, which was lost
whenever the service restarted. An upgraded install therefore usually has
several videos stranded in `uploading`. Startup recovery finds them and
transcodes them properly — which is the point — but a completed transcode also
**emails every member**, so without care the upgrade announces a batch of
weeks-old uploads.

With `NOTIFY_NEW_VIDEOS=false` from step 2, let the backlog finish (the admin
dashboard shows the queue draining), then mark everything as already announced:

```bash
sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db     "UPDATE videos SET notified_at = CURRENT_TIMESTAMP WHERE notified_at IS NULL;"
```

Then set `NOTIFY_NEW_VIDEOS=true` and restart. This also prevents an old
*private* video from announcing itself later if someone makes it public.

Note that every existing member is subscribed by default — notifications are
opt-out. They can turn them off from their profile or via the unsubscribe link
in any message.

#### 5. Optional: backfill storage figures

`videos.raw_size_bytes` is recorded at upload, so it is empty for videos that
predate the upgrade and the dashboard's storage total under-reports them.
Nothing is broken; the number is just low. To correct it from what is on disk:

```bash
sudo -u atomiser /opt/atomiser/venv/bin/python - <<'PY'
import os, sqlite3
db = sqlite3.connect("/opt/atomiser/data/atomiser.db")
rows = db.execute(
    "SELECT id, raw_path FROM videos WHERE raw_path IS NOT NULL AND raw_size_bytes IS NULL"
).fetchall()
updated = 0
for video_id, path in rows:
    # An original that has already been purged has no size to record; skip it.
    if os.path.exists(path):
        db.execute("UPDATE videos SET raw_size_bytes = ? WHERE id = ?",
                   (os.path.getsize(path), video_id))
        updated += 1
db.commit()
print(f"backfilled {updated} of {len(rows)} candidate video(s)")
PY
```

#### 6. Rolling back

Every added column is nullable or has a default, and the new tables are simply
ignored by older code, so the upgraded database stays readable and writable by
the previous release. Rolling back is just redeploying the old zip — no database
surgery, and no need to restore the backup unless the files themselves are
damaged.

The one caveat: anything sitting in `transcode_jobs` or `email_queue` will not
be processed while the old code is running. It resumes if you roll forward
again.

### Update the operating system

```bash
sudo dnf update -y
sudo systemctl restart atomiser
```

### Database migrations

`init_db()` in `app/db.py` runs all migration scripts from `db/migrations/` on every startup, so a normal service restart applies new schema changes. Migrations are idempotent — re-running them is safe.

Back up the database before major updates. The database runs in WAL mode, so use
SQLite's own backup command rather than `cp`, which can capture a torn state
while the service is writing:

```bash
sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db     ".backup '/opt/atomiser/data/atomiser.db.bak.$(date +%F)'"
```

A plain `cp` is only safe with the service stopped.

### Backups

Back up at least these items regularly:

- `/opt/atomiser/data/atomiser.db` — the SQLite database (users, sessions, invites, videos metadata).
- `/opt/atomiser/uploads/` — raw and transcoded video files.
- `/etc/atomiser/atomiser.env` — secrets and configuration.

A simple rsync example:

```bash
rsync -a --delete /opt/atomiser/data /opt/atomiser/uploads /etc/atomiser/atomiser.env /backups/atomiser/
```

### Log rotation

The systemd journal captures uvicorn output. Configure log retention if desired:

```bash
sudo mkdir -p /etc/systemd/journald@atomiser.conf.d
echo -e "[Journal]\nMaxFileSec=1week\nSystemMaxUse=500M" | sudo tee /etc/systemd/journald@atomiser.conf.d/override.conf
sudo systemctl restart systemd-journald
```

---

## 12. Security hardening checklist

- [ ] `ENV=production` is set (this marks cookies `Secure`).
- [ ] `SECRET_KEY` is a long, random value.
- [ ] `WEBAUTHN_RP_ID` matches the public domain.
- [ ] nginx only serves HTTPS and redirects HTTP to HTTPS.
- [ ] The `/internal/` nginx location cannot be reached directly.
- [ ] File uploads are limited in size (`client_max_body_size` / `MAX_UPLOAD_MB`).
- [ ] The `atomiser` user owns the code and data directories, but cannot log in or escalate.
- [ ] `firewalld` allows only HTTP/HTTPS (and SSH).
- [ ] SELinux allows nginx to proxy to the unix socket and read static/upload files.
- [ ] The server OS, nginx, Python packages, and ffmpeg are kept up to date with `dnf`.
- [ ] Backups are automated and tested.
- [ ] The bootstrap password was changed after first login.

---

## 13. Troubleshooting

### Service fails to start

```bash
sudo journalctl -u atomiser -n 100 --no-pager
```

Common causes:

- Missing or unreadable `/etc/atomiser/atomiser.env`.
- The `/run/atomiser` directory is missing or has wrong ownership.
- SQLite database directory does not exist.
- SELinux is blocking access; check `/var/log/audit/audit.log`.

### nginx returns 502 Bad Gateway

- Check that the unix socket exists and nginx can read it:
  ```bash
  ls -la /run/atomiser/atomiser.sock
  sudo -u nginx stat /run/atomiser/atomiser.sock
  ```
  On Fedora the nginx worker process runs as the `nginx` user, not `www-data`.
- Verify `proxy_pass` matches the socket path in the systemd unit.
- Confirm the service is running: `sudo systemctl status atomiser`.
- Check SELinux denials if the socket is not accessible.

### nginx returns 403 Forbidden for static files

Fedora's SELinux may prevent nginx from reading `/opt/atomiser/app/static/`. Fix the context:

```bash
sudo restorecon -Rv /opt/atomiser/app/static
sudo setsebool -P httpd_read_user_content 1
```

### Passkey registration fails

- Ensure the browser sees `https://example.com` and not an IP or local address.
- Confirm `WEBAUTHN_RP_ID` equals exactly the domain portion (`example.com`, not `https://...`).
- Check that `X-Forwarded-Proto` and `X-Forwarded-Host` headers are forwarded by nginx.

### Uploads fail or videos do not transcode

- Verify `ffmpeg` and `ffprobe` are installed and in the service `PATH`:
  ```bash
  sudo -u atomiser ffmpeg -version
  sudo -u atomiser ffprobe -version
  ```
- Check disk space in `/opt/atomiser/uploads`.
- Review the journal for Python exceptions from `app/videos.py` or `app/jobs.py`.
- Ensure the `atomiser` user has write permission to `/opt/atomiser/uploads/raw` and `/opt/atomiser/uploads/videos`.
- Check SELinux denials if files cannot be written.
- Check the queue. **Admin → dashboard** shows how many jobs are queued and lists
  failed transcodes with the ffmpeg error and a retry button. From the shell:
  ```bash
  sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db \
      "SELECT id, video_id, status, attempts, last_error FROM transcode_jobs ORDER BY id DESC LIMIT 20;"
  ```
- Confirm the worker started. The journal logs `Started N transcode worker(s)` at
  boot; if it is missing, check `RUN_TRANSCODE_WORKER` is not set to false.

### A video is stuck on "Processing"

Jobs survive a restart, so this is almost always a real ffmpeg failure rather
than a lost task. Check the failed-transcode list on the admin dashboard for the
error, then retry it from there. A job left `running` by a hard kill is requeued
automatically the next time the service starts.

### Password reset emails are not arriving

Resets are queued and delivered by the email worker, not sent during the
request, so check the queue rather than assuming SMTP is broken:

```bash
sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db \
    "SELECT id, to_address, status, attempts, last_error FROM email_queue WHERE kind = 'password_reset' ORDER BY id DESC LIMIT 10;"
```

- Rows stuck at `queued` with `attempts = 0` mean no worker is running. Check
  `RUN_EMAIL_WORKER` and look for `Started email queue worker` in the journal.
- Rows at `queued` with rising `attempts` mean the SMTP server is rejecting
  them; `last_error` carries the reason.
- No row at all means the request was refused before a token was minted — most
  often `SITE_URL` unset, which is logged as an error and shown on the admin
  dashboard.

### Notification emails are not arriving

- Check the admin dashboard first: it shows whether SMTP is detected, how many
  messages are queued, and any undeliverable ones with the SMTP error.
- A **`SITE_URL` is not set** banner there means notifications are being skipped
  entirely. Set it in the environment file and restart.
- Confirm the recipient is actually subscribed. An unsubscribe is per-user:
  ```bash
  sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db       "SELECT email, notify_new_videos FROM users;"
  ```
- Messages stuck at `queued` with rising `attempts` mean the SMTP server is
  rejecting them; `last_error` carries the reason. Messages that never leave
  `queued` with `attempts = 0` mean the worker is not running — check the
  journal for `Started email queue worker` and `RUN_EMAIL_WORKER`.
- Remember the uploader is deliberately never notified about their own video.

### A user is locked out

Repeated failed sign-ins lock an account for `LOCKOUT_MINUTES`. The lock expires
on its own; to clear it immediately, have the user complete a password reset
(which clears it), or clear it directly:

```bash
sudo -u atomiser sqlite3 /opt/atomiser/data/atomiser.db \
    "UPDATE users SET locked_until = NULL WHERE email = 'someone@example.com';"
```

**Admin → audit log**, filtered to `login_failed` and `login_throttled`, shows
what triggered it and from which addresses.

### `ModuleNotFoundError: No module named 'app'` on startup

The `app` package needs its `__init__.py` marker file to be importable. If you deploy with `rsync --delete` from a source tree that lacks `app/__init__.py`, the marker is deleted on the server and uvicorn cannot import the app. Check and restore it:

```bash
ls -l /opt/atomiser/app/__init__.py   # should exist (0 bytes is fine)
sudo -u atomiser touch /opt/atomiser/app/__init__.py
sudo systemctl restart atomiser
```

Deploying via the zip (`scripts/make_deploy_zip.py`) avoids this entirely — the archive always includes `app/__init__.py`.

### Theme or static assets don't change after a deploy

nginx serves `/static/` with `Cache-Control: public, immutable`, so the browser never re-requests an asset at the same URL. The app busts this cache automatically: every static link carries a `?v=<mtime>` version string (see `asset_version()` in `app/main.py`), which changes whenever the file on disk changes. If a new CSS/JS file does not appear:

- Confirm the **new** file is actually on disk at the path nginx serves:
  ```bash
  ls -l /opt/atomiser/app/static/css/atomiser.css
  grep -c "brand-mark" /opt/atomiser/app/static/css/atomiser.css
  ```
- The live app runs from `/opt/atomiser/`; make sure you deployed into that tree and not a separate checkout/repo directory.
- Hard-reload once (`Ctrl+Shift+R`) to bypass a stale browser cache.

### Large upload fails with `net::ERR_CONNECTION_RESET`

If a multi-hundred-MB upload resets partway through, the most common cause is a body-size limit being enforced **while the body is still streaming** — the server closes the connection before the browser finishes sending, so it reports a reset instead of a readable error. Check, in order:

1. **Cloudflare** (if the domain is orange-clouded): Cloudflare caps a single request at 100 MB on Free/Pro. Large uploads use chunked upload (50 MB chunks), so this is handled — *but only if the updated `upload.js` is deployed*. If the browser is still doing a single POST, the old JS is cached; redeploy `upload.js` and hard-reload.
2. **nginx `client_max_body_size`**: a leftover `client_max_body_size` inside `location /upload` overrides the server-level value (location beats server). Check `nginx -T | grep -B1 client_max_body_size` and remove any duplicate so the chunk endpoints inherit the server-level limit.
3. **The chunk endpoint rate limit**: if `location /upload/` was collapsed back into `location /upload` with `limit_req zone=upload`, chunks get throttled to 10 r/m and stall. Keep the two locations separate.
4. **Worker crash / OOM**: `journalctl -u atomiser` — if the worker died mid-upload (`upstream prematurely closed connection` in the nginx error log), the box may be out of memory while buffering/transcoding, especially if other services (e.g. Synapse) share the host.

The nginx error log pinpoints which: `sudo tail -n 40 /var/log/nginx/error.log`.

---

## 14. Optional: running behind another reverse proxy

If you run nginx behind a CDN or load balancer, pass the original client IP and protocol through so the app can log accurate IPs and build correct URLs:

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Real-IP $remote_addr;
```

Uvicorn is already started with `--proxy-headers` in the provided systemd unit.

---

For day-to-day operation and feature development, see [`README.md`](../README.md).
