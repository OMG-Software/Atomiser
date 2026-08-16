"""
Idempotent migration for the bootstrap user flag.

Adds `is_bootstrap` to the users table if missing and marks the earliest
Configurator as the bootstrap user. This is kept as a standalone script (and
also run automatically at application startup) because SQLite does not support
conditional ALTER TABLE in plain SQL.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from app.config import Config
from app.db import _apply_pragmas


async def migrate():
    Config.ensure_dirs()
    async with aiosqlite.connect(Config.DATABASE_PATH, timeout=30.0) as db:
        await _apply_pragmas(db)
        cursor = await db.execute("PRAGMA table_info(users)")
        cols = [row["name"] for row in await cursor.fetchall()]
        if "is_bootstrap" not in cols:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_bootstrap INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
            print("Added is_bootstrap column")
        else:
            print("is_bootstrap column already exists")

        await db.execute(
            "UPDATE users SET is_bootstrap = 1 WHERE id = ("
            "SELECT MIN(id) FROM users WHERE role = 'configurator'"
            ") AND is_bootstrap = 0"
        )
        await db.commit()
        print("Backfilled bootstrap flag")


if __name__ == "__main__":
    asyncio.run(migrate())
