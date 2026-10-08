"""The mock agent (tools/mock-agent.py) speaks the v0.8.0 agent API.

Each test starts the mock on a free port in a thread, with no mDNS and a
temporary data directory, and talks to it over HTTP. The payloads are then fed through the
integration's own code (devstat.merge_devstat, attention.evaluate_attention,
devstat.data_volume, pool_health) to show the replica drives and pool read
in Home Assistant the way the spec says they should. The drives here are
added by their old preset keys; tests/test_mock_replicas.py covers the
replica files and routes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from custom_components.smart_sniffer import attention as att
from custom_components.smart_sniffer import pool_health as ph
from custom_components.smart_sniffer.devstat import DEVSTAT_KEY, data_volume, merge_devstat

_MOCK_PATH = Path(__file__).resolve().parents[1] / "tools" / "mock-agent.py"

UNC_18 = "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)"

# The Device Statistics status each old preset key reports.
EXPECTED_DEVSTAT = {
    "sata_hdd": ("absent", None),
    "sata_hdd_devstat": ("present", None),
    "sata_ssd": ("absent", None),
    "nvme": ("not_applicable", None),
    "nvme_usb": ("not_applicable", None),
    "usb_blocked": ("unavailable", "failed"),
    "virtual_disk": ("absent", None),
    "sas_enterprise": ("not_applicable", None),
}


def _load_mock():
    spec = importlib.util.spec_from_file_location("smart_sniffer_mock_agent", _MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def get(self, path: str) -> Any:
        code, payload = self.call("GET", path)
        assert code == 200, (path, code, payload)
        return payload

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        code, payload = self.call(method, path, body)
        assert code in (200, 201), (method, path, code, payload)
        return payload

    def add(self, preset: str) -> str:
        return self.ok("POST", "/api/drives", {"preset": preset})["id"]


@pytest.fixture
def client(tmp_path):
    server, zc, _ = mock.build_server([
        "--port", "0", "--host", "127.0.0.1", "--no-mdns", "--data-dir", str(tmp_path),
    ])
    assert zc is None
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


def _merged(drive: dict[str, Any]) -> dict[str, Any]:
    """What the coordinator stores for a drive: the payload plus _devstat."""
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)
    return drive


# --- /api/health ------------------------------------------------------------------


AGENT_HEALTH_KEYS = {"status", "version", "os", "uptime_seconds", "endpoints", "drives", "filesystems"}


def test_health_has_the_agents_shape(client):
    client.add("nvme")
    health = client.get("/api/health")
    assert AGENT_HEALTH_KEYS <= set(health)
    assert health["status"] == "ok"
    assert health["version"].startswith("0.8.0")
    assert health["drives"] == 1
    # The NVMe replica carries / and /home, so /api/filesystems is advertised.
    assert health["endpoints"] == ["/api/health", "/api/drives", "/api/drives/{id}", "/api/filesystems"]
    assert health["filesystems"] == 2
    # No pools yet: the agent with pool status off sends neither key.
    assert "pools" not in health and "pools_status" not in health
    assert not ph.advertises_pools(health)
    # Extras for the app's Control Center.
    assert health["drive_count"] == 1 and health["auth_enabled"] is False


def test_health_advertises_pools_once_a_pool_is_added(client):
    client.ok("POST", "/api/drives", {"preset": "zfs_pool"})
    health = client.get("/api/health")
    assert "/api/pools" in health["endpoints"]
    assert health["pools_status"] == "ok"
    assert health["pools"] == 1
    assert ph.advertises_pools(health)


def test_health_needs_no_token_but_the_rest_does(tmp_path):
    """Like the agent's auth middleware: /api/health is always public."""
    server, zc, _ = mock.build_server([
        "--port", "0", "--host", "127.0.0.1", "--no-mdns", "--data-dir", str(tmp_path),
        "--token", "s3cret", "--preload", "sata_hdd",
    ])
    assert zc is None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path: str, token: str | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(base + path)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    try:
        code, health = get("/api/health")
        assert code == 200 and health["status"] == "ok" and health["auth_enabled"] is True
        assert get("/api/health", "wrong")[0] == 200
        for path in ("/api/drives", "/api/drives/mock-sata-0002", "/api/state", "/api/replicas",
                     "/api/scenarios", "/mock/replicas"):
            assert get(path) == (401, {"error": "unauthorized"}), path
            assert get(path, "wrong")[0] == 401, path
            assert get(path, "s3cret")[0] == 200, path
    finally:
        server.shutdown()
        server.server_close()


