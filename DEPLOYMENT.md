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
- A domain name pointed at your server.

Install the base dependencies with `dnf`:

```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip python3-venv ffmpeg nginx
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

# Not used in production because nginx talks to the unix socket, but keep sensible defaults.
HOST=127.0.0.1
PORT=8000
```

`SECRET_KEY`, `WEBAUTHN_RP_ID`, and the database/upload paths are the most important changes from the development defaults.

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

### Update the operating system

```bash
sudo dnf update -y
sudo systemctl restart atomiser
```

### Database migrations

`init_db()` in `app/db.py` runs all migration scripts from `db/migrations/` on every startup, so a normal service restart applies new schema changes. Back up the database before major updates:

```bash
sudo -u atomiser cp /opt/atomiser/data/atomiser.db /opt/atomiser/data/atomiser.db.bak.$(date +%F)
```

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
- Review the journal for Python exceptions from `app/videos.py`.
- Ensure the `atomiser` user has write permission to `/opt/atomiser/uploads/raw` and `/opt/atomiser/uploads/videos`.
- Check SELinux denials if files cannot be written.

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

For day-to-day operation and feature development, see `README.md`.
