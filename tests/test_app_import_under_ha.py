"""The SMART Sniffer App hands its field machines over (panel_import.py).

A real Home Assistant core with the panel tests' machines (panel_harness):
HAOS, nas-01 (answering), studio-mac (not answering), backup-pi (wants its
password). The network and the config flow's import step are stood in for
(panel_import.async_health and async_create); the import step itself is
tested with the config flow tests' stand-ins at the end. Addresses, names
and passwords are invented.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: E402

from tests import panel_harness as H  # noqa: E402
from tests.test_device_names_under_registry import _make_entry  # noqa: E402

panel_import = importlib.import_module("custom_components.smart_sniffer.panel_import")

NAS = "01NAS00000000000000000000B"
HAOS = "01HAOS0000000000000000000A"
SECRET = "field-pass-7f3a"  # a field machine's password: never in any answer
SUP_ID = "supervisor-user-id"


def supervisor_conn(hass, **user):
    conn = H.FakeConnection(hass)
    conn.user = SimpleNamespace(**{"is_admin": True, "name": "Supervisor", "id": SUP_ID,
                                   "system_generated": True, **user})
    return conn


class Net:
    """Agents on the network: "address:port" -> /api/health; anything else
    does not answer. Creation as the import step does it (duplicates by
    address abort), the entry given made at once."""

    def __init__(self, hass, agents=None):
        self.hass = hass
        self.agents = dict(agents or {})
        self.asked: list[str] = []
        self.created: list[tuple[str, int, str]] = []
        self.fail: set[str] = set()
        self.seq = 0

    async def health(self, hass, address, port):
        self.asked.append(f"{address}:{port}")
        await asyncio.sleep(0)
        return self.agents.get(f"{address}:{port}")

    async def create(self, hass, address, port, password):
        key = f"{address}:{port}"
        if key in self.fail:
            raise RuntimeError("flow failed")
        if panel_import._by_address(hass, address, port) is not None:
            return "already_configured", None
        self.seq += 1
        entry_id = f"01NEW{self.seq:021d}"
        data = {"host": address, "port": port, "token": password, "scan_interval": 60}
        health = self.agents.get(key)
        if health and health.get("agent_id"):
            data["agent_id"] = health["agent_id"]
        entry = _make_entry(entry_id, f"SMART Sniffer ({address}:{port})", data, "import")
        hass.config_entries._entries[entry.entry_id] = entry
        self.created.append((address, port, password))
        return "created", entry_id


def run(tmp_path, body, monkeypatch, specs=None, agents=None, supervisor=True):
    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, specs, monkeypatch)
            net = Net(hass, agents)
            monkeypatch.setattr(panel_import, "async_health", net.health)
            monkeypatch.setattr(panel_import, "async_create", net.create)
            panel_import.async_listen(hass)
            if supervisor:
                hass.data["hassio_supervisor_user"] = SimpleNamespace(id=SUP_ID)
            await body(hass, board, net)
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


async def ws(hass, conn, handler, **fields):
    msg = {"id": next(H._ids), "type": handler._ws_command, **fields}
    if handler._ws_schema is not False:
        msg = handler._ws_schema(msg)
    fut = conn._future(msg["id"])
    handler(hass, conn, msg)
    return await asyncio.wait_for(fut, 10)


async def start(hass, items, conn=None):
    conn = conn or supervisor_conn(hass)
    return (await ws(hass, conn, panel_import.ws_app_import, items=items))["job_id"]


async def result(hass, job_id, conn=None):
    conn = conn or supervisor_conn(hass)
    for _ in range(200):
        answer = await ws(hass, conn, panel_import.ws_app_import_result, job_id=job_id)
        if answer["state"] != "running":
            return answer
        await asyncio.sleep(0.01)
    raise AssertionError("the job did not finish")


async def handover(hass, items):
    return (await result(hass, await start(hass, items)))["report"]


def field(address, port=9099, item_id=None, **more):
    return {"item_id": item_id or f"id-{address}-{port}", "kind": "field",
            "address": address, "port": port, **more}


async def cards(hass):
    return {m["host"]["id"]: m for m in (await H.call(hass, H.FakeConnection(hass), "cards"))["machines"]}


async def first_drive(hass, entry_id):
    data = (await cards(hass))[entry_id]["data"]
    item = next(i for i in data["items"] if i["type"] == "drive" and i["drive"].get("serial"))
    return item["card_id"].split("|", 1)[1], item


def new_entries(hass):
    return [e for e in hass.config_entries.async_entries("smart_sniffer")
            if e.entry_id.startswith("01NEW")]


# ── The handover ─────────────────────────────────────────────────────────


def test_creates_offline_machines_matches_known_ones_and_fills_gaps(tmp_path, monkeypatch):
    async def body(hass, board, net):
        key, _item = await first_drive(hass, NAS)
        items = [
            # nas-01 by its address: matched, never probed or made again.
            field("10.0.10.20", name="Basement NAS", password=SECRET,
                  cards={key: {"name": "Parity disk", "hidden": True,
                               "thresholds": {"Reallocated sectors": 50}}}),
            # Not answering: made anyway, shown offline.
            field("10.0.10.250", item_id="off", password=SECRET, name="Old box",
                  cards={"SER-OFF-1": {"hidden": True, "name": "Old disk"}}),
            {"item_id": "card_order", "kind": "order",
             "order": ["10.0.10.250:9099|SER-OFF-1", f"10.0.10.20:9099|{key}"]},
        ]
        report = await handover(hass, items)
        out = report["items"]
        assert out["id-10.0.10.20-9099"]["outcome"] == "matched"
        assert out["id-10.0.10.20-9099"]["entry_id"] == NAS
        assert out["off"]["outcome"] == "created_offline"
        assert out["card_order"]["outcome"] == "applied"
        assert sorted(report["done"]) == ["card_order", "id-10.0.10.20-9099", "off"]
        assert report["skipped"] == []
        assert "10.0.10.20:9099" not in net.asked  # matched without asking
        assert [(a, p) for a, p, _ in net.created] == [("10.0.10.250", 9099)]
        new = new_entries(hass)[0]
        assert new.data["token"] == SECRET  # the password in the entry only
        state = H.panel_data.panel_data(hass).state.data
        # nas-01 had no label here: the App's fills the gap.
        assert state["machine_names"] == {new.entry_id: "Old box", NAS: "Basement NAS"}
        assert state["hidden"][f"{NAS}|{key}"] is True
        assert state["hidden"][f"{new.entry_id}|SER-OFF-1"] is True
        assert state["order"][:2] == [f"{new.entry_id}|SER-OFF-1", f"{NAS}|{key}"]
        # The drive's name is its device's name; its threshold by drive id.
        target = (await cards(hass))[NAS]["ha"]["target"]
        device = dr.async_get(hass).async_get(target["devices"][key])
        assert device.name_by_user == "Parity disk"
        nas = hass.config_entries.async_get_entry(NAS)
        assert any(v == {"Reallocated sectors": 50} for v in nas.data["thresholds"].values())
        # The offline machine is on the page, offline, with its address.
        machine = (await cards(hass))[new.entry_id]
        assert machine["data"]["status"] in ("reading", "unreachable")
        assert machine["host"]["address"] == "10.0.10.250"
        # Its drive's name waits for its first read.
        stored = panel_import.app_import(hass).data
        assert stored["waiting"][new.entry_id]["names"] == {"SER-OFF-1": "Old disk"}
        info = await H.call(hass, H.FakeConnection(hass), "info")
        assert info["app_import"] == {"machines": 1, "already_here": 1, "names": 3, "hidden": 2,
                                      "thresholds": 1, "skipped": []}
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:2])


def test_matched_by_agent_id_before_the_address(tmp_path, monkeypatch):
    specs = [{**H.BOARD[1], "data": {"agent_hostname": "nas-01", "agent_id": "nas01-agent-id"}}]
    agents = {"192.168.7.9:9099": {"status": "ok", "agent_id": "nas01-agent-id",
                                   "hostname": "nas-01"}}

    async def body(hass, board, net):
        # The App knew the agent at another address; the agent's id says it
        # is nas-01, and nothing is made.
        report = await handover(hass, [field("192.168.7.9", item_id="a")])
        assert report["items"]["a"] == {"outcome": "matched", "entry_id": NAS,
                                        "counts": {"names": 0, "hidden": 0, "thresholds": 0}}
        assert net.created == []
        # The agent_id the App sends is enough on its own.
        report = await handover(hass, [field("nas.lan", item_id="b", agent_id="nas01-agent-id")])
        assert report["items"]["b"]["entry_id"] == NAS and "nas.lan:9099" not in net.asked
    run(tmp_path, body, monkeypatch, specs=specs, agents=agents)


def test_a_password_goes_only_to_a_machine_the_apps_record_names(tmp_path, monkeypatch):
    specs = [{**H.BOARD[1], "data": {"agent_hostname": "nas-01", "agent_id": "nas01-agent-id"}}]
    # Something at the App's address answers health with nas-01's agent_id
    # (health needs no password; agent ids are in mDNS).
    agents = {"192.168.7.9:9099": {"status": "ok", "agent_id": "nas01-agent-id",
                                   "hostname": "nas-01"}}

    async def body(hass, board, net):
        nas = hass.config_entries.async_get_entry(NAS)
        assert not nas.data.get("token")
        report = await handover(hass, [field("192.168.7.9", item_id="a", password=SECRET)])
        assert report["items"]["a"]["outcome"] == "matched"
        assert report["items"]["a"]["entry_id"] == NAS
        assert not nas.data.get("token")  # matched by the answer only: no password
        # The App's own record of the agent_id fills it.
        report = await handover(hass, [field("192.168.7.9", item_id="b", password=SECRET,
                                             agent_id="nas01-agent-id")])
        assert report["items"]["b"]["entry_id"] == NAS
        assert nas.data["token"] == SECRET
    run(tmp_path, body, monkeypatch, specs=specs, agents=agents)


def test_a_password_goes_to_a_machine_matched_by_its_address(tmp_path, monkeypatch):
    agents = {"10.0.10.20:9099": {"status": "ok", "agent_id": "someone-else", "hostname": "x"}}

    async def body(hass, board, net):
        report = await handover(hass, [field("10.0.10.20", item_id="a", password=SECRET)])
        assert report["items"]["a"]["entry_id"] == NAS
        assert hass.config_entries.async_get_entry(NAS).data["token"] == SECRET
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:2], agents=agents)


def test_the_apps_own_agent_is_haos(tmp_path, monkeypatch):
    async def body(hass, board, net):
        report = await handover(hass, [field("local-smart-sniffer-agent", item_id="x")])
        assert report["items"]["x"]["entry_id"] == HAOS and net.created == []
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1])


def test_integration_state_wins(tmp_path, monkeypatch):
    async def body(hass, board, net):
        key, _item = await first_drive(hass, NAS)
        conn = H.FakeConnection(hass)
        await H.call(hass, conn, "machine/name", entry_id=NAS, name="nas mine")
        await H.call(hass, conn, "card/hidden", card_id=f"{NAS}|{key}", hidden=False)
        await H.call(hass, conn, "card/name", card_id=f"{NAS}|{key}", name="Mine")
        await H.call(hass, conn, "cards/order", order=[f"{HAOS}|x", f"{NAS}|{key}"])
        nas = hass.config_entries.async_get_entry(NAS)
        drive_id = (await first_drive(hass, NAS))[1]["id"]
        hass.config_entries.async_update_entry(
            nas, data={**nas.data, "thresholds": {drive_id: {"Reallocated sectors": 9}}})
        report = await handover(hass, [
            field("10.0.10.20", item_id="n", name="App name",
                  cards={key: {"name": "App", "hidden": True,
                               "thresholds": {"Reallocated sectors": 50}}}),
            {"item_id": "card_order", "kind": "order", "order": [f"10.0.10.20:9099|{key}"]}])
        assert report["items"]["n"]["counts"] == {"names": 0, "hidden": 0, "thresholds": 0}
        assert report["items"]["card_order"]["outcome"] == "kept"
        state = H.panel_data.panel_data(hass).state.data
        assert state["machine_names"][NAS] == "nas mine"
        assert state["hidden"][f"{NAS}|{key}"] is False
        assert hass.config_entries.async_get_entry(NAS).data["thresholds"][drive_id] == {
            "Reallocated sectors": 9}
        target = (await cards(hass))[NAS]["ha"]["target"]
        assert dr.async_get(hass).async_get(target["devices"][key]).name_by_user == "Mine"
        line = (await H.call(hass, conn, "info"))["app_import"]
        assert line == {"machines": 0, "already_here": 1, "names": 0, "hidden": 0,
                        "thresholds": 0, "skipped": []}
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:2])


# ── Idempotent: retries, lost replies, deletions ─────────────────────────


def test_a_second_handover_changes_nothing(tmp_path, monkeypatch):
    async def body(hass, board, net):
        items = [field("10.0.10.250", item_id="off", name="Old box",
                       cards={"SER-1": {"hidden": True}}),
                 {"item_id": "replicas", "kind": "replicas"}]
        first = await handover(hass, items)
        assert first["items"]["replicas"]["outcome"] == "replicas_dropped"
        state_before = json.dumps(H.panel_data.panel_data(hass).state.data, sort_keys=True)
        second = await handover(hass, items)
        assert len(new_entries(hass)) == 1 and len(net.created) == 1
        assert second["items"]["off"]["already"] is True
        assert second["items"]["off"]["outcome"] == "created_offline"
        assert second["items"]["off"]["entry_id"] == first["items"]["off"]["entry_id"]
        assert second["items"]["replicas"]["already"] is True
        assert json.dumps(H.panel_data.panel_data(hass).state.data, sort_keys=True) == state_before
        assert net.asked.count("10.0.10.250:9099") == 1  # never probed again
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:2])


def test_a_lost_reply_and_a_retry_make_one_machine(tmp_path, monkeypatch):
    agents = {"10.0.10.77:9099": {"status": "ok", "hostname": "pi7", "agent_id": "pi7-id"}}

    async def body(hass, board, net):
        items = [field("10.0.10.77", item_id="pi")]
        # The App's first job: its answer and every poll are lost.
        await start(hass, items)
        # It starts again (a new job, the same items) before the first ended.
        job = await start(hass, items)
        report = (await result(hass, job))["report"]
        assert report["items"]["pi"]["outcome"] == "created"
        assert report["items"]["pi"].get("already") is True  # done by the first job
        assert len(new_entries(hass)) == 1 and len(net.created) == 1
        # A poll after Home Assistant restarted finds no job: the App asks again.
        conn = supervisor_conn(hass)
        with pytest.raises(H.WsError) as err:
            await ws(hass, conn, panel_import.ws_app_import_result, job_id="feedfacefeedface")
        assert err.value.code == "not_found"
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1], agents=agents)


def test_a_machine_deleted_after_the_handover_is_never_made_again(tmp_path, monkeypatch):
    async def body(hass, board, net):
        items = [field("10.0.10.250", item_id="off", cards={"SER-1": {"hidden": True}})]
        first = await handover(hass, items)
        entry_id = first["items"]["off"]["entry_id"]
        # The user deletes it (async_remove_entry tells the ledger).
        hass.config_entries._entries.pop(entry_id)
        await panel_import.async_forget_entry(hass, entry_id)
        # The App did not hear the answer and sends it all again.
        again = await handover(hass, items)
        assert again["items"]["off"]["already"] is True
        assert again["items"]["off"]["removed_at"]
        assert new_entries(hass) == [] and len(net.created) == 1
        stored = panel_import.app_import(hass).data
        assert stored["ledger"]["off"]["removed_at"] and entry_id not in stored["waiting"]
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1])


def test_a_failed_machine_is_skipped_reported_and_tried_again(tmp_path, monkeypatch):
    async def body(hass, board, net):
        items = [field("10.0.10.250", item_id="ok"), field("10.0.10.251", item_id="bad"),
                 field("bad host/x", item_id="nonsense")]
        net.fail.add("10.0.10.251:9099")
        first = await handover(hass, items)
        assert first["items"]["ok"]["outcome"] == "created_offline"
        assert first["items"]["bad"] == {"outcome": "skipped", "reason": "error"}
        assert first["items"]["nonsense"] == {"outcome": "skipped", "reason": "bad_address"}
        assert sorted(first["skipped"]) == ["bad", "nonsense"]
        line = (await H.call(hass, H.FakeConnection(hass), "info"))["app_import"]
        assert line["machines"] == 1
        assert [(s["item_id"], s["reason"]) for s in line["skipped"]] == [
            ("bad", "error"), ("nonsense", "bad_address")]
        # Fixed by the next run: made then, the first one never again.
        net.fail.clear()
        second = await handover(hass, items)
        assert second["items"]["ok"]["already"] is True
        assert second["items"]["bad"]["outcome"] == "created_offline"
        assert len(net.created) == 2
        # A machine that never works: the user lets it go from the panel.
        admin = H.FakeConnection(hass)
        await ws(hass, admin, panel_import.ws_import_discard, item_id="nonsense")
        third = await handover(hass, items)
        assert third["items"]["nonsense"] == {"outcome": "discarded", "already": True,
                                             "counts": {"names": 0, "hidden": 0, "thresholds": 0}}
        line = (await H.call(hass, admin, "info"))["app_import"]
        assert line["skipped"] == [] and line["machines"] == 2
        # Dismissed: gone for good, also after the App asks again.
        await ws(hass, admin, panel_import.ws_import_dismiss)
        assert (await H.call(hass, admin, "info"))["app_import"] is None
        await handover(hass, items)
        assert (await H.call(hass, admin, "info"))["app_import"] is None
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1])


def test_waiting_settings_are_filled_when_the_machine_is_read(tmp_path, monkeypatch):
    async def body(hass, board, net):
        report = await handover(hass, [field("10.0.10.250", item_id="off", hidden=True,
                                             cards={"SER-W1": {"name": "Spare",
                                                               "thresholds": {"Temperature": 60}}})])
        entry_id = report["items"]["off"]["entry_id"]
        stored = panel_import.app_import(hass).data
        assert stored["waiting"][entry_id]["host_hidden"] is True
        # The machine answers: its first read and its devices.
        agent = H.agent_data(["shipped.sata-hdd-healthy"])
        drive_id, drive = next((k, v) for k, v in agent.items() if not k.startswith("_"))
        drive["serial"] = "SER-W1"
        hass.data.setdefault("smart_sniffer", {})[entry_id] = {
            "coordinator": H.FakeCoordinator(agent),
            "health_coordinator": SimpleNamespace(data=H.health("spare-box"))}
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry_id, identifiers={("smart_sniffer", drive_id)},
            name="spare-box disk", serial_number="SER-W1")
        async_dispatcher_send(hass, H.panel_data.SIGNAL_PANEL_UPDATE, entry_id)
        for _ in range(50):
            await hass.async_block_till_done()
            if entry_id not in stored["waiting"]:
                break
            await asyncio.sleep(0.01)
        assert entry_id not in stored["waiting"]
        target = (await cards(hass))
        state = H.panel_data.panel_data(hass).state.data
        assert state["hidden"][f"{entry_id}|SER-W1"] is True
        device = next(iter(dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)))
        assert device.name_by_user == "Spare"
        assert hass.config_entries.async_get_entry(entry_id).data["thresholds"] == {
            drive_id: {"Temperature": 60}}
        assert stored["ledger"]["off"]["counts"] == {"names": 1, "hidden": 1, "thresholds": 1}
        assert target  # the page still draws
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1])


def test_this_boxs_settings_wait_for_its_machine(tmp_path, monkeypatch):
    async def body(hass, board, net):
        report = await handover(hass, [{"item_id": "self", "kind": "self",
                                        "cards": {"SER-H1": {"hidden": True}}}])
        assert report["items"]["self"]["outcome"] == "pending"
        board.add(H.BOARD[0])  # HAOS is added later (the Discovered card)
        async_dispatcher_send(hass, H.panel_data.SIGNAL_PANEL_UPDATE, HAOS)
        for _ in range(50):
            await hass.async_block_till_done()
            if panel_import.app_import(hass).data["waiting_app"] is None:
                break
            await asyncio.sleep(0.01)
        state = H.panel_data.panel_data(hass).state.data
        assert state["hidden"][f"{HAOS}|SER-H1"] is True
        row = panel_import.app_import(hass).data["ledger"]["self"]
        assert row["outcome"] == "applied" and row["entry_id"] == HAOS
    run(tmp_path, body, monkeypatch, specs=H.BOARD[1:2])


# ── Who may ask, and what never leaves ───────────────────────────────────


def test_only_an_admin_system_user(tmp_path, monkeypatch):
    """The App's two commands: require_admin, then a system user. Nothing
    reads the hassio integration's own data or files (HACS review)."""
    async def body(hass, board, net):
        items = [field("10.0.10.250", item_id="x")]
        refused = [
            H.FakeConnection(hass),  # a browser admin
            supervisor_conn(hass, system_generated=False),  # a person named Supervisor
        ]
        for conn in refused:
            with pytest.raises(H.WsError) as err:
                await start(hass, items, conn)
            assert err.value.code == "unauthorized"
            with pytest.raises(H.WsError) as err:
                await ws(hass, conn, panel_import.ws_app_import_result, job_id="x")
            assert err.value.code == "unauthorized"
        # A system user that is not an admin: refused by require_admin.
        for handler, fields in ((panel_import.ws_app_import, {"items": items}),
                                (panel_import.ws_app_import_result, {"job_id": "x"})):
            with pytest.raises(Exception):
                await ws(hass, supervisor_conn(hass, is_admin=False), handler, **fields)
        assert net.created == []
        # The panel's own commands are for admins, not for the App's job.
        viewer = H.FakeConnection(hass, admin=False)
        for handler, fields in ((panel_import.ws_import_dismiss, {}),
                                (panel_import.ws_import_discard, {"item_id": "x"})):
            with pytest.raises(Exception):
                await ws(hass, viewer, handler, **fields)
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:1])


