"""The SMART Sniffer panel's websocket API, Stores and registration (v0.9).

A real Home Assistant core (config entries, device registry, Stores in a
temporary folder, dispatcher) with the machines of tests/panel_harness.py:
coordinators stand in for agents. Every command goes through its real
decorators and schema. Skipped without Home Assistant, like the other
*_under_ha tests.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import (  # noqa: E402
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntryChange,
)
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: E402

from tests import panel_harness as H  # noqa: E402
from tests.test_device_names_under_registry import _package_init  # noqa: E402

NAS = "01NAS00000000000000000000B"
MAC = "01MAC00000000000000000000C"
PI = "01PI000000000000000000000D"
HAOS = "01HAOS0000000000000000000A"


def run(tmp_path, body, monkeypatch=None, specs=None):
    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, specs, monkeypatch)
            await body(hass, board, H.FakeConnection(hass))
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _fresh_panel_data(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


def _machines(result):
    return {m["host"]["id"]: m for m in result["machines"]}


# ── cards: every entry, any state ─────────────────────────────────────────


def test_cards_has_every_machine_in_any_state(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        result = await H.call(hass, conn, "cards")
        machines = _machines(result)
        assert set(machines) == {HAOS, NAS, MAC, PI}
        assert machines[NAS]["data"]["status"] == "ok"
        assert machines[NAS]["data"]["summary"]["header"] == "attention"
        assert machines[MAC]["data"]["status"] == "unreachable"
        assert all(i["stale"] for i in machines[MAC]["data"]["items"])
        assert machines[PI]["data"]["status"] == "password_needed"
        assert machines[HAOS]["host"]["kind"] == "haos" and machines[HAOS]["host"]["fixed"]
        assert machines[HAOS]["host"]["removable"] is False
        # Every drive and pool card has its device; the trash knows what it can do.
        target = machines[NAS]["ha"]["target"]
        assert any(k.startswith("pool:") for k in target["devices"])
        assert set(machines[NAS]["ha"]["removal"].values()) <= {"disable", "delete", "entry"}
    run(tmp_path, body, monkeypatch)


def test_no_answer_holds_a_password(tmp_path, monkeypatch):
    specs = [{**H.BOARD[1], "token": "s3cret-token-value"}]

    async def body(hass, board, conn):
        texts = [json.dumps(await H.call(hass, conn, "cards")),
                 json.dumps(await H.call(hass, conn, "machine", entry_id=NAS)),
                 json.dumps(await H.call(hass, conn, "agentic"))]
        for text in texts:
            assert "s3cret-token-value" not in text
        assert _machines(json.loads(texts[0]))[NAS]["host"]["has_password"] is True
        await hass.async_block_till_done()
        await H.panel_data.panel_data(hass).state._store.async_save(
            H.panel_data.panel_data(hass).state.data)
        for path in Path(tmp_path, ".storage").glob("smart_sniffer*"):
            assert "s3cret-token-value" not in path.read_text()
    run(tmp_path, body, monkeypatch, specs)


def test_every_command_is_admin_only(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        viewer = H.FakeConnection(hass, admin=False)
        for command, fields in (("cards", {}), ("info", {}), ("agentic", {}),
                                ("card/hidden", {"card_id": f"{NAS}|x", "hidden": True}),
                                ("machine/password", {"entry_id": PI, "password": "x"}),
                                ("subscribe", {})):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, viewer, command, **fields)
            assert err.value.code == "unauthorized", command
        assert len(H.panel_ws.COMMANDS) == 30
        for handler in H.panel_ws.COMMANDS:
            assert handler._ws_command.startswith("smart_sniffer/panel/")
    run(tmp_path, body, monkeypatch)


def test_schemas_refuse_unknown_fields_and_bad_values(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        for command, fields in (("card/hidden", {"card_id": f"{NAS}|x", "hidden": "yes"}),
                                ("cards", {"extra": 1}),
                                ("net/check", {"host": "a", "port": 70000}),
                                ("card/remove", {"card_id": f"{NAS}|x", "ha": "everything"}),
                                ("thresholds/set", {"card_id": f"{NAS}|x", "values": {"a": -1}})):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, command, **fields)
            assert err.value.code == "invalid_format", command
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "card/hidden", card_id="nope|x", hidden=True)
        assert err.value.code == "not_found"
    run(tmp_path, body, monkeypatch)


# ── writes ────────────────────────────────────────────────────────────────


def _card(result, entry_id, text):
    data = _machines(result)[entry_id]["data"]
    for item in data["items"]:
        if text in json.dumps(item.get("drive") or item.get("pool") or {}):
            return item["card_id"]
    for card in data["filesystem_cards"]:
        if text in json.dumps(card):
            return card["card_id"]
    raise AssertionError(text)


def test_hide_order_and_names(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        cards = await H.call(hass, conn, "cards")
        drive = _card(cards, NAS, "MOCK-SATA")
        fs = _card(cards, NAS, "/srv/backup")
        assert (await H.call(hass, conn, "card/hidden", card_id=drive, hidden=True))["hidden_cards"] == [drive]
        order = await H.call(hass, conn, "cards/order", order=[fs, drive, "gone|x"])
        assert order["order"] == [fs, drive]
        # A drive's name is its device's name in Home Assistant.
        named = await H.call(hass, conn, "card/name", card_id=drive, name="  Big   disk ")
        assert named == {"id": drive, "name": "Big disk", "home_assistant": "saved"}
        devices = dr.async_entries_for_config_entry(dr.async_get(hass), NAS)
        assert "Big disk" in [d.name_by_user for d in devices]
        # A filesystem card's name is the panel's.
        assert (await H.call(hass, conn, "card/name", card_id=fs, name="Backups"))["home_assistant"] is None
        await H.call(hass, conn, "machine/name", entry_id=NAS, name="Basement")
        state = (await H.call(hass, conn, "cards"))["state"]
        assert state["hidden"] == [drive] and state["names"] == {fs: "Backups"}
        assert state["host_names"] == {NAS: "Basement"}
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "machine/name", entry_id=HAOS, name="Mine")
        assert err.value.code == "fixed_name"
    run(tmp_path, body, monkeypatch)


def test_trash_page_disable_delete_and_entry(tmp_path, monkeypatch):
    removed_entries = []

    async def remove(entry_id):
        removed_entries.append(entry_id)

    async def body(hass, board, conn):
        hass.config_entries.async_remove = remove
        package = _package_init()
        monkeypatch.setattr(H.panel_ws, "_package_check", None, raising=False)
        import sys
        monkeypatch.setattr(sys.modules["custom_components.smart_sniffer"],
                            "async_remove_config_entry_device",
                            package.async_remove_config_entry_device, raising=False)
        cards = await H.call(hass, conn, "cards")
        haos_card = _card(cards, HAOS, "MOCK")
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "card/remove", card_id=haos_card)
        assert err.value.code == "cannot_remove"
        drive = _card(cards, NAS, "Barracuda")
        mode = _machines(cards)[NAS]["ha"]["removal"][drive]
        assert mode == "disable"
        answer = await H.call(hass, conn, "card/remove", card_id=drive, ha="disable")
        assert answer["home_assistant"] == "disabled" and answer["host_removed"] is False
        device = dr.async_get(hass).async_get(
            _machines(cards)[NAS]["ha"]["target"]["devices"][drive.split("|", 1)[1]])
        assert device.disabled_by is not None
        cards = await H.call(hass, conn, "cards")
        assert drive not in json.dumps(_machines(cards)[NAS]["data"]["items"])
        # The machine's last card with its entry.
        mac_card = _card(cards, MAC, "MOCK")
        assert _machines(cards)[MAC]["ha"]["removal"][mac_card] == "entry"
        answer = await H.call(hass, conn, "card/remove", card_id=mac_card, ha="entry")
        assert answer["host_removed"] and answer["home_assistant"] == "deleted"
        assert removed_entries == [MAC]
    run(tmp_path, body, monkeypatch)


def test_thresholds_are_written_to_the_entry_and_judged_at_once(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        cards = await H.call(hass, conn, "cards")
        drive = next(i for i in _machines(cards)[NAS]["data"]["items"]
                     if i.get("verdict") and i["verdict"]["state"] == "YES"
                     and "Reallocated" in json.dumps(i["verdict"]["reasons"]))
        got = await H.call(hass, conn, "thresholds/get", card_id=drive["card_id"])
        assert got["source"] == "integration"
        flagged = [lab["label"] for lab in got["labels"] if lab["flagged"]]
        assert "Reallocated Sector Count" in flagged
        answer = await H.call(hass, conn, "thresholds/accept", card_id=drive["card_id"])
        assert not [lab for lab in answer["labels"] if lab["flagged"]]
        entry = hass.config_entries.async_get_entry(NAS)
        assert "Reallocated Sector Count" in entry.data["thresholds"][drive["id"]]
        machine = await H.call(hass, conn, "machine", entry_id=NAS)
        again = next(i for i in machine["machine"]["data"]["items"] if i["card_id"] == drive["card_id"])
        assert again["verdict"]["state"] != "YES"
        # A value equal to the default is not stored.
        await H.call(hass, conn, "thresholds/set", card_id=drive["card_id"],
                     values={lab["label"]: lab["default"] for lab in got["labels"]})
        assert drive["id"] not in (hass.config_entries.async_get_entry(NAS).data.get("thresholds") or {})
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "thresholds/set", card_id=drive["card_id"], values={"Nope": 1})
        assert err.value.code == "bad_request"
    run(tmp_path, body, monkeypatch)


def test_card_file_and_agentic(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        cards = await H.call(hass, conn, "cards")
        drive = _card(cards, NAS, "MOCK-SATA")
        file = await H.call(hass, conn, "card/file", card_id=drive)
        doc = json.loads(file["text"])
        assert doc["format"] == "smart-sniffer-replica" and file["name"].endswith(".json")
        pool = next(i["card_id"] for i in _machines(cards)[NAS]["data"]["items"] if i["type"] == "pool")
        assert json.loads((await H.call(hass, conn, "card/file", card_id=pool))["text"])["pool"]
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "card/file", card_id=f"{PI}|SER-NONE")
        assert err.value.code == "not_answering"
        agentic = await H.call(hass, conn, "agentic")
        document = json.loads(agentic["text"])
        assert document["schema"] == "smart-sniffer-agentic-access"
        assert document["counts"]["agents"] == 4 and agentic["down"] == 2
        kinds = {a["id"]: a["kind"] for a in document["agents"]}
        assert kinds[HAOS] == "haos" and kinds[NAS] == "field"
    run(tmp_path, body, monkeypatch)


def test_note_dismissed_for_every_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: {})

    async def body(hass, board, conn):
        hass.config.components.add("hassio")
        assert (await H.call(hass, conn, "info"))["app_note"] is True
        await H.call(hass, conn, "note/dismiss", note="app_hint")
        other = H.FakeConnection(hass)
        assert (await H.call(hass, other, "info"))["app_note"] is False
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])


def test_agent_line_and_app_note_conditions(tmp_path, monkeypatch):
    app = {"value": None}
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: app["value"])

    async def body(hass, board, conn):
        info = await H.call(hass, conn, "info")
        assert info["agent_line"] is None and info["app_note"] is False  # Container or Core
        hass.config.components.add("hassio")
        app["value"] = {"slug": "a0d7b954_smart_sniffer_agent", "state": "started"}
        info = await H.call(hass, conn, "info")
        assert info["agent_line"]["addr"] == "a0d7b954-smart-sniffer-agent:9099"
        assert info["app_url"].endswith("/a0d7b954_smart_sniffer_agent/info")
        app["value"] = {}
        info = await H.call(hass, conn, "info")
        assert info["agent_line"] is None
        assert info["app_note"] is False  # its agent is still a machine
    run(tmp_path, body, monkeypatch)


def test_smart_sniffer_info_for_the_app(tmp_path, monkeypatch):
    async def call_info(hass, conn, **fields):
        handler = H.panel_ws.ws_smart_sniffer_info
        msg = {"id": 7, "type": "smart_sniffer/info", **fields}
        msg = handler._ws_schema(msg) if handler._ws_schema else msg
        handler(hass, conn, msg)
        return await conn.results[7] if 7 in conn.results else conn.sent[-1]

    async def body(hass, board, conn):
        result = await call_info(hass, conn)
        assert result == {"version": H.panel_data.VERSION, "panel": False,
                          "panel_path": "/smart-sniffer", "haos_entry": True,
                          "app_import": 1}
        hass.data["smart_sniffer_panel_setup"] = True
        assert (await call_info(hass, H.FakeConnection(hass)))["panel"] is True
        viewer = H.FakeConnection(hass, admin=False)
        with pytest.raises(Exception):
            H.panel_ws.ws_smart_sniffer_info(hass, viewer, {"id": 1, "type": "smart_sniffer/info"})
    run(tmp_path, body, monkeypatch)

    async def body2(hass, board, conn):
        assert (await call_info(hass, conn))["haos_entry"] is False
        assert (await call_info(hass, H.FakeConnection(hass), host="10.0.10.20", port=9099))["haos_entry"] is True
    run(tmp_path, body2, monkeypatch, specs=[H.BOARD[1]])


def test_haos_entry_found_by_agent_id_first(tmp_path, monkeypatch):
    specs = [{**H.BOARD[1], "data": {"agent_hostname": "nas-01", "agent_id": "abcd1234-app"}}]

    async def body(hass, board, conn):
        assert H.panel_ws.haos_entry(hass, agent_id="abcd1234-app").entry_id == NAS
        assert H.panel_ws.haos_entry(hass, agent_id="other000-app") is None
    run(tmp_path, body, monkeypatch, specs)


def test_password_runs_the_entrys_reauth(tmp_path, monkeypatch):
    seen = []

    async def reauth(hass, entry, password):
        seen.append((entry.entry_id, password))
        return {"good": "ok", "bad": "invalid_auth"}.get(password, "cannot_connect")

    monkeypatch.setattr(H.panel_ws, "_reauth", reauth)

    async def body(hass, board, conn):
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "machine/password", entry_id=PI, password="bad")
        assert err.value.code == "password_refused"
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "machine/password", entry_id=PI, password="down")
        assert err.value.code == "not_answering"
        answer = await H.call(hass, conn, "machine/password", entry_id=PI, password="good")
        assert "good" not in json.dumps(answer)
        assert seen[-1] == (PI, "good")
    run(tmp_path, body, monkeypatch)


# ── live updates ──────────────────────────────────────────────────────────


def test_subscription_pushes_and_lets_go(tmp_path, monkeypatch):
    monkeypatch.setattr(H.panel_ws, "PUSH_DELAY", 0.01)

    async def body(hass, board, conn):
        await H.call(hass, conn, "subscribe")
        assert len(conn.subscriptions) == 1
        board.coordinator(NAS).last_update_success = False
        async_dispatcher_send(hass, H.panel_data.SIGNAL_PANEL_UPDATE, NAS)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if conn.events:
                break
        machine = next(e for e in conn.events if e["type"] == "machine")["machine"]
        assert machine["host"]["id"] == NAS and machine["data"]["status"] == "unreachable"
        # A Store change: the state and every machine again.
        other = H.FakeConnection(hass)
        await H.call(hass, other, "card/hidden", card_id=_card(await H.call(hass, other, "cards"),
                                                               NAS, "/srv/backup"), hidden=True)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(e["type"] == "state" for e in conn.events):
                break
        assert any(e["type"] == "state" for e in conn.events)
        # An entry deleted: the machine goes.
        entry = hass.config_entries._entries.pop(MAC)
        async_dispatcher_send(hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, entry)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(e["type"] == "machine_removed" for e in conn.events):
                break
        assert {"type": "machine_removed", "entry_id": MAC} in conn.events
        # Unsubscribed (or the socket closed): nothing listens any more.
        for unsub in conn.subscriptions.values():
            unsub()
        count = len(conn.events)
        async_dispatcher_send(hass, H.panel_data.SIGNAL_PANEL_UPDATE, NAS)
        await asyncio.sleep(0.1)
        assert len(conn.events) == count
    run(tmp_path, body, monkeypatch)


# ── Stores ────────────────────────────────────────────────────────────────


def test_last_view_is_kept_and_shown_stale_next_run(tmp_path, monkeypatch):
    async def first(hass, board, conn):
        await H.call(hass, conn, "cards")
        pd = H.panel_data.panel_data(hass)
        await pd.views._store(NAS).async_save(pd.views.get(NAS))

    run(tmp_path, first, monkeypatch, specs=[H.BOARD[1]])
    # Next run: the entry is not loaded (agent down at start, setup retry).
    spec = {**H.BOARD[1], "reauth": False}

    async def second(hass, board, conn):
        hass.data["smart_sniffer"].pop(NAS)
        H.panel_data.panel_data(hass).read_at.pop(NAS, None)
        machine = _machines(await H.call(hass, conn, "cards"))[NAS]
        assert machine["data"]["stale"] is True
        assert machine["data"]["status"] == "unreachable"
        assert len(machine["data"]["items"]) == 4
        assert machine["data"]["last_answer"]
    run(tmp_path, second, monkeypatch, specs=[spec])


def test_panel_store_newer_version_is_read_only_and_kept(tmp_path, monkeypatch):
    storage = Path(tmp_path, ".storage")
    storage.mkdir(parents=True, exist_ok=True)
    newer = {"version": 2, "minor_version": 1, "key": "smart_sniffer.panel",
             "data": {"order": ["x"]}}
    (storage / "smart_sniffer.panel").write_text(json.dumps(newer))

    async def body(hass, board, conn):
        info = await H.call(hass, conn, "info")
        assert info["read_only"] is True
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "note/dismiss", note="app_hint")
        assert err.value.code == "panel_state_newer"
        await hass.async_block_till_done()
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])
    assert json.loads((storage / "smart_sniffer.panel").read_text()) == newer


def test_panel_store_older_minor_is_migrated_and_saved(tmp_path, monkeypatch):
    storage = Path(tmp_path, ".storage")
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "smart_sniffer.panel").write_text(json.dumps(
        {"version": 1, "minor_version": 0, "key": "smart_sniffer.panel",
         "data": {"machine_names": {NAS: "Old"}, "bogus": 1}}))

    async def body(hass, board, conn):
        state = (await H.call(hass, conn, "cards"))["state"]
        assert state["host_names"] == {NAS: "Old"} and state["read_only"] is False
        await H.call(hass, conn, "note/dismiss", note="app_hint")
        pd = H.panel_data.panel_data(hass)
        await pd.state._store.async_save(pd.state.data)
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])
    saved = json.loads((storage / "smart_sniffer.panel").read_text())
    assert saved["minor_version"] == 1 and saved["data"]["notes"] == {"app_hint": True}
    assert "bogus" not in saved["data"]


def test_corrupt_panel_store_starts_empty_and_keeps_the_bad_file(tmp_path, monkeypatch):
    """Home Assistant moves an unreadable Store file aside (".corrupt.<time>")
    and starts it empty; the panel goes on with empty settings."""
    storage = Path(tmp_path, ".storage")
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "smart_sniffer.panel").write_text("{not json")

    async def body(hass, board, conn):
        info = await H.call(hass, conn, "info")
        assert len((await H.call(hass, conn, "cards"))["machines"]) == 1
        if not info["read_only"]:
            await H.call(hass, conn, "note/dismiss", note="app_hint")
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])
    kept = [p for p in storage.iterdir() if p.name.startswith("smart_sniffer.panel")]
    assert any(p.read_text() == "{not json" for p in kept)


def test_entry_removal_forgets_the_panels_state(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        cards = await H.call(hass, conn, "cards")
        fs = _card(cards, NAS, "/srv/backup")
        await H.call(hass, conn, "card/hidden", card_id=fs, hidden=True)
        await H.call(hass, conn, "machine/name", entry_id=NAS, name="Gone soon")
        package = _package_init()
        entry = hass.config_entries.async_get_entry(NAS)
        await package.async_remove_entry(hass, entry)
        state = H.panel_data.panel_data(hass).state.data
        assert state["hidden"] == {} and state["machine_names"] == {}
        assert H.panel_data.panel_data(hass).views.get(NAS) is None
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])


# ── Registration ──────────────────────────────────────────────────────────


class _Http:
    def __init__(self, new_api):
        self.calls = []
        self.views = []
        if new_api:
            async def async_register_static_paths(configs):
                self.calls.append(("new", [(c.url_path, c.path, c.cache_headers) for c in configs]))
            self.async_register_static_paths = async_register_static_paths
        else:
            def register_static_path(url, path, cache_headers=True):
                self.calls.append(("old", [(url, path, cache_headers)]))
            self.register_static_path = register_static_path

    def register_view(self, view):
        self.views.append(view.url)


def _setup_mod():
    import importlib
    return importlib.import_module("custom_components.smart_sniffer.panel_setup")


@pytest.mark.parametrize("new_api", [True, False])
def test_panel_registered_once_per_run(tmp_path, monkeypatch, new_api):
    from homeassistant.components import panel_custom, websocket_api

    panels = []
    commands = []

    async def register_panel(hass, **kwargs):
        panels.append(kwargs)

    monkeypatch.setattr(panel_custom, "async_register_panel", register_panel)
    monkeypatch.setattr(websocket_api, "async_register_command",
                        lambda hass, handler, *a: commands.append(handler._ws_command))
    if not new_api:
        import homeassistant.components.http as http_mod
        monkeypatch.delattr(http_mod, "StaticPathConfig", raising=False)
    setup = _setup_mod()

    async def body(hass, board, conn):
        hass.http = _Http(new_api and hasattr(__import__("homeassistant.components.http",
                                                          fromlist=["x"]), "StaticPathConfig"))
        package = _package_init()
        assert await package.async_setup(hass, {}) is True
        assert await package.async_setup(hass, {}) is True  # a reload: nothing again
        assert await setup.async_setup_panel(hass) is True
        assert len(panels) == 1
        panel = panels[0]
        assert panel["frontend_url_path"] == "smart-sniffer"
        assert panel["require_admin"] is True and panel["sidebar_title"] == "SMART Sniffer"
        assert panel["sidebar_icon"] == "mdi:harddisk"
        # The version plus a digest of the page's files: a changed file is
        # never served from a cache under the same version.
        token = panel["config"]["version"]
        assert re.fullmatch(re.escape(H.panel_data.VERSION) + r"\+[0-9a-f]{8}", token), token
        assert panel["module_url"] == f"/smart_sniffer_static/panel.js?v={token}"
        assert token == setup.asset_token()
        assert len(hass.http.calls) == 1
        url, path, _cache = hass.http.calls[0][1][0]
        assert url == "/smart_sniffer_static" and path.endswith("smart_sniffer/panel")
        assert commands.count("smart_sniffer/info") == 1
        # The panel's 30, info, and the App handover's 4 (2 for the App, 2
        # for the panel's report line).
        assert len(commands) == 35
        assert commands.count("smart_sniffer/app/import") == 1
        assert hass.http.views == ["/api/smart_sniffer/replica/upload",
                                   "/api/smart_sniffer/agentic.json"]
    run(tmp_path, body, monkeypatch, specs=[])


def test_a_failing_panel_never_stops_setup(tmp_path, monkeypatch):
    from homeassistant.components import panel_custom, websocket_api

    commands = []

    async def broken(hass, **kwargs):
        raise RuntimeError("frontend gone")

    monkeypatch.setattr(panel_custom, "async_register_panel", broken)
    monkeypatch.setattr(websocket_api, "async_register_command",
                        lambda hass, handler, *a: commands.append(handler._ws_command))
    setup = _setup_mod()

    async def body(hass, board, conn):
        hass.http = _Http(True)
        package = _package_init()
        assert await package.async_setup(hass, {}) is True
        assert hass.data[setup.PANEL_SETUP] is False
        # The App can still ask, and hears the panel is not there.
        assert "smart_sniffer/info" in commands
    run(tmp_path, body, monkeypatch, specs=[])


def test_static_folder_holds_only_the_page():
    panel = Path(_setup_mod().PANEL_DIR)
    names = sorted(p.name for p in panel.rglob("*") if p.is_file())
    assert names == ["alpine.min.js", "cc.css", "cc.js", "index.html", "mark.png",
                     "panel.js", "wordmark.png"]
    assert not list(panel.glob("*.py"))


def test_alpine_is_the_pinned_release():
    import hashlib
    data = (Path(_setup_mod().PANEL_DIR) / "alpine.min.js").read_bytes()
    assert hashlib.md5(data).hexdigest() == "37bbf94201a78634b2780338ce1ba9d9"  # 3.17.4
    assert b'version:"3.17.4"' in data


def test_panel_modules_never_use_the_deprecated_device_lookup():
    root = Path(_setup_mod().PANEL_DIR).parent
    for name in ("panel_data.py", "panel_ws.py", "panel_net.py", "panel_setup.py",
                 "panel_state.py", "panel_view.py"):
        assert "async_get_device(" not in (root / name).read_text(), name


def test_setup_entry_links_the_coordinators_to_the_panel(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        package = _package_init()
        entry = hass.config_entries.async_get_entry(NAS)
        coord = H.FakeCoordinator({"x": {}})
        health = H.FakeCoordinator({})
        got = []
        from homeassistant.helpers.dispatcher import async_dispatcher_connect
        async_dispatcher_connect(hass, H.panel_data.SIGNAL_PANEL_UPDATE, got.append)
        unloads = []
        entry.async_on_unload = unloads.append
        package._panel_hooks(hass, entry, coord, health)
        await hass.async_block_till_done()
        assert got == [NAS] and len(unloads) == 2
        coord._listeners[0]()
        await hass.async_block_till_done()
        assert got == [NAS, NAS]
        assert H.panel_data.panel_data(hass).read_at[NAS]
        # A panel that throws never stops the entry.
        monkeypatch.setattr(package, "panel_data", lambda h: (_ for _ in ()).throw(RuntimeError()))
        package._panel_hooks(hass, entry, coord, health)
    run(tmp_path, body, monkeypatch)


def test_info_command_is_cheap_without_entries(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        cards = await H.call(hass, conn, "cards")
        assert cards["machines"] == [] and cards["info"]["version"] == H.panel_data.VERSION
    run(tmp_path, body, monkeypatch, specs=[])


_ = SimpleNamespace


def test_a_dev_build_of_the_apps_agent_is_haos(tmp_path, monkeypatch):
    """Round 4 (VM999): the dev App's agent, made from the Supervisor's
    discovery, shows as HAOS beside the real App's."""
    dev = {"id": "01DEVAPP000000000000000000", "host": "local-smart-sniffer-agent-dev", "port": 9099,
           "title": "SMART Sniffer (local-smart-sniffer-agent-dev)", "source": "hassio",
           "data": {}, "files": ["shipped.sata-ssd"]}
    lan = {**dev, "id": "01DEVAPP2000000000000000000"[:26], "host": "10.0.10.7", "port": 9199,
           "title": "SMART Sniffer (10.0.10.7)"}

    async def body(hass, board, conn):
        machines = _machines(await H.call(hass, conn, "cards"))
        for entry_id in (dev["id"], lan["id"]):
            host = machines[entry_id]["host"]
            assert (host["kind"], host["label"], host["fixed"]) == ("haos", "HAOS", True), host
        assert machines[NAS]["host"]["kind"] == "field"
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1], dev, lan])