def test_pools_endpoint_is_absent_without_pools(client):
    code, _ = client.call("GET", "/api/pools")
    assert code == 404


# --- /api/drives/{id} -------------------------------------------------------------


@pytest.mark.parametrize("preset", sorted(EXPECTED_DEVSTAT))
def test_every_preset_has_device_statistics_and_derived(client, preset):
    drive_id = client.add(preset)
    drive = client.get(f"/api/drives/{drive_id}")
    assert list(drive) == [
        "id", "device_path", "model", "serial", "protocol", "readable", "last_updated",
        "smart_data", "device_statistics", "derived",
    ]
    status, reason = EXPECTED_DEVSTAT[preset]
    block = drive["device_statistics"]
    assert block["status"] == status
    assert block.get("reason") == reason
    if status == "present":
        assert block["complete"] is True and block["exit_status"] == 0
        assert block["logical_block_size"] == 512
        assert {p["number"] for p in block["pages"]} == {1, 3, 4, 5}
    else:
        assert set(block) <= {"status", "reason"}
    derived = drive["derived"]
    for key in ("host_writes", "host_reads"):
        assert (key in derived) != (f"{key}_omitted" in derived), (preset, derived)


def test_derived_sources_per_preset(client):
    ids = {p: client.add(p) for p in EXPECTED_DEVSTAT}

    def derived(preset):
        return client.get(f"/api/drives/{ids[preset]}")["derived"]

    assert derived("sata_hdd_devstat")["host_writes"] == {
        "bytes": 210_606_206_000 * 512, "source": "ata_device_statistics",
    }
    assert derived("nvme")["host_writes"] == {"bytes": 9_121_094 * 512_000, "source": "nvme"}
    assert derived("sata_hdd")["host_writes"]["source"] == "ata_attribute"
    assert derived("sata_hdd")["host_writes"]["attribute_id"] == 241
    assert derived("sata_ssd")["host_reads_omitted"] == "no_source"
    assert derived("usb_blocked") == {
        "host_writes_omitted": "device_statistics_unavailable",
        "host_reads_omitted": "device_statistics_unavailable",
    }
    for preset in ("virtual_disk", "sas_enterprise"):
        assert derived(preset) == {"host_writes_omitted": "no_source", "host_reads_omitted": "no_source"}


def test_the_devstat_hdd_has_a_fake_serial_and_no_187(client):
    drive = client.get(f"/api/drives/{client.add('sata_hdd_devstat')}")
    assert drive["serial"].startswith("MOCK-")
    assert drive["smart_data"]["serial_number"] == drive["serial"]
    table = drive["smart_data"]["ata_smart_attributes"]["table"]
    assert 187 not in {a["id"] for a in table}
    assert not {a["name"] for a in table} & att._ATA_WEAR_NAMES


def test_the_existing_smart_patch_is_unchanged(client):
    drive_id = client.add("sata_hdd")
    assert client.ok("PATCH", f"/api/drives/{drive_id}/smart", {"Reallocated_Sector_Ct": "5"}) == {"ok": True}
    assert client.ok("PATCH", f"/mock/drives/{drive_id}", {"smart_passed": False}) == {"ok": True}
    smart = client.get(f"/api/drives/{drive_id}")["smart_data"]
    row = next(a for a in smart["ata_smart_attributes"]["table"] if a["name"] == "Reallocated_Sector_Ct")
    assert row["raw"] == {"value": 5, "string": "5"}
    assert smart["smart_status"]["passed"] is False


# --- The integration reads the gap-fill drive ------------------------------------


def test_gap_fill_hdd_goes_from_no_to_yes(client):
    drive_id = client.add("sata_hdd_devstat")
    drive = _merged(client.get(f"/api/drives/{drive_id}"))
    assert att.evaluate_attention(drive) == ("NO", "none", [], [])

    result = client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"reported_uncorrectable": 18})
    assert result["device_statistics"]["status"] == "present"

    drive = _merged(client.get(f"/api/drives/{drive_id}"))
    state, severity, reasons, accepted = att.evaluate_attention(drive)
    assert (state, severity, accepted) == ("YES", "critical", [])
    assert reasons == [UNC_18]
    assert reasons[0].endswith("(expected 0; from device statistics)")


