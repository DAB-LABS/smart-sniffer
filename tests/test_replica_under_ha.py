"""The Replica machine under Home Assistant (v0.9).

A real core (config entries, device registry, Stores, dispatcher) with the
board of tests/panel_harness.py, and the Replica entry with the
integration's real ReplicaSource and coordinators (no HTTP). Every panel
command goes through its real decorators and schema.

Also: the entry kind everywhere an address is read (the grep test), the
Replica's config entry and options, the SMART Sniffer App's old Replica
entries removed at start, and a rollback to v0.8.5 with a Replica entry.
Skipped without Home Assistant.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

import aiohttp  # noqa: E402

from homeassistant.helpers import device_registry as dr  # noqa: E402

from tests import panel_harness as H  # noqa: E402
from tests.test_device_names_under_registry import _make_entry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "smart_sniffer"
DOMAIN = "smart_sniffer"
NAS = "01NAS00000000000000000000B"
HAOS = "01HAOS0000000000000000000A"
R = H.REPLICA_ID

replica = H.replica
engine = importlib.import_module("custom_components.smart_sniffer.replica_engine")
source = importlib.import_module("custom_components.smart_sniffer.source")
entity_plan = importlib.import_module("custom_components.smart_sniffer.entity_plan")
panel_replica = importlib.import_module("custom_components.smart_sniffer.panel_replica")
config_flow = importlib.import_module("custom_components.smart_sniffer.config_flow")


def run(tmp_path, body, monkeypatch, specs=None):
    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, [H.BOARD[0], H.BOARD[1]] if specs is None else specs,
                                  monkeypatch)
            H.patch_replica(hass, monkeypatch)
            await body(hass, board, H.FakeConnection(hass))
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


def _replica_machine(cards):
    return next((m for m in cards["machines"] if m["host"]["kind"] == "replica"), None)


def _device(hass, entry_id, identifier):
    registry = dr.async_get(hass)
    return next((d for d in dr.async_entries_for_config_entry(registry, entry_id)
                 if (DOMAIN, identifier) in d.identifiers), None)


def _user_dir(hass):
    return Path(replica.user_dir(hass))


# ── + > Replica ────────────────────────────────────────────────────────────


def test_the_library_lists_files_by_source(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        lib = await H.call(hass, conn, "replica/library")
        seeds = [f for f in lib["files"] if f["source"] == "seed"]
        assert seeds and all(f["valid"] for f in seeds)
        assert lib["folders"]["user"] == "ok"  # made on first use
        assert _user_dir(hass).is_dir()
        assert lib["caps"] == {"drives": 12, "pools": 4}
        assert lib["used"] == {"drives": 0, "pools": 0} and lib["entry_id"] is None
        info = await H.call(hass, conn, "info")
        assert info["replica"] is True
    run(tmp_path, body, monkeypatch)


def test_the_first_add_makes_the_replica_and_its_cards(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        assert replica.replica_entry(hass) is None
        added = await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-healthy", "seed.nvme-wear", "seed.zfs-pool-degraded",
            "seed.sata-hdd-healthy"])
        entry = replica.replica_entry(hass)
        assert entry is not None and added["entry_id"] == entry.entry_id
        assert entry.data["host"] == "replica.invalid" and entry.data["kind"] == "replica"
        machine = _replica_machine(await H.call(hass, conn, "cards"))
        host = machine["host"]
        assert (host["label"], host["fixed"], host["removable"], host["address"], host["port"]) \
            == ("Replica", True, True, "", None)
        items = machine["data"]["items"]
        assert len(items) == 4 and machine["data"]["status"] == "ok"
        names = [i["lab"]["name"] for i in items]
        assert names.count("SATA HDD, healthy") == 2 and "ZFS pool, degraded" in names
        states = {i["lab"]["name"]: (i["verdict"] or {}).get("state") for i in items}
        assert states["NVMe, worn"] == "MAYBE" and states["ZFS pool, degraded"] == "YES"
        # Its devices, by the namespaced ids.
        target = machine["ha"]["target"]
        assert set(target["devices"]) >= {i["card_id"].split("|", 1)[1] for i in items}
        # The coordinator reads the source: never an address.
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        assert coordinator.host == "Replica" and coordinator.port == 0
        assert hass.data[DOMAIN][entry.entry_id]["health_coordinator"].data["connected"]
        lib = await H.call(hass, conn, "replica/library")
        assert lib["used"] == {"drives": 3, "pools": 1}
    run(tmp_path, body, monkeypatch)


def test_past_the_caps_nothing_is_added(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=["seed.sata-hdd-healthy"] * 12)
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/add", file_ids=["seed.zfs-pool-healthy", "seed.nvme-wear"])
        assert err.value.code == "replica_cap"
        assert "12 drives and 4 pools" in err.value.message
        picks = replica.runtime(hass).picks_of(R).items
        assert len(picks) == 12
        await H.call(hass, conn, "replica/add", file_ids=["seed.zfs-pool-healthy"] * 4)
        with pytest.raises(H.WsError):
            await H.call(hass, conn, "replica/add", file_ids=["seed.zfs-pool-healthy"])
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/add", file_ids=["seed.nope"])
        assert err.value.code == "not_found"
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/add", file_ids=["seed.x"] * 17)
        assert err.value.code == "invalid_format"
    run(tmp_path, body, monkeypatch)


def test_remove_a_card_then_the_last_one_takes_the_replica(tmp_path, monkeypatch):
    removed = []

    async def body(hass, board, conn):
        async def remove_entry(entry_id):
            removed.append(entry_id)
            await replica.async_forget_entry(hass, entry_id)
            hass.config_entries._entries.pop(entry_id, None)
            hass.data[DOMAIN].pop(entry_id, None)
        monkeypatch.setattr(hass.config_entries, "async_remove", remove_entry)
        added = await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-healthy", "seed.zfs-pool-degraded"])
        drive_card, pool_card = (a["card_id"] for a in added["added"])
        await H.call(hass, conn, "card/hidden", card_id=drive_card, hidden=True)
        drive_id = replica.runtime(hass).picks_of(R).items[0]["drive_id"]
        assert _device(hass, R, drive_id) is not None
        # The trash on a Replica card: its pick goes, with its device.
        result = await H.call(hass, conn, "card/remove", card_id=drive_card)
        assert result == {"removed": drive_card, "host_removed": False}
        assert _device(hass, R, drive_id) is None
        state = (await H.call(hass, conn, "cards"))["state"]
        assert drive_card not in state["hidden"]
        assert [p["kind"] for p in replica.runtime(hass).picks_of(R).items] == ["pool"]
        # The file stays in the library.
        lib = await H.call(hass, conn, "replica/library")
        assert any(f["id"] == "seed.sata-hdd-healthy" for f in lib["files"])
        result = await H.call(hass, conn, "replica/remove", card_id=pool_card)
        assert result["host_removed"] is True and removed == [R]
        with pytest.raises(H.WsError):
            await H.call(hass, conn, "replica/remove", card_id=f"{NAS}|MOCK-SATA-0003")
    run(tmp_path, body, monkeypatch)


def test_the_picks_store_goes_with_the_entry(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=["seed.nvme-wear"])
        await hass.async_block_till_done()
        store = Path(tmp_path, ".storage", f"smart_sniffer.replica.{R}")
        assert store.exists()
        assert "MOCK-NVME" not in store.read_text()  # references, not copies
        package = importlib.import_module("tests.test_device_names_under_registry")._package_init()
        await package.async_remove_entry(hass, hass.config_entries.async_get_entry(R))
        assert not store.exists()
        assert _user_dir(hass).is_dir()  # the library stays
    run(tmp_path, body, monkeypatch)


def test_a_drive_with_the_same_serial_never_collides(tmp_path, monkeypatch):
    """NAS-01 reports MOCK-SATA-0003, the serial inside the reallocated seed
    file: the Replica's drive is its own device and card."""
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=["seed.sata-hdd-reallocated"])
        cards = await H.call(hass, conn, "cards")
        nas = next(m for m in cards["machines"] if m["host"]["id"] == NAS)
        rep = _replica_machine(cards)
        real = next(i for i in nas["data"]["items"] if i.get("drive", {}).get("serial") == "MOCK-SATA-0003")
        ours = rep["data"]["items"][0]
        assert ours["drive"]["serial"] != "MOCK-SATA-0003" and ours["id"] != real["id"]
        assert ours["card_id"] != real["card_id"]
        a = _device(hass, NAS, real["id"])
        b = _device(hass, R, ours["id"])
        assert a is not None and b is not None and a.id != b.id
        assert _device(hass, R, real["id"]) is None
    run(tmp_path, body, monkeypatch)