def test_no_hassio_internals_are_read():
    src = (Path(panel_import.__file__)).read_text()
    assert "hassio_supervisor_user" not in src
    assert '".storage"' not in src and "'.storage'" not in src


def test_no_password_in_any_answer_store_or_document(tmp_path, monkeypatch):
    agents = {"10.0.10.77:9099": {"status": "ok", "hostname": "pi7"}}

    async def body(hass, board, net):
        items = [field("10.0.10.77", item_id="pi", password=SECRET),
                 field("10.0.10.250", item_id="off", password=SECRET),
                 field("10.0.10.20", item_id="nas", password=SECRET)]
        conn = supervisor_conn(hass)
        job = await ws(hass, conn, panel_import.ws_app_import, items=items)
        await result(hass, job["job_id"], conn)
        await handover(hass, items)
        admin = H.FakeConnection(hass)
        for command in ("cards", "info", "agentic"):
            conn.sent.append(await H.call(hass, admin, command))
        status, text, _headers = await H.panel_ws.async_agentic_http(
            hass, SimpleNamespace(is_admin=True), {})
        assert status == 200
        texts = [json.dumps(conn.sent), json.dumps(admin.sent), text]
        await hass.async_block_till_done()
        ai = panel_import.app_import(hass)
        await ai._store.async_save(ai.data)
        await H.panel_data.panel_data(hass).state._store.async_save(
            H.panel_data.panel_data(hass).state.data)
        texts += [p.read_text() for p in Path(tmp_path, ".storage").glob("smart_sniffer*")]
        assert any("ledger" in t for t in texts)
        for t in texts:
            assert SECRET not in t
        # It is in the entries, as their token: only the new ones and the one
        # that had none.
        tokens = {e.entry_id: e.data["token"] for e in hass.config_entries.async_entries("smart_sniffer")}
        assert sum(1 for v in tokens.values() if v == SECRET) == 3
    run(tmp_path, body, monkeypatch, specs=H.BOARD[:2], agents=agents)


