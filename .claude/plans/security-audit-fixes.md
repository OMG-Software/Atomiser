# Security audit remediation plan

## Goal
Fix the six issues identified in the defensive security audit of Atomiser.

## Changes by file

### 1. `app/auth.py` — open redirect on login
- Add a small helper `_safe_next_url(next_url: str) -> str` that only allows relative paths starting with `/` and rejects protocol-relative (`//`) and absolute URLs.
- Use it in `login_post` before the `RedirectResponse(url=next, ...)` and in the `LoginRequiredException` handler (`main.py`) if applicable.
- Keep the existing `next` form field; just validate the value at redirect time.

### 2. `app/videos.py` — upload memory exhaustion
- Replace the current "read 8 KB + rest into memory" logic with streaming writes.
- Use `shutil.copyfileobj` or a chunked read/write loop inside `run_in_threadpool` so the full upload is never held in memory.
- Keep the 8 KB magic-byte inspection by reading the first chunk, then seeking back to 0 and streaming the rest to disk.
- Validate `Content-Length` with `try/except ValueError` and return `HTTPException(400)` on bad input.

### 3. `app/admin.py` — Configurator role-change protection
- In `change_role`, after checking `is_bootstrap`, also reject when the target user already has the `configurator` role (so no Configurator can demote another Configurator).
- This matches the UI rule in `admin/users.html` and closes the backend bypass.

### 4. `app/users.py` — profile access for non-admins
- In `user_profile`, replace the unconditional `require_role(current, Role.ADMIN)` call with `has_role(current["role"], Role.ADMIN)` so non-admins can view public profiles.
- Public profiles should show only videos whose `visibility = 'site'`; private videos remain hidden.

### 5. `app/config.py` — `SECRET_KEY` is required but unused
- Remove `SECRET_KEY` from the required environment variables since session tokens and CSRF tokens are already generated with `secrets.token_urlsafe` and stored server-side.
- Keep `SECRET_KEY` as an optional env var (default empty) so existing `.env` files do not break; add a comment explaining it is reserved for future use.

### 6. `app/videos.py` — malformed `Content-Length`
- Wrap `int(content_length)` in a `try/except ValueError` block and raise `HTTPException(status_code=400, detail="Invalid Content-Length")`.

## Tests to add
- `tests/test_auth.py`: login with malicious `next` values (`https://evil.com`, `//evil.com`) should redirect to `/` instead.
- `tests/test_videos.py`: malformed `Content-Length` returns 400; a large upload does not crash the process (streaming behaviour).
- `tests/test_admin.py`: a Configurator cannot demote another Configurator to member/admin.
- `tests/test_users.py`: a member can view another member's public profile without 403.

## Verification
- Run the full pytest suite after all changes: `venv\Scripts\python -m pytest tests\ -q`.
- Confirm all existing tests still pass and the new tests pass.
