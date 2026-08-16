import pytest


@pytest.mark.asyncio
async def test_configurator_can_change_site_title(client, logged_in_configurator):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "Test Site", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "success=1" in resp.headers["location"]

    # The new title appears on the settings page.
    page = await client.get("/admin/settings", follow_redirects=False)
    assert page.status_code == 200
    assert "Test Site" in page.text


@pytest.mark.asyncio
async def test_admin_cannot_change_site_title(client, logged_in_admin):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "Hacked", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_settings_rejects_invalid_title(client, logged_in_configurator):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "x" * 121, "csrf": csrf},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_settings_missing_csrf(client, logged_in_configurator):
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "No CSRF"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_settings_blank_title(client, logged_in_configurator):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "   ", "csrf": csrf},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_settings_member_denied(client, logged_in_member):
    csrf = client.cookies.get("csrf")
    resp = await client.post(
        "/admin/settings",
        data={"site_title": "Hacked", "csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403