def test_save_as_replica_from_real_cards(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        saved = await H.call(hass, conn, "replica/save", card_id=f"{NAS}|MOCK-SATA-0003")
        entry = saved["saved"]
        assert entry["source"] == "user" and entry["valid"] and entry["kind"] == "ata"
        path = _user_dir(hass) / f"{entry['id'][5:]}.json"
        text = path.read_text()
        assert "MOCK-SATA-0003" not in text and '"REPLICA-' in text
        assert json.loads(text)["origin"]["type"] == "saved"
        pool = await H.call(hass, conn, "replica/save", card_id=f"{NAS}|pool:tank")
        assert pool["saved"]["kind"] == "zfs_pool"
        # The saved pool reads as the real one did.
        await H.call(hass, conn, "replica/add", file_ids=[pool["saved"]["id"], entry["id"]])
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        states = sorted((i["type"], i["verdict"]["state"]) for i in rep["data"]["items"])
        assert states == [("drive", "YES"), ("pool", "YES")]
        # Never from a Replica card, never an unsupported drive.
        with pytest.raises(H.WsError):
            await H.call(hass, conn, "replica/save", card_id=rep["data"]["items"][0]["card_id"])
        qemu = next(i for i in next(m for m in (await H.call(hass, conn, "cards"))["machines"]
                                    if m["host"]["id"] == NAS)["data"]["items"]
                    if (i.get("drive") or {}).get("model") == "QEMU HARDDISK")
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/save", card_id=qemu["card_id"])
        assert err.value.code == "unsupported_drive"
    run(tmp_path, body, monkeypatch)


class _Body:
    def __init__(self, data: bytes, piece: int = 4096):
        self.buf = io.BytesIO(data)
        self.piece = piece
        self.read_total = 0

    async def read(self, n=-1):
        chunk = self.buf.read(min(n, self.piece) if n > 0 else self.piece)
        self.read_total += len(chunk)
        return chunk


def test_upload_admins_only_checked_and_capped(tmp_path, monkeypatch):
    seed = json.loads((COMPONENT / "replicas" / "nvme-wear.json").read_text())

    async def body(hass, board, conn):
        admin, viewer = SimpleNamespace(is_admin=True), SimpleNamespace(is_admin=False)
        good = json.dumps({**seed, "name": "Uploaded NVMe"}).encode()
        status, out = await panel_replica.async_handle_upload(hass, viewer, _Body(good), len(good))
        assert status == 401
        status, out = await panel_replica.async_handle_upload(hass, None, _Body(good), len(good))
        assert status == 401
        status, out = await panel_replica.async_handle_upload(hass, admin, _Body(good), len(good))
        assert status == 200 and out["saved"]["id"] == "user.uploaded-nvme"
        doc = json.loads((_user_dir(hass) / "uploaded-nvme.json").read_text())
        assert doc["origin"] == {"type": "uploaded"}
        # Too large: refused by its length, or while reading, before all of it.
        big = b" " * (engine.MAX_REPLICA_BYTES + 1)
        status, out = await panel_replica.async_handle_upload(hass, admin, _Body(big), len(big))
        assert status == 413 and out["error"] == "too_large"
        body_ = _Body(b" " * (engine.MAX_REPLICA_BYTES * 3))
        status, out = await panel_replica.async_handle_upload(hass, admin, body_, None)
        assert status == 413 and body_.read_total <= engine.MAX_REPLICA_BYTES + 4096
        for bad in (b"{", b"\xff\xfe", json.dumps({**seed, "format": "x"}).encode(),
                    b"[" * 50000, json.dumps({**seed, "name": "../../x"}).encode()[:-1]):
            status, out = await panel_replica.async_handle_upload(hass, admin, _Body(bad), len(bad))
            assert status == 422 and out["error"] == "invalid_replica"
            assert out["message"] == "That file is not a replica file."
        # A path-like name is only a name.
        odd = json.dumps({**seed, "name": "../../../etc/passwd", "__proto__": {"x": 1}}).encode()
        status, out = await panel_replica.async_handle_upload(hass, admin, _Body(odd), len(odd))
        assert status == 200 and out["saved"]["id"] == "user.etc-passwd"
        assert sorted(p.name for p in _user_dir(hass).iterdir()) == ["etc-passwd.json",
                                                                     "uploaded-nvme.json"]
    run(tmp_path, body, monkeypatch)


def test_a_missing_file_card_and_a_hand_edit(tmp_path, monkeypatch):
    seed = json.loads((COMPONENT / "replicas" / "sata-hdd-healthy.json").read_text())
    failing = json.loads((COMPONENT / "replicas" / "sata-hdd-smart-failed.json").read_text())
    notes = []

    async def body(hass, board, conn):
        from homeassistant.components import persistent_notification as pn

        coordinator_mod = H.coordinator_mod
        monkeypatch.setattr(coordinator_mod, "pn_create",
                            lambda hass, message, title=None, notification_id=None: notes.append(title))
        user = _user_dir(hass)
        user.mkdir(parents=True, exist_ok=True)
        (user / "mine.json").write_text(json.dumps({**seed, "name": "Mine"}))
        (user / "gone.json").write_text(json.dumps({**seed, "name": "Gone soon"}))
        await H.call(hass, conn, "replica/add", file_ids=["user.mine", "user.gone"])
        assert notes == []  # the first read is the baseline
        # Hand edit: the file now reads failed; Refresh reads it again.
        (user / "mine.json").write_text(json.dumps({**failing, "name": "Mine"}))
        (user / "gone.json").unlink()
        await replica.async_refresh(hass, R)  # what Refresh does for the Replica
        assert any("needs attention" in (t or "") and t.startswith("🔴 Replica:") for t in notes)
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        missing = [i for i in rep["data"]["items"] if i.get("error") == "replica_missing"]
        assert len(missing) == 1 and missing[0]["lab"]["name"] == "Gone soon"
        assert missing[0]["lab"]["missing"] is True
        # It can be removed like any Replica card.
        await H.call(hass, conn, "replica/remove", card_id=missing[0]["card_id"])
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        assert not [i for i in rep["data"]["items"] if i.get("error")]
        del pn
    run(tmp_path, body, monkeypatch)


def _notes(monkeypatch):
    """Notifications raised and dismissed, as (kind, id, title)."""
    told: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(H.coordinator_mod, "pn_create",
                        lambda hass, message, title=None, notification_id=None:
                        told.append(("create", notification_id, title)))
    monkeypatch.setattr(H.coordinator_mod, "pn_dismiss",
                        lambda hass, notification_id: told.append(("dismiss", notification_id, None)))
    return told


def test_a_failing_pick_raises_its_notification_at_once(tmp_path, monkeypatch):
    """The Replica's drives and pools are picked to be looked at: a failing
    one tells on its first read, not only on a change (v0.9 bench)."""
    told = _notes(monkeypatch)

    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-healthy", "seed.sata-hdd-smart-failed", "seed.zfs-pool-degraded"])
        created = [(i, t) for k, i, t in told if k == "create"]
        drives = [(i, t) for i, t in created if i.startswith("smart_sniffer_attention_")]
        pools = [(i, t) for i, t in created if i.startswith("smart_sniffer_pool_")]
        assert len(drives) == 1 and drives[0][1].startswith("🔴 Replica:")
        assert "needs attention" in drives[0][1]
        assert len(pools) == 1
        # Read again: nothing new.
        before = len(told)
        await replica.async_refresh(hass, R)
        assert len(told) == before
        # A drive added later to the same Replica tells too (MAYBE).
        await H.call(hass, conn, "replica/add", file_ids=["seed.nvme-wear"])
        later = [t for k, i, t in told[before:] if k == "create"]
        assert len(later) == 1 and later[0].startswith("⚠️ Replica:")
        # A healthy pick never does.
        before = len(told)
        await H.call(hass, conn, "replica/add", file_ids=["seed.nvme-healthy"])
        assert [k for k, _, _ in told[before:] if k == "create"] == []
    run(tmp_path, body, monkeypatch)


def test_real_agents_keep_the_first_poll_baseline(tmp_path, monkeypatch):
    """The same failing drives and pool read from a real agent raise nothing
    on the first poll: the baseline rule is unchanged for agents."""
    told = _notes(monkeypatch)

    async def body(hass, board, conn):
        from homeassistant import config_entries  # noqa: PLC0415

        await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-smart-failed", "seed.nvme-wear", "seed.zfs-pool-degraded"])
        data = hass.data[DOMAIN][R]["coordinator"].data
        real = _make_entry("01REAL0000000000000000000E", "SMART Sniffer (10.0.0.9:9099)",
                           {"host": "10.0.0.9", "port": 9099, "token": "", "scan_interval": 60},
                           "user")
        hass.config_entries._entries[real.entry_id] = real
        config_entries.current_entry.set(real)
        agent = H.coordinator_mod.SmartSnifferCoordinator(hass, real, None, None)
        assert not agent._notifies_first_poll
        told.clear()
        for _ in range(2):
            await agent._handle_attention_notifications(data)
            agent._handle_pool_notifications(data.get("_pools"), [])
        assert [t for k, _, t in told if k == "create"] == []
    run(tmp_path, body, monkeypatch)


def test_agentic_counts_keep_the_replica_apart_and_match_the_header(tmp_path, monkeypatch):
    """VM999 bench: the header said "6 agents, 8 drives" while counts said 5
    and 6 and left out the Replica's pool and its attention. Real counts
    exclude the Replica, which is counted apart; the header uses the same
    numbers."""
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-smart-failed", "seed.nvme-wear", "seed.zfs-pool-degraded"])
        agentic = await H.call(hass, conn, "agentic")
        document = json.loads(agentic["text"])
        counts = document["counts"]
        real = [a for a in document["agents"] if a["kind"] != "replica"]
        rep = next(a for a in document["agents"] if a["kind"] == "replica")
        assert counts["agents"] == len(real) == 2
        assert counts["drives"] == sum(len(a.get("drives") or []) for a in real)
        assert counts["pools"] == sum(len(a.get("pools") or []) for a in real) == 1
        assert counts["replica_drives"] == len(rep["drives"]) == 2
        assert counts["replica_pools"] == len(rep["pools"]) == 1
        # The failed drive and the degraded pool are YES; the worn NVMe MAYBE.
        assert counts["replica_attention"] == 2 and counts["replica_maybe"] == 1
        assert counts["attention"] == sum(a["summary"]["attention"] for a in real)
        # The header's numbers are the document's.
        assert (agentic["agents"], agentic["drives"], agentic["pools"], agentic["down"]) == (
            counts["agents"], counts["drives"], counts["pools"], counts["unreachable_agents"])
        assert agentic["counts"] == counts
    run(tmp_path, body, monkeypatch)


def test_agentic_counts_without_a_replica(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        counts = json.loads((await H.call(hass, conn, "agentic"))["text"])["counts"]
        assert (counts["replica_drives"], counts["replica_pools"], counts["replica_attention"],
                counts["replica_maybe"]) == (0, 0, 0, 0)
    run(tmp_path, body, monkeypatch)


def test_the_drop_folder_is_read_only(tmp_path, monkeypatch):
    seed = json.loads((COMPONENT / "replicas" / "nvme-wear.json").read_text())

    async def body(hass, board, conn):
        share = tmp_path / "share"
        share.mkdir()
        (share / "lab.json").write_text(json.dumps({**seed, "name": "From the share"}))
        replica.runtime(hass).library.folders["share"] = str(share)
        lib = await H.call(hass, conn, "replica/library")
        assert [f["name"] for f in lib["files"] if f["source"] == "share"] == ["From the share"]
        await H.call(hass, conn, "replica/add", file_ids=["share.lab"])
        await H.call(hass, conn, "replica/save", card_id=f"{NAS}|MOCK-SATA-0003")
        assert [p.name for p in share.iterdir()] == ["lab.json"]
        file = await H.call(hass, conn, "replica/file", file_id="share.lab")
        assert file["name"] == "lab.json" and json.loads(file["text"])["name"] == "From the share"
    run(tmp_path, body, monkeypatch)


def test_every_replica_command_is_admin_only(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        viewer = H.FakeConnection(hass, admin=False)
        for command, fields in (("replica/library", {}), ("replica/add", {"file_ids": ["seed.nvme-wear"]}),
                                ("replica/remove", {"card_id": f"{R}|x"}),
                                ("replica/save", {"card_id": f"{NAS}|x"}),
                                ("replica/file", {"file_id": "seed.nvme-wear"})):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, viewer, command, **fields)
            assert err.value.code == "unauthorized", command
        assert replica.replica_entry(hass) is None
    run(tmp_path, body, monkeypatch)


# ── The Replica entry everywhere ───────────────────────────────────────────


def test_no_agent_address_or_password_entities_for_the_replica():
    data = {"x": {}}
    sensors = {s.kind for s in entity_plan.sensor_specs(data, "E", (), {}, replica=True)}
    binaries = {s.kind for s in entity_plan.binary_sensor_specs(data, "E", (), replica=True)}
    assert entity_plan.KIND_AGENT_IP not in sensors and entity_plan.KIND_AGENT_PORT not in sensors
    assert entity_plan.KIND_AUTH_ACTIVE not in binaries
    assert entity_plan.KIND_AGENT_STATUS in binaries
    assert entity_plan.KIND_AGENT_IP in {s.kind for s in entity_plan.sensor_specs(data, "E", (), {})}


def test_no_agent_update_repair_and_no_identity_learnt(tmp_path, monkeypatch):
    issues = []

    async def body(hass, board, conn):
        monkeypatch.setattr(H.coordinator_mod, "async_create_issue",
                            lambda *a, **kw: issues.append(kw.get("issue_id")))
        await H.call(hass, conn, "replica/add", file_ids=["seed.nvme-wear"])
        entry = replica.replica_entry(hass)
        assert issues == []  # version 0.8.0-mock would be "outdated" for an agent
        assert set(entry.data) == {"kind", "host", "port", "token", "scan_interval", "agent_id"}
        assert entry.data["agent_id"].startswith("replica-")
        diag = importlib.import_module("custom_components.smart_sniffer.diagnostics")
        out = await diag.async_get_config_entry_diagnostics(hass, entry)
        assert out["kind"] == "replica" and "host" not in out["config_entry"]
        assert len(out["drives"]) == 1
    run(tmp_path, body, monkeypatch)


def test_device_names_and_labels_say_replica(tmp_path):
    host_name = importlib.import_module("custom_components.smart_sniffer.host_name")
    data = source.replica_entry_data("replica-abc", 60)
    assert host_name.entry_host(data, source.REPLICA_TITLE) == "Replica"
    assert host_name.is_app_entry(data, source.REPLICA_TITLE) is False
    assert source.agent_address(data) is None and source.agent_port(data) is None
    assert source.agent_address({"host": "10.0.0.2"}) == "10.0.0.2"
    entry = _make_entry(R, source.REPLICA_TITLE, data, "import")
    naming = importlib.import_module("custom_components.smart_sniffer.device_naming")
    assert naming.agent_device_info(entry)["name"] == "SMART Sniffer (Replica)"


def test_the_import_step_makes_one_replica():
    from tests.test_config_flow_under_ha import _flow, _run

    flow = _flow([], "import")
    result = _run(flow.async_step_import({"kind": "replica"}))
    assert result["type"] == "create_entry" and result["title"] == "SMART Sniffer (Replica)"
    assert result["data"]["host"] == "replica.invalid" and result["data"]["port"] == 9099
    assert flow.context["unique_id"] == "replica"
    existing = SimpleNamespace(entry_id="r", unique_id="other", domain=DOMAIN, source="import",
                               state=None, title="SMART Sniffer (Replica)", data=result["data"])
    flow = _flow([existing], "import")
    result = _run(flow.async_step_import({"kind": "replica"}))
    assert result["type"] == "abort" and result["reason"] == "replica_exists"


def test_address_checks_never_match_the_replica():
    from tests.test_config_flow_under_ha import _flow, _run

    rep = SimpleNamespace(entry_id="r", unique_id="replica", domain=DOMAIN, source="import",
                          state=None, title="SMART Sniffer (Replica)",
                          data=source.replica_entry_data("replica-x", 60))
    flow = _flow([rep], "user", {"replica.invalid:9099": ("nas", ["S1"], "")},
                 dns={"replica.invalid": ["10.0.0.9"]})
    assert _run(flow._entry_at("replica.invalid", 9099)) is None
    result = _run(flow.async_step_user({"host": "replica.invalid", "port": 9099, "token": "",
                                        "scan_interval": 60}))
    assert result["type"] == "create_entry"


def test_the_replica_options_are_its_interval_and_force_update(tmp_path, monkeypatch):
    from homeassistant import config_entries

    if hasattr(config_entries, "report_usage"):  # a log line in an install, as in thresholds
        monkeypatch.setattr(config_entries, "report_usage", lambda *a, **kw: None)

    async def body(hass, board, conn):
        entry = _make_entry(R, source.REPLICA_TITLE, source.replica_entry_data("replica-x", 60),
                            "import")
        hass.config_entries._entries[R] = entry
        flow = config_flow.SmartSnifferOptionsFlow(entry)
        flow.hass, flow.handler, flow.flow_id = hass, R, "options"
        result = await flow.async_step_settings(None)
        assert {str(k) for k in result["data_schema"].schema} == {"scan_interval", "force_update"}
        result = await flow.async_step_settings({"scan_interval": 30, "force_update": True})
        assert result["type"] == "create_entry"
        assert entry.data["scan_interval"] == 30 and entry.data["host"] == "replica.invalid"
    run(tmp_path, body, monkeypatch, specs=[])


def test_every_address_read_knows_the_kind():
    """Outside source.py and config_flow.py, nothing reads an entry's host
    (CONF_HOST or "host") but through source.agent_address; config_flow.py
    skips the Replica in every loop over entries."""
    reads = re.compile(r"""(\.get\(\s*CONF_HOST|\[\s*CONF_HOST\s*\]|data\.get\(\s*["']host["']|"""
                       r"""data\[\s*["']host["']\s*\])""")
    found = []
    for path in sorted(COMPONENT.glob("*.py")):
        if path.name in ("source.py", "config_flow.py"):
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if reads.search(line):
                found.append(f"{path.name}:{n}: {line.strip()}")
    assert found == []
    flow = (COMPONENT / "config_flow.py").read_text()
    loops = flow.count("self._async_current_entries(include_ignore=False)") \
        + flow.count("entries = self._async_current_entries()")
    skips = flow.count("is_replica_data(entry.data)")
    assert loops == 4 and skips >= 6


# ── The SMART Sniffer App's old Replica machine ────────────────────────────

LEGACY = [
    {"id": "01OLDREP000000000000000000", "host": "a0d7b954-smart-sniffer-agent", "port": 9100,
     "title": "SMART Sniffer (a0d7b954-smart-sniffer-agent:9100)",
     "data": {"agent_hostname": "a0d7b954-smart-sniffer-agent", "agent_mock": True},
     "files": ["shipped.nvme-wear"]},
    {"id": "01OLDREP200000000000000000", "host": "local-smart-sniffer-agent", "port": 9100,
     "title": "SMART Sniffer (local-smart-sniffer-agent:9100)", "data": {}, "files": []},
    {"id": "01FIELD9100000000000000000", "host": "10.0.0.40", "port": 9100,
     "title": "SMART Sniffer (10.0.0.40:9100)", "data": {"agent_mock": True}, "files": []},
    {"id": "01HAOSMOCKFALSE00000000000", "host": "a0d7b954-smart-sniffer-agent", "port": 9100,
     "title": "SMART Sniffer (a0d7b954-smart-sniffer-agent:9100)",
     "data": {"agent_mock": False}, "files": []},
]


def test_old_app_replicas_are_removed_once_with_a_note(tmp_path, monkeypatch):
    told = []

    async def body(hass, board, conn):
        from homeassistant.components import persistent_notification as pn

        monkeypatch.setattr(pn, "async_create",
                            lambda hass, message, title=None, notification_id=None:
                            told.append((message, notification_id)))
        assert {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)
                if replica.is_legacy_app_replica(e)} == {LEGACY[0]["id"], LEGACY[1]["id"]}
        gone = await replica.async_remove_legacy(hass)
        assert gone == [LEGACY[0]["id"], LEGACY[1]["id"]]
        left = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
        assert left == {HAOS, LEGACY[2]["id"], LEGACY[3]["id"]}
        assert told == [("Replicas moved into the integration. Create them again from + > Replica.",
                         "smart_sniffer_replicas_moved")]
        state = (await H.call(hass, conn, "cards"))["state"]
        assert state["notes"]["replicas_moved"] is False  # the panel's line, shown
        # Made again by an old App: removed again, told no more.
        hass.config_entries._entries[LEGACY[0]["id"]] = board.entries[LEGACY[0]["id"]]
        assert await replica.async_remove_legacy(hass) == [LEGACY[0]["id"]]
        assert len(told) == 1
        await H.call(hass, conn, "note/dismiss", note="replicas_moved")
        assert (await H.call(hass, conn, "cards"))["state"]["notes"]["replicas_moved"] is True
    run(tmp_path, body, monkeypatch, specs=[H.BOARD[0], *LEGACY])


def test_an_old_app_replica_is_never_polled(tmp_path, monkeypatch):
    from homeassistant.exceptions import ConfigEntryNotReady

    async def body(hass, board, conn):
        package = importlib.import_module("tests.test_device_names_under_registry")._package_init()
        scheduled = []
        monkeypatch.setattr(replica, "async_schedule_legacy_removal", lambda h: scheduled.append(1))
        with pytest.raises(ConfigEntryNotReady):
            await package.async_setup_entry(hass, board.entries[LEGACY[0]["id"]])
        assert scheduled == [1]
    run(tmp_path, body, monkeypatch, specs=[LEGACY[0]])


# ── Rollback to v0.8.5 ─────────────────────────────────────────────────────


def _v085(tmp_path) -> str:
    """The v0.8.5 package from the repository's tag, as its own package."""
    if shutil.which("git") is None:
        pytest.skip("no git")
    try:
        tar = subprocess.run(["git", "-C", str(ROOT), "archive", "v0.8.5",
                              "custom_components/smart_sniffer"],
                             check=True, capture_output=True).stdout
    except subprocess.CalledProcessError:
        pytest.skip("no v0.8.5 tag here")
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        archive.extractall(tmp_path / "v085", filter="data") if sys.version_info >= (3, 12) \
            else archive.extractall(tmp_path / "v085")
    name = "ss_v085"
    package = ModuleType(name)
    package.__path__ = [str(tmp_path / "v085" / "custom_components" / "smart_sniffer")]
    sys.modules[name] = package
    return name


def test_v085_with_a_replica_entry_retries_quietly(tmp_path, caplog):
    name = _v085(tmp_path)
    old = importlib.import_module(f"{name}.coordinator")
    old_naming = importlib.import_module(f"{name}.device_naming")
    from homeassistant import config_entries
    from homeassistant.helpers.update_coordinator import UpdateFailed

    class _NoName:
        """replica.invalid never resolves (RFC 6761)."""

        def get(self, url, headers=None, timeout=None):
            assert url.startswith("http://replica.invalid:9099/")

            class _Resp:
                async def __aenter__(self):
                    raise aiohttp.ClientConnectionError(
                        "Cannot connect to host replica.invalid:9099 [Name or service not known]")

                async def __aexit__(self, *exc):
                    return False
            return _Resp()

    old.async_get_clientsession = lambda hass: _NoName()

    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            data = source.replica_entry_data("replica-abc", 60)
            entry = _make_entry(R, source.REPLICA_TITLE, data, "import")
            hass.config_entries._entries[R] = entry
            config_entries.current_entry.set(entry)
            coordinator = old.SmartSnifferCoordinator(hass, entry)  # no KeyError
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
            health = old.AgentHealthCoordinator(hass, entry)
            assert (await health._async_update_data())["connected"] is False
            assert old_naming.agent_device_info(entry)["name"]
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())
    assert "Traceback" not in caplog.text


