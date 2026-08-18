"""Tests for new-video email notifications (app/notifications.py)."""

import uuid

import pytest

from app import mail, notifications
from app.config import Config


@pytest.fixture
def mail_configured(monkeypatch):
    """Pretend SMTP and SITE_URL are set up, capturing batches instead of sending."""
    monkeypatch.setattr(Config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(Config, "SMTP_FROM", "atomiser@example.com")
    monkeypatch.setattr(Config, "SITE_URL", "https://videos.example.com")
    monkeypatch.setattr(Config, "NOTIFY_NEW_VIDEOS", True)

    sent = []

    async def _capture(items, site_title="Atomiser"):
        sent.extend(items)
        return {}

    monkeypatch.setattr(mail, "send_batch", _capture)
    return sent


async def _user_id(db, email):
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    return row["id"]


async def _add_member(db, email, notify=1):
    from app.auth import hash_password

    cursor = await db.execute(
        """
        INSERT INTO users (email, password_hash, display_name, role, is_bootstrap, notify_new_videos)
        VALUES (?, ?, ?, 'member', 0, ?)
        """,
        (email, await hash_password("SomePassword12345"), email.split("@")[0], notify),
    )
    await db.commit()
    return cursor.lastrowid


async def _ready_video(db, owner_id, visibility="site", status="ready", title="A new clip"):
    video_uuid = str(uuid.uuid4())
    cursor = await db.execute(
        """
        INSERT INTO videos (uuid, owner_id, title, description, visibility, status)
        VALUES (?, ?, ?, 'Something worth watching', ?, ?)
        """,
        (video_uuid, owner_id, title, visibility, status),
    )
    await db.commit()
    return cursor.lastrowid, video_uuid


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ready_video_queues_one_email_per_subscriber(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    await _add_member(db, "b@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    queued = await notifications.queue_new_video_notifications(db, video_id)
    assert queued == 2

    cursor = await db.execute("SELECT to_address, kind, status FROM email_queue ORDER BY to_address")
    rows = [dict(r) for r in await cursor.fetchall()]
    assert [r["to_address"] for r in rows] == ["a@example.com", "b@example.com"]
    assert {r["kind"] for r in rows} == {"new_video"}
    assert {r["status"] for r in rows} == {"queued"}


@pytest.mark.asyncio
async def test_uploader_is_not_emailed_about_their_own_video(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "someone@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    await notifications.queue_new_video_notifications(db, video_id)

    cursor = await db.execute("SELECT to_address FROM email_queue")
    addresses = [r["to_address"] for r in await cursor.fetchall()]
    assert member_user["email"] not in addresses


@pytest.mark.asyncio
async def test_unsubscribed_members_are_skipped(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "wants@example.com", notify=1)
    await _add_member(db, "optedout@example.com", notify=0)
    video_id, _ = await _ready_video(db, owner_id)

    await notifications.queue_new_video_notifications(db, video_id)

    cursor = await db.execute("SELECT to_address FROM email_queue")
    addresses = [r["to_address"] for r in await cursor.fetchall()]
    assert addresses == ["wants@example.com"]


@pytest.mark.asyncio
async def test_members_are_subscribed_by_default(db, member_user, mail_configured):
    """Opt-out: a fresh account receives notifications without doing anything."""
    owner_id = await _user_id(db, member_user["email"])
    cursor = await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role) VALUES (?, 'x', 'New', 'member')",
        ("fresh@example.com",),
    )
    await db.commit()

    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE email = 'fresh@example.com'")
    assert (await cursor.fetchone())["notify_new_videos"] == 1

    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    cursor = await db.execute("SELECT to_address FROM email_queue")
    assert "fresh@example.com" in [r["to_address"] for r in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_private_video_notifies_nobody(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id, visibility="private")

    assert await notifications.queue_new_video_notifications(db, video_id) == 0


@pytest.mark.asyncio
async def test_unready_video_notifies_nobody(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id, status="processing")

    assert await notifications.queue_new_video_notifications(db, video_id) == 0


@pytest.mark.asyncio
async def test_fan_out_happens_only_once(db, member_user, mail_configured):
    """A retried transcode must not email the whole membership twice."""
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    assert await notifications.queue_new_video_notifications(db, video_id) == 1
    assert await notifications.queue_new_video_notifications(db, video_id) == 0

    cursor = await db.execute("SELECT COUNT(*) AS c FROM email_queue")
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_nothing_queued_without_smtp(db, member_user, monkeypatch):
    monkeypatch.setattr(Config, "SMTP_HOST", "")
    monkeypatch.setattr(Config, "SMTP_FROM", "")

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    assert await notifications.queue_new_video_notifications(db, video_id) == 0


@pytest.mark.asyncio
async def test_master_switch_disables_notifications(db, member_user, mail_configured, monkeypatch):
    monkeypatch.setattr(Config, "NOTIFY_NEW_VIDEOS", False)

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    assert await notifications.queue_new_video_notifications(db, video_id) == 0


@pytest.mark.asyncio
async def test_missing_site_url_refuses_rather_than_sending_broken_links(
    db, member_user, mail_configured, monkeypatch
):
    monkeypatch.setattr(Config, "SITE_URL", "")

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)

    assert await notifications.queue_new_video_notifications(db, video_id) == 0
    # The fan-out was not claimed, so it can still go out once SITE_URL is set.
    cursor = await db.execute("SELECT notified_at FROM videos WHERE id = ?", (video_id,))
    assert (await cursor.fetchone())["notified_at"] is None


@pytest.mark.asyncio
async def test_body_carries_the_video_and_unsubscribe_links(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, video_uuid = await _ready_video(db, owner_id, title="Bridge timelapse")

    await notifications.queue_new_video_notifications(db, video_id)

    cursor = await db.execute("SELECT subject, body FROM email_queue")
    row = await cursor.fetchone()
    assert "Bridge timelapse" in row["subject"]
    assert f"https://videos.example.com/videos/{video_uuid}" in row["body"]
    assert "https://videos.example.com/notifications/unsubscribe?token=" in row["body"]


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_sends_and_marks_queue_rows_sent(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    batch = await notifications._claim_batch(db)
    assert len(batch) == 1
    await notifications._send_batch(db, batch)

    cursor = await db.execute("SELECT status, sent_at FROM email_queue")
    row = await cursor.fetchone()
    assert row["status"] == "sent"
    assert row["sent_at"] is not None
    assert len(mail_configured) == 1


@pytest.mark.asyncio
async def test_a_message_is_only_claimed_once(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    first = await notifications._claim_batch(db)
    second = await notifications._claim_batch(db)

    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_batch_size_bounds_a_pass(db, member_user, mail_configured, monkeypatch):
    monkeypatch.setattr(Config, "EMAIL_BATCH_SIZE", 2)

    owner_id = await _user_id(db, member_user["email"])
    for i in range(5):
        await _add_member(db, f"m{i}@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    assert len(await notifications._claim_batch(db)) == 2


@pytest.mark.asyncio
async def test_failed_send_is_retried_with_backoff(db, member_user, mail_configured, monkeypatch):
    async def _all_fail(items, site_title="Atomiser"):
        return {index: "mailbox unavailable" for index in range(len(items))}

    monkeypatch.setattr(mail, "send_batch", _all_fail)

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    batch = await notifications._claim_batch(db)
    await notifications._send_batch(db, batch)

    cursor = await db.execute("SELECT status, attempts, last_error, scheduled_for FROM email_queue")
    row = await cursor.fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["last_error"] == "mailbox unavailable"

    # Backed off into the future, so the next pass does not immediately retry.
    from app.utils import now_utc

    assert row["scheduled_for"] > now_utc().isoformat()


@pytest.mark.asyncio
async def test_message_is_abandoned_after_max_attempts(db, member_user, mail_configured, monkeypatch):
    monkeypatch.setattr(Config, "EMAIL_MAX_ATTEMPTS", 1)

    async def _all_fail(items, site_title="Atomiser"):
        return {index: "nope" for index in range(len(items))}

    monkeypatch.setattr(mail, "send_batch", _all_fail)

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    batch = await notifications._claim_batch(db)
    await notifications._send_batch(db, batch)

    cursor = await db.execute("SELECT status FROM email_queue")
    assert (await cursor.fetchone())["status"] == "failed"


@pytest.mark.asyncio
async def test_one_bad_address_does_not_block_the_rest(db, member_user, mail_configured, monkeypatch):
    async def _first_fails(items, site_title="Atomiser"):
        return {0: "no such mailbox"}

    monkeypatch.setattr(mail, "send_batch", _first_fails)

    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "bad@example.com")
    await _add_member(db, "good@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)

    batch = await notifications._claim_batch(db)
    await notifications._send_batch(db, batch)

    cursor = await db.execute("SELECT to_address, status FROM email_queue ORDER BY to_address")
    rows = {r["to_address"]: r["status"] for r in await cursor.fetchall()}
    assert rows["bad@example.com"] == "queued"   # will be retried
    assert rows["good@example.com"] == "sent"


@pytest.mark.asyncio
async def test_requeue_orphans_recovers_in_flight_messages(db, member_user, mail_configured):
    owner_id = await _user_id(db, member_user["email"])
    await _add_member(db, "a@example.com")
    video_id, _ = await _ready_video(db, owner_id)
    await notifications.queue_new_video_notifications(db, video_id)
    await notifications._claim_batch(db)

    assert await notifications.requeue_orphans(db) == 1

    cursor = await db.execute("SELECT status FROM email_queue")
    assert (await cursor.fetchone())["status"] == "queued"


@pytest.mark.asyncio
async def test_prune_drops_old_delivered_rows(db, member_user, mail_configured):
    from datetime import timedelta

    from app.utils import now_utc

    old = (now_utc() - timedelta(days=Config.EMAIL_RETENTION_DAYS + 1)).isoformat()
    await db.execute(
        """
        INSERT INTO email_queue (to_address, subject, body, status, scheduled_for, created_at)
        VALUES ('old@example.com', 's', 'b', 'sent', ?, ?)
        """,
        (old, old),
    )
    await db.execute(
        """
        INSERT INTO email_queue (to_address, subject, body, status, scheduled_for, created_at)
        VALUES ('new@example.com', 's', 'b', 'queued', ?, ?)
        """,
        (now_utc().isoformat(), now_utc().isoformat()),
    )
    await db.commit()

    assert await notifications.prune(db) == 1

    cursor = await db.execute("SELECT to_address FROM email_queue")
    assert [r["to_address"] for r in await cursor.fetchall()] == ["new@example.com"]


# ---------------------------------------------------------------------------
# Publishing a previously private video
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_making_a_video_public_later_notifies(client, logged_in_member, db, mail_configured):
    owner_id = await _user_id(db, logged_in_member["email"])
    await _add_member(db, "a@example.com")
    video_id, video_uuid = await _ready_video(db, owner_id, visibility="private")

    csrf = client.cookies.get("csrf")
    resp = await client.post(
        f"/videos/{video_uuid}/edit",
        data={"title": "Now public", "description": "", "visibility": "site", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cursor = await db.execute("SELECT COUNT(*) AS c FROM email_queue")
    assert (await cursor.fetchone())["c"] == 1


@pytest.mark.asyncio
async def test_toggling_visibility_back_and_forth_notifies_once(
    client, logged_in_member, db, mail_configured
):
    owner_id = await _user_id(db, logged_in_member["email"])
    await _add_member(db, "a@example.com")
    video_id, video_uuid = await _ready_video(db, owner_id, visibility="private")
    csrf = client.cookies.get("csrf")

    for visibility in ("site", "private", "site"):
        await client.post(
            f"/videos/{video_uuid}/edit",
            data={"title": "Flip flop", "description": "", "visibility": visibility, "csrf": csrf},
            follow_redirects=False,
        )

    cursor = await db.execute("SELECT COUNT(*) AS c FROM email_queue")
    assert (await cursor.fetchone())["c"] == 1


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsubscribe_get_does_not_change_anything(client, db, member_user, mail_configured):
    """Mail scanners prefetch links, so a GET must never unsubscribe someone."""
    user_id = await _user_id(db, member_user["email"])
    token = await notifications.unsubscribe_token(db, user_id)

    resp = await client.get(f"/notifications/unsubscribe?token={token}")
    assert resp.status_code == 200
    assert "unsubscribe" in resp.text.lower()

    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 1


@pytest.mark.asyncio
async def test_unsubscribe_post_turns_notifications_off(client, db, member_user, mail_configured):
    user_id = await _user_id(db, member_user["email"])
    token = await notifications.unsubscribe_token(db, user_id)

    resp = await client.post("/notifications/unsubscribe", data={"token": token})
    assert resp.status_code == 200

    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 0


@pytest.mark.asyncio
async def test_one_click_unsubscribe_via_query_string(client, db, member_user, mail_configured):
    """RFC 8058 clients POST to the List-Unsubscribe URL with no form body."""
    user_id = await _user_id(db, member_user["email"])
    token = await notifications.unsubscribe_token(db, user_id)

    resp = await client.post(f"/notifications/unsubscribe?token={token}")
    assert resp.status_code == 200

    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 0


@pytest.mark.asyncio
async def test_resubscribe_turns_them_back_on(client, db, member_user, mail_configured):
    user_id = await _user_id(db, member_user["email"])
    token = await notifications.unsubscribe_token(db, user_id)
    await notifications.set_preference(db, user_id, False)

    resp = await client.post(
        "/notifications/unsubscribe", data={"token": token, "resubscribe": "1"}
    )
    assert resp.status_code == 200

    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 1


@pytest.mark.asyncio
async def test_unknown_unsubscribe_token_is_rejected(client, db, member_user):
    resp = await client.post("/notifications/unsubscribe", data={"token": "not-a-real-token"})
    assert resp.status_code == 404

    resp = await client.get("/notifications/unsubscribe?token=not-a-real-token")
    assert resp.status_code == 404
    assert "not recognised" in resp.text.lower()


@pytest.mark.asyncio
async def test_unsubscribe_needs_no_login(client, db, member_user, mail_configured):
    """The token is the credential, so this works straight from a mail client."""
    user_id = await _user_id(db, member_user["email"])
    token = await notifications.unsubscribe_token(db, user_id)

    resp = await client.post(
        "/notifications/unsubscribe", data={"token": token}, follow_redirects=False
    )
    assert resp.status_code == 200
    assert "/auth/login" not in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_tokens_are_stable_and_distinct_per_user(db, member_user, admin_user):
    member_id = await _user_id(db, member_user["email"])
    admin_id = await _user_id(db, admin_user["email"])

    first = await notifications.unsubscribe_token(db, member_id)
    again = await notifications.unsubscribe_token(db, member_id)
    other = await notifications.unsubscribe_token(db, admin_id)

    assert first and first == again
    assert other != first


# ---------------------------------------------------------------------------
# Profile preference
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_shows_the_toggle_when_mail_is_on(client, logged_in_member, mail_configured):
    resp = await client.get("/profile")
    assert resp.status_code == 200
    assert 'name="notify_new_videos"' in resp.text


@pytest.mark.asyncio
async def test_profile_hides_the_toggle_without_smtp(client, logged_in_member):
    resp = await client.get("/profile")
    assert resp.status_code == 200
    assert 'name="notify_new_videos"' not in resp.text
    assert "not configured to send email" in resp.text


@pytest.mark.asyncio
async def test_profile_can_turn_notifications_off_and_on(client, logged_in_member, db, mail_configured):
    user_id = await _user_id(db, logged_in_member["email"])
    csrf = client.cookies.get("csrf")

    # An unchecked box is simply absent from the body.
    resp = await client.post(
        "/profile", data={"display_name": "Member User", "csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 0

    resp = await client.post(
        "/profile",
        data={"display_name": "Member User", "notify_new_videos": "1", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cursor = await db.execute("SELECT notify_new_videos FROM users WHERE id = ?", (user_id,))
    assert (await cursor.fetchone())["notify_new_videos"] == 1


@pytest.mark.asyncio
async def test_profile_update_still_requires_csrf(client, logged_in_member):
    resp = await client.post(
        "/profile", data={"display_name": "No CSRF"}, follow_redirects=False
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------

def test_unsubscribe_headers_are_set_for_bulk_mail(monkeypatch):
    monkeypatch.setattr(Config, "SMTP_FROM", "atomiser@example.com")
    message = mail._build_message(
        "someone@example.com", "New video", "body",
        "Atomiser Site", "https://videos.example.com/notifications/unsubscribe?token=abc",
    )
    assert message["List-Unsubscribe"] == "<https://videos.example.com/notifications/unsubscribe?token=abc>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_no_unsubscribe_headers_on_transactional_mail(monkeypatch):
    monkeypatch.setattr(Config, "SMTP_FROM", "atomiser@example.com")
    message = mail._build_message("someone@example.com", "Reset", "body", "Atomiser Site")
    assert message["List-Unsubscribe"] is None
