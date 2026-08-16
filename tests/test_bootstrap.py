import pytest


@pytest.mark.asyncio
async def test_bootstrap_script_refuses_second_configurator(db):
    from scripts.bootstrap import bootstrap
    result = await bootstrap("another@example.com", "Another Admin", "Password123456")
    assert result is None

    cur = await db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'configurator'")
    row = await cur.fetchone()
    assert row["c"] == 1


@pytest.mark.asyncio
async def test_bootstrap_user_is_configurator_and_bootstrap(db, bootstrap_user):
    cur = await db.execute("SELECT role, is_bootstrap FROM users WHERE email = ?", (bootstrap_user["email"],))
    row = await cur.fetchone()
    assert row["role"] == "configurator"
    assert row["is_bootstrap"] == 1


@pytest.mark.asyncio
async def test_bootstrap_script_requires_existing_db(db):
    """The bootstrap script is idempotent and refuses to add a second configurator."""
    from scripts.bootstrap import bootstrap

    # First call already created the bootstrap user in the previous test; this call should abort.
    result = await bootstrap("yetanother@example.com", "Yet Another", "Password123456")
    assert result is None

    cur = await db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'configurator'")
    row = await cur.fetchone()
    assert row["c"] == 1
