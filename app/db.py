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


async def _ensure_bootstrap_column(db):
    """Idempotent column addition that SQLite's executescript cannot express."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("PRAGMA table_info(users)")
    cols = [row["name"] for row in await cursor.fetchall()]
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
        await _apply_pragmas(db)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            await db.executescript(sql)
            await db.commit()
        await _ensure_bootstrap_column(db)
        await db.commit()