# ── Library files deleted, readings edited (v0.9 round 3) ──────────────────


def test_delete_only_your_own_files_and_never_one_in_use(tmp_path, monkeypatch):
    seed = json.loads((COMPONENT / "replicas" / "sata-hdd-healthy.json").read_text())

    async def body(hass, board, conn):
        user = _user_dir(hass)
        user.mkdir(parents=True, exist_ok=True)
        (user / "mine.json").write_text(json.dumps({**seed, "name": "Mine"}))
        (user / "spare.json").write_text(json.dumps({**seed, "name": "Spare"}))
        (user / "broken.json").write_text("{not json")
        share = tmp_path / "share"
        share.mkdir()
        (share / "lab.json").write_text(json.dumps({**seed, "name": "From the share"}))
        replica.runtime(hass).library.folders["share"] = str(share)
        await H.call(hass, conn, "replica/add", file_ids=["user.mine"])
        # A picked file: its card goes first.
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/delete", file_id="user.mine")
        assert err.value.code == "replica_file_in_use"
        assert err.value.message == "Remove its replica drive first."
        assert (user / "mine.json").exists()
        # Shipped and /share files are never deleted.
        for file_id in ("seed.sata-hdd-healthy", "share.lab"):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "replica/delete", file_id=file_id)
            assert err.value.code == "not_deletable", file_id
        assert (share / "lab.json").exists()
        assert (COMPONENT / "replicas" / "sata-hdd-healthy.json").exists()
        # Your own, valid or not, unpicked: gone.
        done = await H.call(hass, conn, "replica/delete", file_id="user.spare")
        assert done["deleted"]["name"] == "Spare" and not (user / "spare.json").exists()
        await H.call(hass, conn, "replica/delete", file_id="user.broken")
        assert not (user / "broken.json").exists()
        lib = await H.call(hass, conn, "replica/library")
        assert {f["id"] for f in lib["files"] if f["source"] == "user"} == {"user.mine"}
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "replica/delete", file_id="user.spare")
        assert err.value.code == "not_found"
        # Never a path: ids only.
        for bad in ("user.../x", "../../etc/passwd"):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "replica/delete", file_id=bad)
            assert err.value.code == "not_found"
        # Once its card is removed, the file can go.
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        await H.call(hass, conn, "replica/remove", card_id=rep["data"]["items"][0]["card_id"])
        await H.call(hass, conn, "replica/delete", file_id="user.mine")
        assert not (user / "mine.json").exists()
    run(tmp_path, body, monkeypatch)


