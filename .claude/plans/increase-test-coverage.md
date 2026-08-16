# Increase test coverage plan

## Goal
Add tests that exercise error paths, failure handling, and edge cases across Atomiser's authentication, authorization, video upload/streaming, admin, invites, users, and settings modules.

## Scope
Focus on behavioural/regression tests using the existing `httpx` ASGI client and `pytest-asyncio` fixtures. No production code changes unless a test reveals a genuine bug.

## Areas to cover

### Authentication (`tests/test_auth.py`)
- Login failure paths: missing fields, wrong email, wrong password, wrong TOTP, TOTP missing when enabled.
- Registration failure paths: invalid invite, used/expired invite, duplicate email, weak password, missing CSRF.
- Session management: accessing a logged-in page after logout, expired session handling (if feasible without time travel).
- TOTP setup failure: invalid code, missing CSRF.
- Passkey flows: missing CSRF on JSON endpoints, unauthenticated access to passkey endpoints.

### Authorization / roles (`tests/test_roles.py` existing + additions)
- `has_role` edge cases: unknown role string, comparing member vs admin vs configurator.
- Admin-only endpoints reject members (dashboard, users list, videos list, invites).
- Configurator-only endpoints reject admins (settings, role changes).

### Videos (`tests/test_videos.py`)
- Upload failure paths: missing file, unsupported MIME, oversized file, missing title, invalid visibility value.
- Edit failure paths: non-owner edit, missing title, missing CSRF.
- Streaming/thumbnail failure paths: non-existent video, private video accessed by non-owner/non-admin, malformed UUID, missing rendition.

### Admin (`tests/test_admin.py`)
- Delete user failure paths: self-delete, deleting bootstrap user, deleting configurator as admin, missing CSRF.
- Delete video failure paths: non-existent video, deleting configurator-owned video as admin, missing CSRF.
- Settings failure paths: invalid title length, missing CSRF, admin access denied.

### Invites (`tests/test_invites.py`)
- Create invite failure paths: missing CSRF, member access denied, invalid expires_hours.
- Invite reuse/expiration: using an invite beyond max_uses, using an expired invite.

### Users (`tests/test_users.py`)
- Update profile failure paths: missing CSRF, empty display name.
- Public profile edge cases: non-existent user ID, private videos not shown to strangers.

### Bootstrap / config (`tests/test_bootstrap.py`, `tests/test_settings.py`)
- Bootstrap script refuses to create a second configurator.
- Site settings validation and access control.

## Strategy
1. Read each existing test file to avoid duplicating existing tests.
2. Add focused `test_*` functions grouped by module in the existing test files (and create `test_users.py` which is new).
3. Use the existing fixtures (`client`, `csrf`, `admin_user`, `member_user`, `configurator_user`, `bootstrap_user`, `logged_in_*`).
4. Add small helper functions in test files only when needed to reduce repetition.
5. Run the suite after each module's tests are added to catch issues early.
6. If a test reveals a real bug, fix it as a separate sub-task.

## Verification
Run `venv\Scripts\python -m pytest tests\ -q` and aim for all tests passing with no warnings beyond the existing FastAPI deprecation.