def test_schema_refuses_bad_items(tmp_path, monkeypatch):
    async def body(hass, board, net):
        conn = supervisor_conn(hass)
        for items in ([], [{"item_id": "a", "kind": "field", "address": "x", "port": 70000}],
                      [{"item_id": "a", "kind": "other"}],
                      [field("10.0.0.1", cards={"k": {"thresholds": {"x": -1}}})]):
            with pytest.raises(Exception):
                await ws(hass, conn, panel_import.ws_app_import, items=items)
        with pytest.raises(H.WsError) as err:
            await start(hass, [field("10.0.0.1", item_id="a"), field("10.0.0.2", item_id="a")])
        assert err.value.code == "invalid_format"
    run(tmp_path, body, monkeypatch, specs=[])


# ── The config flow's import step, for the App ───────────────────────────


def test_import_step_makes_an_offline_machine_only_for_the_app():
    flows = importlib.import_module("tests.test_config_flow_under_ha")
    flow = flows._flow([], "import", {})
    result = flows._run(flow.async_step_import({"host": "10.0.10.250", "port": 9099,
                                                "token": SECRET, "from_app": True}))
    assert result["type"] == "create_entry"
    assert result["data"] == {"host": "10.0.10.250", "port": 9099, "token": SECRET,
                              "scan_interval": 60}
    assert flow.context["unique_id"] == "10.0.10.250:9099"
    # Without the App's flag, an agent that does not answer is not added.
    flow = flows._flow([], "import", {})
    result = flows._run(flow.async_step_import({"host": "10.0.10.250", "port": 9099}))
    assert result["reason"] == "cannot_connect"
    # Already an entry at that address: never a second one.
    existing = flows._entry("E1", "10.0.10.250:9099", "10.0.10.250")
    flow = flows._flow([existing], "import", {})
    result = flows._run(flow.async_step_import({"host": "10.0.10.250", "port": 9099,
                                                "from_app": True}))
    assert result["reason"] == "already_configured"
