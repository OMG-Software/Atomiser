# Atomiser

A small, invite-only community video-hosting web application built with Python and FastAPI.
The default page title is **Atomiser Site** and is editable in the admin settings UI.

## Features

- **Invite-only registration** — invite links with expiry, a use limit, an admin-only note, and revocation.
- **Role-based access** — Configurator, Admin, and Member roles.
- **Authentication** — email/password with Argon2id, TOTP two-factor, and WebAuthn passkeys.
- **Brute-force protection** — in-app sliding-window throttling and account lockout on the auth endpoints, independent of the nginx rate-limit zones.
- **Session management** — users see every device signed in to their account and can revoke any of them; admins can sign a user out everywhere.
- **Protected video hosting** — only authenticated users can upload or view content.
- **Transcoding with a durable queue** — uploads are transcoded into 720p, 480p, and 360p MP4s with a poster thumbnail. Jobs live in the database, so a restart mid-transcode resumes instead of stranding the video, and failures retry.
- **Quality selection** — the player offers every rendition and remembers the viewer's choice, preserving playback position when switching.
- **Live processing feedback** — a video still transcoding shows progress and swaps in the player automatically when it is ready.
- **Secure streaming** — videos are served through the app with nginx X-Accel-Redirect so the file system is never exposed directly.
- **Activity feed** — default landing page shows the latest site-visible videos.
- **User profiles** — each profile lists videos posted by that user; owners can edit, re-scope, or delete their own videos.
- **Admin dashboard** — storage use per user, videos by status, recent registrations, and one-click retry of failed transcodes.
- **Audit log viewer** — filter the recorded activity trail by action, user, and date range.
- **Optional email** — with SMTP configured, invites can be emailed, users can reset their own passwords, and members are notified when someone posts a new video. Without it the site behaves exactly as before: copy-paste invite links and admin-driven resets.
- **New-video notifications** — members are emailed when a video goes live, with a one-click unsubscribe link in every message. Delivery runs through a durable queue with retries, so a mail server hiccup does not lose notifications.

## Quick start (local)

1. Install Python 3.12+ and ffmpeg.
2. Clone the repository and create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and set `SECRET_KEY` to a long random value.
4. Bootstrap the first Configurator account:
   ```bash
   python scripts/bootstrap.py --email you@example.com --name "Your Name" --password "A Strong Password"
   ```
5. Run the app:
   ```bash
   uvicorn app.main:app --reload
   ```
6. Open http://127.0.0.1:8000/ and log in.

## Roles

- **Configurator** — full access; can change site title, manage roles, and delete anything.
- **Admin** — manage users, invites, and content; cannot change global site settings or demote Configurators.
- **Member** — upload and view videos; edit own profile.

## Production deployment

The recommended stack is a Linux VPS/bare-metal server with nginx as a reverse proxy. A detailed, step-by-step guide is in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md); the high-level steps are:

1. Create an unprivileged user and deploy the code to `/opt/atomiser`.
2. Create `/etc/atomiser/atomiser.env` from `.env.example` and set `ENV=production`, `SECRET_KEY`, paths, etc.
3. Create the runtime directory and set permissions:
   ```bash
   sudo mkdir -p /run/atomiser /opt/atomiser/data /opt/atomiser/uploads
   sudo chown -R atomiser:atomiser /opt/atomiser /run/atomiser
   ```
4. Bootstrap the first Configurator:
   ```bash
   sudo -u atomiser venv/bin/python scripts/bootstrap.py --email you@example.com --name "Your Name"
   ```
5. Install `nginx/atomiser.service` to `/etc/systemd/system/atomiser.service` and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now atomiser
   ```
6. Obtain TLS certificates (e.g. Let's Encrypt with certbot) and install `nginx/atomiser.conf`.
7. Reload nginx.

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for TLS setup, backup strategy, troubleshooting, and the full security checklist.

## Optional email

Atomiser runs with no mail server by default. Set `SMTP_HOST` **and** `SMTP_FROM`
in your `.env` to additionally enable:

- **Emailed invites** — an optional "email it to" field on the invite form. The
  link is still shown on screen, so a delivery failure never loses the invite.
- **Self-service password reset** — `/auth/forgot` sends a single-use link that
  expires after `PASSWORD_RESET_TTL_MINUTES`. Completing a reset signs the
  account out of every device.
- **New-video notifications** — when a video finishes transcoding and is visible
  to the site, every subscribed member is emailed a link to it. Members are
  subscribed by default and can opt out from their profile or from the
  unsubscribe link carried by every notification (including the native
  "unsubscribe" button in mail clients that support RFC 8058). Set
  `NOTIFY_NEW_VIDEOS=false` to disable the feature for the whole site.

Set `SITE_URL` as well. Password reset and invite links prefer it over the
request's `Host` header, and notifications **require** it: the sending worker
runs outside any request, so without it there is no hostname to build links
from and no notification will be sent. The admin dashboard shows a warning if
this is the case.

Notifications are delivered through a queue rather than sent inline, so a
fan-out to a large membership never blocks a transcode, failures retry with a
backoff, and a restart mid-send resumes. The admin dashboard reports how many
messages are queued, sent and undeliverable.

With SMTP unset, `/auth/forgot` explains that an admin performs resets, the
invite form omits the email field, the profile page hides the notification
setting, and nothing is ever queued.

## Security notes

- All cookies are `HttpOnly`/`Secure` in production and use `SameSite=Lax`.
- Forms include CSRF tokens using the double-submit cookie pattern.
- Uploaded files are inspected with magic-byte detection before being accepted.
- Video files are stored outside the web root and are only accessible through authenticated app endpoints or nginx internal redirects.
- Passwords are hashed with Argon2id.
- TOTP uses a server-side secret per user; passkey credentials are bound to the origin.
- Failed sign-ins are throttled per email address and per source IP, and an account locks after repeated failures. An unknown address is throttled identically to a real one, so the response cannot be used to enumerate accounts.
- Password reset tokens are stored only as hashes, are single-use, and expire.

## License

Copyright (C) 2026 James Chapman / OMG-Software

Licensed under the GNU Affero General Public License v3.0 — see
[`LICENSE`](LICENSE) for the full text.

Atomiser is intended to be run as a network service, so the AGPL's section 13
matters here: if you run a modified version and let other people use it over a
network, you must offer those users the source of your modified version. Plain
distribution of the source is covered by the usual GPL terms.
