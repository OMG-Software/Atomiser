-- Outbound email queue.
--
-- A new-video notification fans out to every subscribed member, so sending it
-- inline would block the transcode worker for as long as the mail server takes
-- times the number of recipients. Each message is a row instead: the worker in
-- app/notifications.py drains them over a shared SMTP connection, retries
-- transient failures with a backoff, and survives a restart mid-send.
--
-- Column additions to existing tables live in _ADDED_COLUMNS in app/db.py,
-- because SQLite has no ADD COLUMN IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS email_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL once the recipient's account is deleted; the address is kept so an
    -- in-flight message can still be delivered or reported on.
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    to_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    -- 'new_video', 'test', etc. Lets the admin view group them.
    kind TEXT NOT NULL DEFAULT 'notification',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    -- Set into the future to back a retry off; the worker ignores rows whose
    -- scheduled_for has not arrived.
    scheduled_for TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL,
    sent_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_email_queue_pending ON email_queue(status, scheduled_for, id);
CREATE INDEX IF NOT EXISTS idx_email_queue_user ON email_queue(user_id);