def _drive_item(machine, name):
    return next(i for i in machine["data"]["items"] if (i.get("lab") or {}).get("name") == name)


def test_edit_a_shipped_files_readings_makes_your_copy(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=["seed.sata-hdd-healthy", "seed.nvme-wear"])
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        sata = _drive_item(rep, "SATA HDD, healthy")
        assert sata["verdict"]["state"] == "NO"
        coordinator = hass.data[DOMAIN][R]["coordinator"]
        drive_id = sata["id"]
        done = await H.call(hass, conn, "replica/edit", card_id=sata["card_id"],
                            ata={"5": 40, "194": 51})
        assert done["switched"] is True
        assert done["file"]["name"] == "SATA HDD, healthy, edited"
        assert done["file"]["id"].startswith("user.")
        # The shipped file is unchanged; the copy has the new readings.
        shipped = json.loads((COMPONENT / "replicas" / "sata-hdd-healthy.json").read_text())
        attrs = {a["id"]: a for a in shipped["smart"]["ata_smart_attributes"]["table"]}
        assert attrs[5]["raw"]["value"] == 0
        stem = done["file"]["id"].split(".", 1)[1]
        copy = json.loads((_user_dir(hass) / f"{stem}.json").read_text())
        cattrs = {a["id"]: a for a in copy["smart"]["ata_smart_attributes"]["table"]}
        assert cattrs[5]["raw"] == {"value": 40, "string": "40"}
        assert cattrs[194]["raw"]["value"] & 0xFF == 51 and copy["smart"]["temperature"]["current"] == 51
        assert copy["origin"] == {"type": "uploaded", "edited_from": "seed.sata-hdd-healthy"}
        # The card now shows the copy, judged again at once; same drive.
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        item = _drive_item(rep, "SATA HDD, healthy, edited")
        assert item["card_id"] == sata["card_id"] and item["id"] == drive_id
        assert item["verdict"]["state"] == "YES"
        assert coordinator.data[drive_id]["smart_data"]["ata_smart_attributes"]["table"][0]["raw"]["value"] == 40
        # A second edit changes the copy in place.
        again = await H.call(hass, conn, "replica/edit", card_id=sata["card_id"], ata={"5": 0})
        assert again["switched"] is False and again["file"]["id"] == done["file"]["id"]
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        assert _drive_item(rep, "SATA HDD, healthy, edited")["verdict"]["state"] == "NO"
        # NVMe: the four fields.
        nvme = _drive_item(rep, "NVMe, worn")
        await H.call(hass, conn, "replica/edit", card_id=nvme["card_id"],
                     nvme={"available_spare": 100, "percentage_used": 0, "media_errors": 0,
                           "temperature": 40})
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        item = _drive_item(rep, "NVMe, worn, edited")
        log = item["drive"]["smart_data"]["nvme_smart_health_information_log"]
        assert log["available_spare"] == 100 and item["verdict"]["state"] == "NO"
    run(tmp_path, body, monkeypatch)


