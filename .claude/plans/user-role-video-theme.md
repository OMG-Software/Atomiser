# Plan: Role editing, video title editing, and modern theme

## Goals
1. Make user role changes clearly save-able (explicit button instead of hidden auto-submit).
2. Let owners and admins edit a video's title and description from the video page.
3. Refresh the UI with a lightweight, modern CSS theme.

## Changes

### 1. Admin users page — save role button
- File: `app/templates/admin/users.html`
- Replace the `onchange="this.form.submit()"` dropdown with a dropdown + explicit "Update" button.
- Add minimal inline form styling so the button sits neatly beside the select.

### 2. Video metadata editing
- File: `app/videos.py`
- Add `POST /videos/{video_uuid}/edit`:
  - CSRF verified.
  - Owner or admin may edit.
  - Validate title (required, ≤200 chars) and description (≤5000 chars).
  - Redirect back to the video page with `?success=1`.
- File: `app/templates/videos/player.html`
- Show an inline edit form when `is_owner` or `is_admin` is true.
- Pass `is_owner` and `is_admin` from the route to the template.

### 3. Modern theme refresh
- File: `app/static/css/atomiser.css`
- Keep vanilla CSS, no build step.
- Tighten the design system:
  - Refined color palette (dark background, subtle surfaces, blue/violet accent, danger/success tints).
  - Larger radius, subtle shadows, consistent spacing scale.
  - Better buttons (hover/focus/active states).
  - Improved forms (focus rings, disabled states).
  - Cleaner navbar with small-screen wrapping.
  - Better table, card, alert, badge, and video-player styling.
  - Add `.badge-*`, `.btn-secondary`, `.inline-form`, and `.edit-form` utility classes.

### 4. Tests
- New file: `tests/test_admin.py`
  - Configurator can promote/demote a user.
  - Configurator cannot change the bootstrap user's role.
  - Admin cannot change roles (403).
  - CSRF enforced.
- File: `tests/test_videos.py`
  - Owner can edit their video title/description.
  - Admin can edit another user's video.
  - Non-owner member is rejected (403).
  - Missing/too-long title rejected (400).
  - CSRF enforced.

## Out of scope
- Video description was not explicitly requested, but editing it alongside title is natural and uses the same endpoint.
- No new build tools or frameworks; theme stays a single CSS file.