def test_devstat_status_overrides_and_reset(client):
    drive_id = client.add("sata_hdd_devstat")
    client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"reported_uncorrectable": 18})
    fresh = _merged(client.get(f"/api/drives/{drive_id}"))
    _, held = merge_devstat(fresh, None)

    result = client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"status": "unavailable", "reason": "timeout"})
    assert result["device_statistics"] == {"status": "unavailable", "reason": "timeout"}
    assert result["derived"]["host_writes_omitted"] == "device_statistics_unavailable"
    # The integration keeps the held count through a failed read.
    stale = client.get(f"/api/drives/{drive_id}")
    stale[DEVSTAT_KEY], _ = merge_devstat(stale, held)
    assert att.evaluate_attention(stale)[2] == [UNC_18]

    off = client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"status": "off", "reason": "os"})
    assert off["device_statistics"] == {"status": "off", "reason": "os"}
    reset = client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"status": "default"})
    assert reset["device_statistics"]["status"] == "present"


def test_devstat_other_counts_create_their_entries(client):
    drive_id = client.add("sata_hdd_devstat")
    result = client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {
        "reallocated_logical_sectors": 4, "percentage_used_endurance": 91,
    })
    pages = {p["number"]: p for p in result["device_statistics"]["pages"]}
    assert pages[3]["table"][0]["value"] == 4
    assert (pages[7]["table"][0]["offset"], pages[7]["table"][0]["value"]) == (8, 91)
    drive = _merged(client.get(f"/api/drives/{drive_id}"))
    # Attribute 5 is in the table, so only the wear reading is a gap.
    assert drive[DEVSTAT_KEY]["realloc"] == 4
    assert att.evaluate_attention(drive)[0] == "MAYBE"


@pytest.mark.parametrize("body, code", [
    ({"status": "bogus"}, 400),
    ({"status": "unavailable", "reason": "tired"}, 400),
    ({"status": "absent", "reason": "timeout"}, 400),
    ({"reported_uncorrectable": -1}, 400),
    ({"reported_uncorrectable": True}, 400),
    ({"surprise": 1}, 400),
])
def test_devstat_patch_refuses_bad_bodies(client, body, code):
    drive_id = client.add("sata_hdd_devstat")
    assert client.call("PATCH", f"/api/drives/{drive_id}/devstat", body)[0] == code


def test_devstat_counts_need_pages(client):
    drive_id = client.add("nvme")
    code, payload = client.call("PATCH", f"/api/drives/{drive_id}/devstat", {"reported_uncorrectable": 1})
    assert code == 409 and "pages" in payload["error"]
    assert client.call("PATCH", "/api/drives/nope/devstat", {"reported_uncorrectable": 1})[0] == 404


# --- Data Written / Data Read ---------------------------------------------------


def test_data_volume_reads_the_mock(client):
    hdd = _merged(client.get(f"/api/drives/{client.add('sata_hdd_devstat')}"))
    nvme = _merged(client.get(f"/api/drives/{client.add('nvme')}"))
    assert data_volume(hdd, "host_writes") == (107_830_377_472_000, {"source": "ata_device_statistics"})
    assert round(data_volume(nvme, "host_writes")[0] / 1e12, 2) == 4.67
    assert round(data_volume(nvme, "host_reads")[0] / 1e12, 2) == 9.34
    sata = _merged(client.get(f"/api/drives/{client.add('sata_hdd')}"))
    assert data_volume(sata, "host_writes") == (
        12 * 10**12,
        {"source": "ata_attribute", "attribute_id": 241, "attribute_name": "Total_LBAs_Written"},
    )


@pytest.mark.parametrize("preset, source", [
    ("sata_hdd_devstat", "ata_device_statistics"),
    ("nvme", "nvme"),
    ("sata_ssd", "ata_attribute"),
])
def test_derived_patch_sets_tb_and_keeps_the_source(client, preset, source):
    drive_id = client.add(preset)
    result = client.ok("PATCH", f"/api/drives/{drive_id}/derived", {"host_writes_tb": 4.67})
    assert result["derived"]["host_writes"]["source"] == source
    drive = _merged(client.get(f"/api/drives/{drive_id}"))
    written, attrs = data_volume(drive, "host_writes")
    assert attrs["source"] == source
    assert round(written / 1e12, 2) == 4.67