def test_edit_a_device_statistics_reading(tmp_path, monkeypatch):
    """The uncorrectables seed's count lives only in Device Statistics (no
    attribute 187): the editor changes it there, and the card is judged again."""
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=["seed.sata-hdd-devstat-uncorrectables"])
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        item = _drive_item(rep, "SATA HDD, uncorrectables in Device Statistics")
        assert item["verdict"]["state"] == "YES"
        assert any(r["key"] == "reported_unc_devstat" and r["mark"] == "crit" for r in item["view"]["rows"])
        done = await H.call(hass, conn, "replica/edit", card_id=item["card_id"], devstat={"4:8": 0})
        assert done["switched"] is True
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        item = _drive_item(rep, "SATA HDD, uncorrectables in Device Statistics, edited")
        assert item["verdict"]["state"] == "NO"
        page4 = next(p for p in item["drive"]["device_statistics"]["pages"] if p["number"] == 4)
        assert next(e for e in page4["table"] if e["offset"] == 8)["value"] == 0
        for fields in ({"devstat": {"1:16": 1}}, {"devstat": {"4:8": -1}}, {"devstat": {"7:8": 1}}):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "replica/edit", card_id=item["card_id"], **fields)
            assert err.value.code in ("invalid_format", "bad_value"), fields
    run(tmp_path, body, monkeypatch)


