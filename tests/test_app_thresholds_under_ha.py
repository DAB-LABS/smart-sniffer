"""The SMART Sniffer App reads this box's thresholds (v1.0, #11).

smart_sniffer/app/thresholds answers the per-drive thresholds of the App's
own entry, in the shape the App keeps its own (card id "self|<serial>" ->
{label: int}) and by the agent's drive id. Gated exactly like app/import:
an admin that is a system user (the Supervisor), never a person's login.

A real Home Assistant core with the panel tests' machines (panel_harness).
Serials, labels and values are invented.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

pytest.importorskip("homeassistant")

from tests import panel_harness as H  # noqa: E402
from tests.test_app_import_under_ha import supervisor_conn, ws  # noqa: E402

panel_import = importlib.import_module("custom_components.smart_sniffer.panel_import")

HAOS = "01HAOS0000000000000000000A"
NAS = "01NAS00000000000000000000B"


def run(tmp_path, body, monkeypatch, specs=None):
    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, specs, monkeypatch)
            await body(hass, board)
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


def _drive_ids(board, entry_id):
    data = board.coordinator(entry_id).data
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _set(hass, entry_id, thresholds):
    entry = hass.config_entries.async_get_entry(entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "thresholds": thresholds})


async def ask(hass, conn=None, **fields):
    return await ws(hass, conn or supervisor_conn(hass), panel_import.ws_app_thresholds, **fields)


def test_the_apps_entry_thresholds_in_the_apps_shape(tmp_path, monkeypatch):
    async def body(hass, board):
        drives = _drive_ids(board, HAOS)
        (one, d1), (two, _d2), (three, d3) = sorted(drives.items())
        _set(hass, HAOS, {
            one: {"Reallocated Sector Count": 8, "junk": "x"},
            two: {},  # nothing overridden: left out
            three: {"NVMe Available Spare Warning": 10},
        })
        _set(hass, NAS, {"other-drive": {"Reallocated Sector Count": 99}})
        answer = await ask(hass)
        assert answer["entry_id"] == HAOS
        assert answer["drives"] == {one: {"Reallocated Sector Count": 8},
                                    three: {"NVMe Available Spare Warning": 10}}
        assert answer["thresholds"] == {
            f"self|{d1['serial']}": {"Reallocated Sector Count": 8},
            f"self|{d3['serial']}": {"NVMe Available Spare Warning": 10},
        }
        # Another machine's thresholds never come with it.
        assert "other-drive" not in answer["drives"]
    run(tmp_path, body, monkeypatch)


def test_matched_by_the_agent_id_the_app_reports(tmp_path, monkeypatch):
    specs = [dict(H.BOARD[0]), dict(H.BOARD[1])]
    specs[1] = {**specs[1], "data": {**specs[1]["data"], "agent_id": "b" * 32}}

    async def body(hass, board):
        drive = next(iter(_drive_ids(board, NAS)))
        _set(hass, NAS, {drive: {"Reallocated Sector Count": 3}})
        answer = await ask(hass, agent_id="b" * 32)
        assert answer["entry_id"] == NAS and answer["drives"] == {
            drive: {"Reallocated Sector Count": 3}}
    run(tmp_path, body, monkeypatch, specs)


def test_a_drive_not_read_yet_takes_its_serial_from_its_device(tmp_path, monkeypatch):
    async def body(hass, board):
        drives = _drive_ids(board, HAOS)
        drive_id, drive = next(iter(sorted(drives.items())))
        board.coordinator(HAOS).data = {}  # not read since the restart
        _set(hass, HAOS, {drive_id: {"Reallocated Sector Count": 8},
                          "gone-drive": {"Reallocated Sector Count": 1}})
        answer = await ask(hass)
        assert answer["thresholds"] == {
            f"self|{drive['serial']}": {"Reallocated Sector Count": 8},
            "self|id:gone-drive": {"Reallocated Sector Count": 1},
        }
    run(tmp_path, body, monkeypatch)


def test_no_entry_for_this_box(tmp_path, monkeypatch):
    async def body(hass, board):
        assert await ask(hass) == {"entry_id": None, "thresholds": {}, "drives": {}}
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[1]])


def test_refused_for_a_browser_admin_and_any_person(tmp_path, monkeypatch):
    async def body(hass, board):
        for conn in (H.FakeConnection(hass),  # a browser admin
                     supervisor_conn(hass, system_generated=False)):  # a person named Supervisor
            with pytest.raises(H.WsError) as err:
                await ask(hass, conn)
            assert err.value.code == "unauthorized"
        # A system user that is not an admin: refused by require_admin.
        with pytest.raises(Exception):
            await ask(hass, supervisor_conn(hass, is_admin=False))
    run(tmp_path, body, monkeypatch)


def test_registered_with_the_apps_commands_and_info_says_so(tmp_path, monkeypatch):
    assert panel_import.ws_app_thresholds in panel_import.APP_COMMANDS
    assert panel_import.ws_app_thresholds._ws_command == "smart_sniffer/app/thresholds"

    async def body(hass, board):
        panel_ws = importlib.import_module("custom_components.smart_sniffer.panel_ws")
        answer = await ws(hass, H.FakeConnection(hass), panel_ws.ws_smart_sniffer_info)
        assert answer["app_thresholds"] == 1
    run(tmp_path, body, monkeypatch)