def test_derived_patch_without_a_source_is_refused(client):
    drive_id = client.add("sas_enterprise")
    code, payload = client.call("PATCH", f"/api/drives/{drive_id}/derived", {"host_writes_tb": 1})
    assert code == 409 and payload["omitted"] == "no_source"
    nvme = client.add("nvme")
    assert client.call("PATCH", f"/api/drives/{nvme}/derived", {"host_writes_tb": -1})[0] == 400
    assert client.call("PATCH", f"/api/drives/{nvme}/derived", {})[0] == 400


# --- The pool ---------------------------------------------------------------------


def _poll(client: Client, registered: list[str]) -> tuple[list | None, list | None]:
    """One coordinator poll's pool step: fetch_pools, then missing_pools.

    The HTTP calls are made first, outside the event loop: Home Assistant's
    blocking-call guard, installed once any test imports Home Assistant,
    refuses a blocking socket call made inside a coroutine.
    """
    health = client.get("/api/health")
    code, payload = client.call("GET", ph.POOLS_ENDPOINT)
    responses = {ph.POOLS_ENDPOINT: payload if code == 200 else None}

    async def get_json(path):
        return responses.get(path)

    pools = asyncio.run(ph.fetch_pools(health, get_json))
    return pools, ph.missing_pools(ph.advertises_pools(health), pools, registered)


def test_pool_healthy_degraded_missing_and_back(client):
    assert client.ok("POST", "/api/drives", {"preset": "zfs_pool"})["name"] == "tank"

    pools, missing = _poll(client, [])
    (tank,) = pools
    assert set(tank) == {
        "name", "state", "status", "action", "read_errors", "write_errors", "checksum_errors",
        "data_errors", "scan_function", "scan_state", "scrub_in_progress", "last_scrub_end",
        "last_scrub_repaired", "last_scrub_errors", "problem_vdevs",
    }
    assert tank["state"] == "ONLINE" and tank["problem_vdevs"] == []
    assert ph.pool_problems(tank) == []
    assert ph.last_scrub_end(tank) is not None
    assert missing == []
    actions, state = ph.notification_actions({}, pools, missing)
    assert actions == []

    client.ok("PATCH", "/api/pools/tank", {
        "state": "DEGRADED",
        "devices": [{"name": "MOCK0001", "state": "FAULTED", "read": 12, "write": 0, "cksum": 0}],
    })
    pools, missing = _poll(client, ["tank"])
    (tank,) = pools
    assert ph.pool_problems(tank) == [
        "State: DEGRADED",
        "Read errors: 12",
        "mirror-0: DEGRADED",
        "MOCK0001: FAULTED, 12 read errors",
    ]
    actions, state = ph.notification_actions(state, pools, missing)
    assert [a.kind for a in actions] == ["create"]

    client.ok("POST", "/api/pools/tank/vanish")
    pools, missing = _poll(client, ["tank"])
    assert pools == [] and missing == ["tank"]
    actions, state = ph.notification_actions(state, pools, missing)
    assert [(a.kind, a.reasons) for a in actions] == [("missing", [ph.MISSING_REASON])]
    assert client.get("/api/health")["pools"] == 0

    client.ok("POST", "/api/pools/tank/restore")
    client.ok("PATCH", "/api/pools/tank", {
        "devices": [{"name": "MOCK0001", "state": "ONLINE", "read": 0}],
    })
    pools, missing = _poll(client, ["tank"])
    assert missing == [] and pools[0]["state"] == "ONLINE"
    assert pools[0]["status"] is None
    actions, _ = ph.notification_actions(state, pools, missing)
    assert [a.kind for a in actions] == ["dismiss"]


def test_pool_patch_data_errors_and_validation(client):
    client.ok("POST", "/api/pools", {"preset": "zfs_pool"})
    result = client.ok("PATCH", "/api/pools/tank", {"errors": "3 data errors, use '-v' for a list"})
    assert result["pool"]["data_errors"] == 3
    assert ph.pool_problems(result["pool"]) == ["Data errors: 3"]
    assert client.ok("PATCH", "/api/pools/tank", {"errors": "No known data errors"})["pool"]["data_errors"] == 0
    for body in (
        {"state": "MISSING"},
        {"devices": [{"name": "sdz", "state": "FAULTED"}]},
        {"devices": [{"name": "MOCK0001", "state": "BROKEN"}]},
        {"devices": [{"name": "MOCK0001", "read": -2}]},
        {"errors": "lots"},
        {"colour": "red"},
    ):
        assert client.call("PATCH", "/api/pools/tank", body)[0] == 400, body
    assert client.call("PATCH", "/api/pools/nope", {"state": "ONLINE"})[0] == 404
    assert client.call("POST", "/api/pools/nope/vanish")[0] == 404