def test_edit_refuses_what_it_cannot_change(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        await H.call(hass, conn, "replica/add", file_ids=[
            "seed.sata-hdd-healthy", "seed.nvme-wear", "seed.zfs-pool-degraded"])
        rep = _replica_machine(await H.call(hass, conn, "cards"))
        sata = _drive_item(rep, "SATA HDD, healthy")
        nvme = _drive_item(rep, "NVMe, worn")
        pool = next(i for i in rep["data"]["items"] if i["type"] == "pool")
        cases = [
            (sata["card_id"], {"ata": {"7": 1}}, "bad_value"),          # not in the file
            (sata["card_id"], {"ata": {"194": 151}}, "bad_value"),      # temperature range
            (sata["card_id"], {}, "bad_value"),                         # nothing to change
            (sata["card_id"], {"nvme": {"media_errors": 1}}, "bad_value"),  # not an NVMe drive
            (nvme["card_id"], {"nvme": {"available_spare": 101}}, "bad_value"),
            (pool["card_id"], {"ata": {"5": 1}}, "not_found"),          # a pool: not editable
            (f"{NAS}|MOCK-SATA-0003", {"ata": {"5": 1}}, "not_found"),  # a real drive
        ]
        for card_id, fields, code in cases:
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "replica/edit", card_id=card_id, **fields)
            assert err.value.code == code, (card_id, fields)
        for fields in ({"ata": {"5": -1}}, {"ata": {"5": True}}, {"ata": {"x5": 1}},
                       {"nvme": {"critical_warning": 1}}, {"ata": {"5": 2 ** 48}}):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "replica/edit", card_id=sata["card_id"], **fields)
            assert err.value.code in ("invalid_format", "bad_value"), fields
        assert [f for f in (await H.call(hass, conn, "replica/library"))["files"]
                if f["source"] == "user"] == []
        viewer = H.FakeConnection(hass, admin=False)
        for command, fields in (("replica/edit", {"card_id": sata["card_id"], "ata": {"5": 1}}),
                                ("replica/delete", {"file_id": "user.x"})):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, viewer, command, **fields)
            assert err.value.code == "unauthorized"
    run(tmp_path, body, monkeypatch)
