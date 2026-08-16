import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ENV", "development")

from app.config import Config
from app.db import init_db
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def test_env():
    """Force test-scoped database and upload directories."""
    tmp = Path(tempfile.mkdtemp(prefix="atomiser_tests_"))
    Config.DATABASE_PATH = tmp / "atomiser.db"
    Config.UPLOAD_DIR = tmp / "uploads"
    Config.ensure_dirs()
    yield tmp
    # Optional: cleanup is handled by tempfile on reboot; keep for debugging.


@pytest_asyncio.fixture(scope="function")
async def db():
    """Fresh database per test function."""
    Config.ensure_dirs()
    await init_db(clear_existing=True)
    async with __import__("aiosqlite").connect(Config.DATABASE_PATH) as db:
        db.row_factory = __import__("aiosqlite").Row
        yield db


@pytest_asyncio.fixture(scope="function")
async def client(db):
    """Async httpx client for FastAPI app with test DB."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="function")
async def csrf(client):
    """Fetch the CSRF cookie from the login page."""
    resp = await client.get("/auth/login")
    assert resp.status_code == 200
    token = resp.cookies.get("csrf")
    assert token
    return token


@pytest_asyncio.fixture(scope="function")
async def admin_user(db):
    """Create an admin user and return their credentials."""
    from app.auth import hash_password
    password = "AdminPass123456"
    pwd_hash = await hash_password(password)
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("admin@example.com", pwd_hash, "Admin User", "admin", 0),
    )
    await db.commit()
    return {"email": "admin@example.com", "password": password, "role": "admin"}


@pytest_asyncio.fixture(scope="function")
async def member_user(db):
    """Create a regular member user and return their credentials."""
    from app.auth import hash_password
    password = "MemberPass123456"
    pwd_hash = await hash_password(password)
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("member@example.com", pwd_hash, "Member User", "member", 0),
    )
    await db.commit()
    return {"email": "member@example.com", "password": password, "role": "member"}


@pytest_asyncio.fixture(scope="function")
async def configurator_user(db):
    """Create a Configurator (not the bootstrap user) and return credentials."""
    from app.auth import hash_password
    password = "ConfigPass123456"
    pwd_hash = await hash_password(password)
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("config@example.com", pwd_hash, "Configurator User", "configurator", 0),
    )
    await db.commit()
    return {"email": "config@example.com", "password": password, "role": "configurator"}


@pytest_asyncio.fixture(scope="function")
async def bootstrap_user(db):
    """Create the immutable bootstrap Configurator."""
    from app.auth import hash_password
    password = "BootstrapPass123456"
    pwd_hash = await hash_password(password)
    await db.execute(
        "INSERT INTO users (email, password_hash, display_name, role, is_bootstrap) VALUES (?, ?, ?, ?, ?)",
        ("bootstrap@example.com", pwd_hash, "Bootstrap User", "configurator", 1),
    )
    await db.commit()
    return {"email": "bootstrap@example.com", "password": password, "role": "configurator"}


async def login(client, email, password, csrf):
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password, "csrf": csrf, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return resp


async def logout(client):
    resp = await client.get("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    return resp


@pytest_asyncio.fixture(scope="function")
async def logged_in_admin(client, admin_user, csrf):
    await login(client, admin_user["email"], admin_user["password"], csrf)
    return admin_user


@pytest_asyncio.fixture(scope="function")
async def logged_in_member(client, member_user, csrf):
    await login(client, member_user["email"], member_user["password"], csrf)
    return member_user


@pytest_asyncio.fixture(scope="function")
async def logged_in_configurator(client, configurator_user, csrf):
    await login(client, configurator_user["email"], configurator_user["password"], csrf)
    return configurator_user


@pytest_asyncio.fixture(scope="function")
async def logged_in_bootstrap(client, bootstrap_user, csrf):
    await login(client, bootstrap_user["email"], bootstrap_user["password"], csrf)
    return bootstrap_user