def test_a_second_pool_gets_its_own_name_and_removal_turns_pools_off(client):
    assert client.ok("POST", "/api/pools", {"preset": "zfs_pool"})["name"] == "tank"
    assert client.ok("POST", "/api/pools", {"preset": "zfs_pool"})["name"] == "tank2"
    assert [p["name"] for p in client.get("/api/pools")] == ["tank", "tank2"]
    client.ok("DELETE", "/api/pools/tank")
    client.ok("DELETE", "/api/pools/tank2")
    assert "/api/pools" not in client.get("/api/health")["endpoints"]


# --- Control views and the dashboard ---------------------------------------------


def test_lab_state_lists_drives_pools_and_presets(client):
    drive_id = client.add("sata_hdd_devstat")
    client.ok("POST", "/api/drives", {"preset": "zfs_pool"})
    client.ok("POST", "/api/pools/tank/vanish")
    lab = client.get("/api/lab")
    assert lab["version"].startswith("0.8.0")
    assert lab["presets"]["sata_hdd_devstat"] == {
        "label": "SATA HDD with Device Statistics (WDC Ultrastar 14TB)", "kind": "drive",
    }
    assert lab["presets"]["zfs_pool"] == {"label": "ZFS pool (two-disk mirror)", "kind": "pool"}
    (drive,) = lab["drives"]
    assert drive["id"] == drive_id
    assert drive["lab"]["devstat_counts"]["reported_uncorrectable"] == 0
    (pool,) = lab["pools"]
    assert pool["lab"]["vanished"] is True
    assert [d["name"] for d in pool["lab"]["devices"]] == ["MOCK0001", "MOCK0002"]
    assert client.get("/mock/state")["pools"] == lab["pools"]


def test_dashboard_lists_the_replica_files(client):
    req = urllib.request.urlopen(client.base + "/", timeout=5)
    html = req.read().decode()
    assert "shipped.sata-hdd-devstat-uncorrectables" in html and "shipped.zfs-pool-degraded" in html
    assert "REPLICAS_JSON" not in html


def test_unknown_preset_is_refused(client):
    assert client.call("POST", "/api/drives", {"preset": "floppy"})[0] == 400


def test_persistence_round_trip(tmp_path):
    def start():
        server, _, _ = mock.build_server([
            "--port", "0", "--host", "127.0.0.1", "--no-mdns",
            "--data-dir", str(tmp_path), "--preload", "sata_hdd_devstat,zfs_pool",
        ])
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return server, Client(f"http://127.0.0.1:{server.server_address[1]}")

    server, client = start()
    (drive_id,) = [d["id"] for d in client.get("/api/drives")]
    client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"reported_uncorrectable": 7})
    client.ok("POST", "/api/pools/tank/vanish")
    server.shutdown()
    server.server_close()

    server, client = start()
    try:
        assert [d["id"] for d in client.get("/api/drives")] == [drive_id]
        lab = client.get("/api/lab")
        assert lab["drives"][0]["lab"]["devstat_counts"]["reported_uncorrectable"] == 7
        assert [p["name"] for p in lab["pools"]] == ["tank"]
        assert client.get("/api/pools") == []
    finally:
        server.shutdown()
        server.server_close()


# --- The /mock/ spelling and the app's proxy ---------------------------------------


