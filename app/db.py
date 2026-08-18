import aiosqlite
from pathlib import Path
from app.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


_PRAGMAS = [
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 30000",
]


async def _apply_pragmas(db):
    for pragma in _PRAGMAS:
        await db.execute(pragma)


async def get_db():
    """FastAPI dependency that yields a connected sqlite row-factory cursor."""
    async with aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0) as db:
        db.row_factory = aiosqlite.Row
        await _apply_pragmas(db)
        yield db


async def _table_columns(db, table: str) -> set:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cursor.fetchall()}


# Columns that SQLite cannot add idempotently in DDL (there is no
# "ADD COLUMN IF NOT EXISTS"), so they are applied here after the .sql
# migrations have run. Each entry is the column definition minus the name.
_ADDED_COLUMNS = {
    "users": {
        # Set while an account is locked out after repeated failed logins.
        "locked_until": "TIMESTAMP",
        # Email me when someone posts a new site-visible video. Opt-out: on by
        # default, cleared from the profile page or an unsubscribe link.
        "notify_new_videos": "INTEGER NOT NULL DEFAULT 1",
        # Stable per-user secret in the unsubscribe link, so someone can turn
        # notifications off from an email without signing in. Generated lazily
        # by app/notifications.py; it grants nothing except unsubscribing.
        "notify_token": "TEXT",
    },
    "videos": {
        # Size of the original upload, so the admin dashboard can report real
        # storage use even after the raw file has been purged.
        "raw_size_bytes": "INTEGER",
        # Set when new-video notifications have been queued, so a retried
        # transcode or a second visibility change cannot email everyone twice.
        "notified_at": "TIMESTAMP",
    },
    "sessions": {
        # 'session' for a real login; 'webauthn' for the short-lived rows that
        # only carry a passkey challenge. The session list on /profile shows
        # only real logins.
        "purpose": "TEXT NOT NULL DEFAULT 'session'",
    },
    "transcode_jobs": {
        # Which process holds the job, and until when. requeue_orphans() only
        # reclaims a 'running' row once its lease has expired, so two overlapping
        # processes cannot both transcode the same video.
        "worker_id": "TEXT",
        "lease_expires_at": "TIMESTAMP",
    },
    "email_queue": {
        # Same lease scheme as transcode_jobs. Without it, a second process
        # starting up would requeue messages another worker is mid-send on and
        # deliver them twice.
        "worker_id": "TEXT",
        "lease_expires_at": "TIMESTAMP",
    },
    "invites": {
        # Free-text label so an admin can remember who an invite was for.
        "note": "TEXT",
        # Set when an admin revokes an unused invite.
        "revoked_at": "TIMESTAMP",
    },
}


async def _ensure_columns(db):
    """Apply idempotent column additions that SQLite's DDL cannot express."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = await _table_columns(db, table)
        for name, definition in columns.items():
            if name not in existing:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


async def _ensure_bootstrap_column(db):
    """Idempotent column addition that SQLite's executescript cannot express."""
    db.row_factory = aiosqlite.Row
    cols = await _table_columns(db, "users")
    if "is_bootstrap" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN is_bootstrap INTEGER NOT NULL DEFAULT 0"
        )
    # The earliest configurator is the bootstrap user.
    await db.execute(
        "UPDATE users SET is_bootstrap = 1 WHERE id = ("
        "SELECT MIN(id) FROM users WHERE role = 'configurator'"
        ") AND is_bootstrap = 0"
    )


async def init_db(clear_existing: bool = False):
    """Apply SQL migration files in lexical order.

    Args:
        clear_existing: If True, drop and recreate the database file before
            applying migrations. Useful for tests.
    """
    Config.ensure_dirs()
    if clear_existing and Config.DATABASE_PATH.exists():
        Config.DATABASE_PATH.unlink()
    async with aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0) as db:
        db.row_factory = aiosqlite.Row
        await _apply_pragmas(db)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            await db.executescript(sql)
            await db.commit()
        await _ensure_bootstrap_column(db)
        await _ensure_columns(db)
        await db.commit()
