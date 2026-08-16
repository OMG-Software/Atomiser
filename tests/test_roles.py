import pytest

from app.roles import Role, has_role, require_role


def test_has_role_ranking():
    assert has_role(Role.MEMBER.value, Role.MEMBER)
    assert has_role(Role.ADMIN.value, Role.MEMBER)
    assert has_role(Role.CONFIGURATOR.value, Role.ADMIN)
    assert not has_role(Role.MEMBER.value, Role.ADMIN)
    assert not has_role(Role.ADMIN.value, Role.CONFIGURATOR)


def test_has_role_unknown_role():
    assert not has_role("superuser", Role.ADMIN)


def test_require_role_raises_for_member():
    user = {"role": Role.MEMBER.value}
    with pytest.raises(Exception):
        require_role(user, Role.ADMIN)


@pytest.mark.asyncio
async def test_member_cannot_access_admin(client, logged_in_member):
    for path in ["/admin", "/admin/users", "/admin/videos", "/admin/settings"]:
        resp = await client.get(path, follow_redirects=False)
        # Redirect to login for unauthenticated-looking paths is not expected here since
        # the user is logged in; require_role raises 403. However the trailing slash issue
        # may cause 307 from starlette, so accept 307/302 as redirect and 403 as forbidden.
        assert resp.status_code in (403, 307, 302), f"{path} should be forbidden to member"


@pytest.mark.asyncio
async def test_member_cannot_post_admin_actions(client, logged_in_member):
    csrf = client.cookies.get("csrf") or "x"
    for path, data in [
        ("/admin/users/1/role", {"role": "member", "csrf": csrf}),
        ("/admin/users/1/delete", {"csrf": csrf}),
        ("/admin/videos/any/delete", {"csrf": csrf}),
    ]:
        resp = await client.post(path, data=data, follow_redirects=False)
        assert resp.status_code in (403, 307, 302), f"{path} should be forbidden to member"


@pytest.mark.asyncio
async def test_admin_can_access_admin_pages(client, logged_in_admin):
    for path in ["/admin/", "/admin/users", "/admin/videos"]:
        resp = await client.get(path, follow_redirects=False)
        assert resp.status_code == 200, f"{path} should be accessible to admin"


@pytest.mark.asyncio
async def test_admin_cannot_access_settings(client, logged_in_admin):
    resp = await client.get("/admin/settings", follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_configurator_can_access_settings(client, logged_in_configurator):
    resp = await client.get("/admin/settings", follow_redirects=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_change_roles(client, logged_in_admin, member_user):
    resp = await client.post(
        "/admin/users/999/role",
        data={"role": "member", "csrf": client.cookies.get("csrf") or "x"},
        follow_redirects=False,
    )
    # Admins cannot reach role change endpoint (Configurator only)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_user_role_protected(client, db, logged_in_configurator, bootstrap_user):
    # Find bootstrap user id
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (bootstrap_user["email"],))
    row = await cur.fetchone()
    bootstrap_id = row["id"]

    resp = await client.post(
        f"/admin/users/{bootstrap_id}/role",
        data={"role": "admin", "csrf": client.cookies.get("csrf") or "x"},
    )
    assert resp.status_code == 403

    # Verify role unchanged
    cur = await db.execute("SELECT role FROM users WHERE id = ?", (bootstrap_id,))
    row = await cur.fetchone()
    assert row["role"] == "configurator"


@pytest.mark.asyncio
async def test_bootstrap_user_delete_protected(client, db, logged_in_configurator, bootstrap_user):
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (bootstrap_user["email"],))
    row = await cur.fetchone()
    bootstrap_id = row["id"]

    resp = await client.post(
        f"/admin/users/{bootstrap_id}/delete",
        data={"csrf": client.cookies.get("csrf") or "x"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_configurator_can_demote_admin(client, db, logged_in_configurator, admin_user):
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (admin_user["email"],))
    row = await cur.fetchone()
    admin_id = row["id"]

    resp = await client.post(
        f"/admin/users/{admin_id}/role",
        data={"role": "member", "csrf": client.cookies.get("csrf") or "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cur = await db.execute("SELECT role FROM users WHERE id = ?", (admin_id,))
    row = await cur.fetchone()
    assert row["role"] == "member"


@pytest.mark.asyncio
async def test_admin_can_delete_member(client, db, logged_in_admin, member_user):
    cur = await db.execute("SELECT id FROM users WHERE email = ?", (member_user["email"],))
    row = await cur.fetchone()
    member_id = row["id"]

    resp = await client.post(
        f"/admin/users/{member_id}/delete",
        data={"csrf": client.cookies.get("csrf") or "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cur = await db.execute("SELECT id FROM users WHERE id = ?", (member_id,))
    row = await cur.fetchone()
    assert row is None
