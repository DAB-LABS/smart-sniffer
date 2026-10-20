"""The raw Agentic access JSON over HTTP (v0.9): GET
/api/smart_sniffer/agentic.json with Home Assistant's own auth.

The view is served by a real aiohttp app with Home Assistant's real auth
middleware and auth manager (users, groups, long-lived access tokens), so
a script's token is checked exactly as Home Assistant checks it. The
machines are the panel tests' (panel_harness). Skipped without Home
Assistant.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from tests import panel_harness as H  # noqa: E402

URL = "/api/smart_sniffer/agentic.json"
SECRET = "s3cret-agent-token-value"


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


def run(tmp_path, body, monkeypatch, specs=None):
    async def main():
        from homeassistant.auth import auth_manager_from_config  # noqa: PLC0415
        from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER  # noqa: PLC0415
        from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN  # noqa: PLC0415
        from homeassistant.components.http.auth import async_setup_auth  # noqa: PLC0415
        from homeassistant.helpers.http import KEY_HASS  # noqa: PLC0415

        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, specs, monkeypatch)
            hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
            views = []
            hass.http = SimpleNamespace(register_view=views.append,
                                        supervisor_unix_socket_path=None)
            app = web.Application()
            app[KEY_HASS] = hass
            await async_setup_auth(hass, app)
            H.panel_ws.register_agentic_view(hass)
            assert [v.url for v in views] == [URL]
            views[0].register(hass, app, app.router)

            async def token(name, group):
                user = await hass.auth.async_create_user(name, group_ids=[group])
                refresh = await hass.auth.async_create_refresh_token(
                    user, client_name=f"{name} script",
                    token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                    access_token_expiration=timedelta(days=30))
                return hass.auth.async_create_access_token(refresh)

            tokens = {"admin": await token("Admin", GROUP_ID_ADMIN),
                      "user": await token("Viewer", GROUP_ID_USER)}
            async with TestClient(TestServer(app)) as client:
                await body(hass, board, client, tokens)
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_an_admins_long_lived_token_reads_the_document(tmp_path, monkeypatch):
    specs = [H.BOARD[0], {**H.BOARD[1], "token": SECRET}, H.BOARD[2]]

    async def body(hass, board, client, tokens):
        resp = await client.get(URL, headers=bearer(tokens["admin"]))
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        assert resp.headers["Cache-Control"] == "no-store"
        assert "Content-Disposition" not in resp.headers
        text = await resp.text()
        assert SECRET not in text
        doc = json.loads(text)
        assert doc["schema"] == "smart-sniffer-agentic-access"
        assert doc["counts"]["agents"] == 3
        # The same document the panel's Agentic access shows.
        panel = await H.call(hass, H.FakeConnection(hass), "agentic")
        same = json.loads(panel["text"])
        for d in (doc, same):
            d.pop("generated_at")
            for agent in d["agents"]:
                agent.pop("data_age_seconds", None)
        assert doc == same
        nas = next(a for a in doc["agents"] if a["id"] == H.BOARD[1]["id"])
        assert nas["has_token"] is True and "token" not in nas
    run(tmp_path, body, monkeypatch, specs)


def test_download_and_fresh(tmp_path, monkeypatch):
    async def body(hass, board, client, tokens):
        resp = await client.get(URL + "?download=1", headers=bearer(tokens["admin"]))
        assert resp.status == 200
        disposition = resp.headers["Content-Disposition"]
        assert re.fullmatch(r'attachment; filename="smart-sniffer-\d{4}-\d{2}-\d{2}\.json"',
                            disposition), disposition
        doc = json.loads(await resp.text())
        assert disposition.endswith(f'smart-sniffer-{doc["generated_at"][:10]}.json"')
        before = board.coordinator(H.BOARD[1]["id"]).refreshes
        resp = await client.get(URL + "?fresh=1", headers=bearer(tokens["admin"]))
        assert resp.status == 200 and "Content-Disposition" not in resp.headers
        assert board.coordinator(H.BOARD[1]["id"]).refreshes == before + 1
    run(tmp_path, body, monkeypatch, H.BOARD[:2])


def test_non_admin_and_no_auth_are_refused(tmp_path, monkeypatch):
    async def body(hass, board, client, tokens):
        resp = await client.get(URL, headers=bearer(tokens["user"]))
        assert resp.status == 403
        assert json.loads(await resp.text())["error"] == "unauthorized"
        resp = await client.get(URL)
        assert resp.status == 401
        resp = await client.get(URL, headers=bearer("not-a-token"))
        assert resp.status == 401
        resp = await client.get(URL + "?download=1&fresh=1")
        assert resp.status == 401
        assert board.coordinator(H.BOARD[1]["id"]).refreshes == 0  # nothing asked
    run(tmp_path, body, monkeypatch, H.BOARD[:2])


def test_the_page_says_how_to_use_a_token():
    from pathlib import Path  # noqa: PLC0415

    page = (Path(H.ROOT) / "custom_components/smart_sniffer/panel/index.html").read_text()
    assert 'curl -H "Authorization: Bearer &lt;token&gt;"' in page
    assert "/api/smart_sniffer/agentic.json" in page
