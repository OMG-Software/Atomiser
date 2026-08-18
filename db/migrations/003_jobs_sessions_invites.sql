-- Durable transcode jobs, password resets, login throttling, and the indexes
-- the audit viewer and feed need. Column additions to existing tables cannot be
-- expressed idempotently in SQLite DDL, so they live in _ensure_columns() in
-- app/db.py and run after this script.

-- ---------------------------------------------------------------------------
-- Transcode jobs
-- ---------------------------------------------------------------------------
-- Transcoding used to run as a FastAPI BackgroundTask, which meant a restart
-- mid-transcode left the video stuck in 'uploading' forever with no retry path.
-- A job row survives the restart; app/jobs.py requeues anything left 'running'
-- when the worker starts.
CREATE TABLE IF NOT EXISTS transcode_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON transcode_jobs(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_video ON transcode_jobs(video_id);

-- ---------------------------------------------------------------------------
-- Password resets
-- ---------------------------------------------------------------------------
-- Only the hash is stored, like sessions and invites. A row is single-use
-- (used_at) and time-limited (expires_at).
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    requested_ip TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);

-- ---------------------------------------------------------------------------
-- Login attempts (in-app rate limiting)
-- ---------------------------------------------------------------------------
-- The nginx limit_req zones only protect deployments that use the shipped
-- nginx config. This table backs a portable sliding-window limiter so dev,
-- container, and non-nginx deployments are throttled too.
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,          -- 'login', 'register', 'passkey', 'forgot'
    key_kind TEXT NOT NULL,       -- 'ip' or 'email'
    key_value TEXT NOT NULL,
    successful INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
    ON login_attempts(scope, key_kind, key_value, created_at);

-- ---------------------------------------------------------------------------
-- Indexes for the audit viewer
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
