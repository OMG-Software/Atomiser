"""
Create the first Configurator account for a fresh Atomiser installation.

Usage:
    python scripts/bootstrap.py --email admin@example.com --name "Alex Admin"
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running the script directly from the scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from app.auth import hash_password
from app.config import Config
from app.db import init_db
from app.roles import Role
from app.utils import generate_token


async def bootstrap(email: str, display_name: str, password: str | None = None):
    await init_db()

    async with aiosqlite.connect(Config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id FROM users WHERE role = ?", (Role.CONFIGURATOR.value,))
        if await cursor.fetchone():
            print("A Configurator already exists. Bootstrapping aborted.")
            print("The bootstrap Configurator has full Admin permissions and cannot be demoted or deleted.")
            return

        temp_password = password or generate_token(length=24)
        password_hash = await hash_password(temp_password)

        await db.execute(
            "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
            (email.lower().strip(), password_hash, display_name.strip(), Role.CONFIGURATOR.value, 1),
        )
        await db.commit()

    print(f"Bootstrap Configurator account created: {email}")
    print("This account has full Admin permissions and cannot be demoted or deleted.")
    if password is None:
        print(f"One-time password: {temp_password}")
        print("Change this password immediately after first login.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bootstrap the first Atomiser Configurator")
    parser.add_argument("--email", required=True, help="Email address for the Configurator")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--password", default=None, help="Optional initial password")
    args = parser.parse_args()

    asyncio.run(bootstrap(args.email, args.name, args.password))