def test_every_write_works_under_mock_too(client):
    """The dashboard calls /mock/...; the app's proxy turns that into /api/..."""
    drive_id = client.ok("POST", "/mock/drives", {"preset": "sata_hdd_devstat"})["id"]
    nvme = client.ok("POST", "/mock/drives", {"preset": "nvme"})["id"]
    assert client.ok("PATCH", f"/mock/drives/{drive_id}/smart", {"Reallocated_Sector_Ct": 2}) == {"ok": True}
    devstat = client.ok("PATCH", f"/mock/drives/{drive_id}/devstat", {"reported_uncorrectable": 18})
    assert devstat["device_statistics"]["status"] == "present"
    derived = client.ok("PATCH", f"/mock/drives/{nvme}/derived", {"host_reads_tb": 2.5})
    # Stored as whole NVMe data units (512,000 bytes), so within one unit.
    reads = derived["derived"]["host_reads"]
    assert reads["source"] == "nvme" and abs(reads["bytes"] - 2_500_000_000_000) <= 512_000

    assert client.ok("POST", "/mock/pools", {"preset": "zfs_pool"})["name"] == "tank"
    pool = client.ok("PATCH", "/mock/pools/tank", {"state": "FAULTED"})["pool"]
    assert pool["state"] == "FAULTED"
    assert client.ok("POST", "/mock/pools/tank/vanish")["pool"]["lab"]["vanished"] is True
    assert client.get("/api/pools") == []
    assert client.ok("POST", "/mock/pools/tank/restore")["pool"]["lab"]["vanished"] is False
    assert [p["name"] for p in client.get("/api/pools")] == ["tank"]

    lab = client.get("/api/state")
    assert lab == client.get("/mock/state")
    counts = next(d for d in lab["drives"] if d["id"] == drive_id)["lab"]["devstat_counts"]
    assert counts["reported_uncorrectable"] == 18

    client.ok("DELETE", f"/mock/drives/{nvme}")
    client.ok("DELETE", "/mock/pools/tank")
    assert [d["id"] for d in client.get("/api/drives")] == [drive_id]
    assert client.call("GET", "/api/pools")[0] == 404


def test_the_apps_preload_line(tmp_path):
    """run.sh starts the mock this way; all four drives come up."""
    server, _, _ = mock.build_server([
        "--port=0", "--host", "127.0.0.1", "--no-mdns", "--data-dir", str(tmp_path),
        "--preload", "sata_hdd,sata_ssd,nvme,usb_blocked",
    ])
    try:
        assert [d["_preset"] for d in mock.store.drives.values()] == ["sata_hdd", "sata_ssd", "nvme", "usb_blocked"]
        assert (tmp_path / "mock-drives.json").exists()
    finally:
        server.server_close()


def test_a_store_saved_by_the_apps_old_copy_loads_and_gains_the_new_fields(tmp_path):
    """The app's earlier mock wrote drives without device statistics or pools."""
    old = {
        "order": ["s4enmockab"],
        "drives": {"s4enmockab": {
            "id": "s4enmockab", "device_path": "/dev/sdb", "model": "Samsung SSD 870 EVO 500GB",
            "serial": "S4ENMOCKAB", "protocol": "ATA", "_preset": "sata_ssd",
            "smart_data": {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 0,
                 "raw": {"value": 3, "string": "3"}},
            ]}},
        }},
    }
    (tmp_path / "mock-drives.json").write_text(json.dumps(old))
    server, _, _ = mock.build_server([
        "--port", "0", "--host", "127.0.0.1", "--no-mdns", "--data-dir", str(tmp_path),
        "--preload", "nvme",
    ])
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        # The saved drive comes back instead of the preload, with its edit kept.
        assert [d["id"] for d in client.get("/api/drives")] == ["s4enmockab"]
        drive = client.get("/api/drives/s4enmockab")
        assert drive["device_statistics"] == {"status": "absent"}
        table = drive["smart_data"]["ata_smart_attributes"]["table"]
        assert next(a for a in table if a["id"] == 5)["raw"]["value"] == 3
        assert drive["derived"]["host_writes"]["attribute_id"] == 241
    finally:
        server.shutdown()
        server.server_close()


def test_mdns_name_is_never_the_real_agents(monkeypatch):
    """smartha-<host> is the real agent's name and the integration's unique id."""
    registered = []

    class FakeInfo:
        def __init__(self, stype, name, **kwargs):
            self.name = name
            self.kwargs = kwargs

    class FakeZeroconf:
        def register_service(self, info):
            registered.append(info)

    fake = type(sys)("zeroconf")
    fake.ServiceInfo = FakeInfo
    fake.Zeroconf = FakeZeroconf
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    monkeypatch.setattr(mock.socket, "gethostname", lambda: "labhost.local")
    mock.start_mdns(9100, "")
    (info,) = registered
    assert info.name == "smartha-mock-labhost._smartha._tcp.local."
    assert info.kwargs["port"] == 9100
