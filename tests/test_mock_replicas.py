"""Replica files and the mock agent's replica routes.

Every drive and pool the mock serves comes from a replica file in
tools/replicas (six shipped files), tools/replicas/extra (the other old
presets) or a user folder. These tests load each file, run it through the
integration's own rules, apply every scenario, and cover upload, save (with
the serial and WWN replaced), export, the user folder and persistence.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from custom_components.smart_sniffer import attention as att
from custom_components.smart_sniffer import pool_health as ph
from custom_components.smart_sniffer.devstat import DEVSTAT_KEY, merge_devstat
from tests.devstat_payloads import agent_drive, read_fixture
from tests.test_mock_agent import mock

REPO = Path(__file__).resolve().parents[1]
REPLICAS = REPO / "tools" / "replicas"
EXTRA = REPLICAS / "extra"
OLD_STORE = Path(__file__).resolve().parent / "fixtures" / "mock_v080" / "mock-drives.json"

SHIPPED = {
    "sata-hdd-healthy": "NO",
    "sata-hdd-reallocated": "YES",
    "sata-hdd-devstat-uncorrectables": "YES",
    "nvme-wear": "MAYBE",
    "zfs-pool-degraded": "YES",
    "smart-failed": "YES",
}
EXTRAS = {
    "sata-ssd": "NO",
    "nvme-usb": "NO",
    "usb-blocked": "UNSUPPORTED",
    "virtual-disk": "UNSUPPORTED",
    "sas-enterprise": "NO",
}
# What each scenario promises, by kind.
SCENARIO_STATES = {
    "ata": {"healthy": "NO", "reallocated_growing": "YES", "pending_sectors": "YES",
            "command_timeouts": "MAYBE", "smart_failed": "YES"},
    "ata_devstat": {"healthy": "NO", "devstat_uncorrectables": "YES"},
    "nvme": {"healthy": "NO", "nvme_wear": "MAYBE", "media_errors": "YES", "critical_warning": "YES"},
    "zfs_pool": {"healthy": "NO", "degraded": "YES", "faulted": "YES", "vanished": "MISSING"},
}
SHIPPED_BY_KIND = {
    "ata": ["shipped.sata-hdd-healthy", "shipped.sata-hdd-reallocated", "shipped.smart-failed"],
    "ata_devstat": ["shipped.sata-hdd-devstat-uncorrectables"],
    "nvme": ["shipped.nvme-wear"],
    "zfs_pool": ["shipped.zfs-pool-degraded"],
}


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def raw(self, method: str, path: str, data: bytes | None = None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        code, _, data = self.raw(method, path, None if body is None else json.dumps(body).encode())
        return code, json.loads(data)

    def get(self, path: str) -> Any:
        code, payload = self.call("GET", path)
        assert code == 200, (path, code, payload)
        return payload

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        code, payload = self.call(method, path, body)
        assert code in (200, 201), (method, path, code, payload)
        return payload


def _start(*args: str) -> tuple[Any, Client]:
    server, zc, _ = mock.build_server(["--port", "0", "--host", "127.0.0.1", "--no-mdns", *args])
    assert zc is None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, Client(f"http://127.0.0.1:{server.server_address[1]}")


def _stop(server: Any) -> None:
    server.shutdown()
    server.server_close()


@pytest.fixture
def user_dir(tmp_path):
    folder = tmp_path / "user"
    folder.mkdir()
    return folder


@pytest.fixture
def client(tmp_path, user_dir):
    server, client = _start("--data-dir", str(tmp_path / "data"), "--user-replicas", str(user_dir))
    try:
        yield client
    finally:
        _stop(server)


def _verdict(drive: dict[str, Any]) -> tuple[str, list[str]]:
    drive = copy.deepcopy(drive)
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)
    state, _, reasons, _ = att.evaluate_attention(drive)
    return state, reasons


def _file_drive(doc: dict[str, Any]) -> dict[str, Any]:
    """A replica file's drive as the agent would serve it (no derived)."""
    meta = doc["meta"]
    return {"id": "x", "device_path": meta["device_path"], "model": meta["model"], "serial": meta["serial"],
            "protocol": meta["protocol"], "readable": True, "smart_data": copy.deepcopy(doc["smart"]),
            "device_statistics": copy.deepcopy(doc["devstat"])}


def _pool_state(client: Client, name: str) -> str:
    """NO, YES (unhealthy) or MISSING, as the integration reads the pool."""
    health = client.get("/api/health")
    code, pools = client.call("GET", "/api/pools")
    pools = pools if code == 200 else None
    missing = ph.missing_pools(ph.advertises_pools(health), pools, [name])
    if name in missing:
        return "MISSING"
    pool = next(p for p in pools if p["name"] == name)
    return "YES" if ph.pool_problems(pool) else "NO"


def _drive_state(client: Client, drive_id: str) -> str:
    return _verdict(client.get(f"/api/drives/{drive_id}"))[0]


# --- The files -----------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("stem, state", sorted({**SHIPPED, **EXTRAS}.items()))
def test_every_file_is_valid_and_reads_as_promised(stem, state):
    path = (REPLICAS if stem in SHIPPED else EXTRA) / f"{stem}.json"
    doc = mock.validate_replica(_load(path))
    assert path.stat().st_size <= mock.MAX_REPLICA_BYTES
    assert doc["format"] == "smart-sniffer-replica" and doc["version"] == 1
    assert doc["origin"] == {"type": "shipped"}
    if doc["kind"] == "zfs_pool":
        served = mock.pool_payload(doc["pool"])
        problems = ph.pool_problems(served)
        assert (state == "YES") == bool(problems)
        assert doc["verdict"]["reasons"] == problems
        assert all(d["name"].startswith("MOCK") for d in doc["pool"]["devices"])
        return
    assert doc["meta"]["serial"].startswith("MOCK-")
    assert doc["smart"].get("serial_number", doc["meta"]["serial"]) == doc["meta"]["serial"]
    got, reasons = _verdict(_file_drive(doc))
    assert got == state
    assert (doc["verdict"]["state"], doc["verdict"]["reasons"]) == (got, reasons)
    if stem == "sata-hdd-devstat-uncorrectables":
        assert reasons == ["Reported Uncorrectable Errors: 18 (expected 0; from device statistics)"]
    if stem == "smart-failed":
        assert reasons[0] == "SMART overall status: FAILED"


def test_the_six_shipped_files_and_their_order():
    stems = sorted(p.stem for p in REPLICAS.glob("*.json") if p.name != "scenarios.json")
    assert stems == sorted(SHIPPED)
    orders = {p.stem: _load(p)["order"] for p in REPLICAS.glob("*.json") if p.name != "scenarios.json"}
    assert sorted(orders.values()) == [1, 2, 3, 4, 5, 6]
    assert sorted(p.stem for p in EXTRA.glob("*.json")) == sorted(EXTRAS)


def test_the_listing(client):
    listing = client.get("/api/replicas")
    assert listing["agent"] == "ok"
    assert listing["folder"]["status"] == "ok"
    shipped = [f for f in listing["files"] if f["source"] == "shipped"]
    assert [f["id"] for f in shipped[:6]] == [
        "shipped.sata-hdd-healthy", "shipped.sata-hdd-reallocated", "shipped.sata-hdd-devstat-uncorrectables",
        "shipped.nvme-wear", "shipped.zfs-pool-degraded", "shipped.smart-failed",
    ]
    assert shipped[0] == {
        "id": "shipped.sata-hdd-healthy", "name": "SATA HDD, healthy", "kind": "ata",
        "description": "A healthy Seagate Barracuda 2TB.", "source": "shipped", "order": 1, "valid": True,
    }
    assert {f["id"] for f in shipped[6:]} == {f"shipped.{s}" for s in EXTRAS}
    assert all(f["valid"] for f in listing["files"])
    assert "scenarios" not in {f["id"].split(".", 1)[1] for f in listing["files"]}


def test_only_the_named_folders_load(tmp_path):
    server, client = _start("--replicas", str(REPLICAS))
    try:
        listing = client.get("/api/replicas")
        assert len(listing["files"]) == 6
        assert listing["folder"] == {"path": None, "status": "missing"}
        assert client.call("POST", "/api/drives", {"preset": "sata_ssd"})[0] == 400
    finally:
        _stop(server)


# --- Adding ----------------------------------------------------------------------------


def test_add_by_file_id_keeps_the_serial_until_it_clashes(client):
    first = client.ok("POST", "/api/drives", {"replica": "shipped.smart-failed"})
    assert first == {"id": "mock-sata-0001", "kind": "drive", "replica": "shipped.smart-failed"}
    second = client.ok("POST", "/mock/drives", {"replica": "shipped.smart-failed"})["id"]
    drive = client.get(f"/api/drives/{second}")
    assert drive["serial"].startswith("MOCK-SATA-0001-") and len(drive["serial"]) == len("MOCK-SATA-0001-") + 4
    assert drive["smart_data"]["serial_number"] == drive["serial"]
    assert _verdict(drive)[0] == "YES"

    # Either route takes either kind; the file decides.
    pool = client.ok("POST", "/api/drives", {"replica": "shipped.zfs-pool-degraded"})
    assert pool == {"id": "tank", "name": "tank", "kind": "pool", "replica": "shipped.zfs-pool-degraded"}
    assert client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})["name"] == "tank2"
    assert _pool_state(client, "tank") == "YES"

    assert client.call("POST", "/api/drives", {"replica": "shipped.nope"}) == (
        404, {"error": "not_found", "message": "no replica file shipped.nope"})
    assert client.call("POST", "/api/drives", {"replica": "x", "preset": "nvme"})[0] == 400


def test_a_shipped_pool_keeps_its_scrub_age(client):
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    pool = client.get("/api/pools")[0]
    age = mock.datetime.now(mock.timezone.utc) - ph.last_scrub_end(pool)
    # Written as 2 days 9 hours before the file's verdict time.
    assert abs(age.total_seconds() - (2 * 86400 + 9 * 3600)) < 120


def test_state_carries_the_replica_fields(client):
    drive_id = client.ok("POST", "/api/drives", {"replica": "shipped.nvme-wear"})["id"]
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    state = client.get("/api/state")
    (drive,) = state["drives"]
    assert drive["id"] == drive_id
    lab = drive["lab"]
    assert {k: lab[k] for k in ("replica", "name", "kind", "description", "preset")} == {
        "replica": "shipped.nvme-wear", "name": "NVMe, wear", "kind": "nvme",
        "description": "A Samsung 980 PRO 1TB with its available spare down to 15%.", "preset": "nvme",
    }
    assert lab["scenario"] == {"current": "nvme_wear", "options": [
        {"id": "healthy", "label": "Healthy NVMe"}, {"id": "nvme_wear", "label": "NVMe wear"},
        {"id": "media_errors", "label": "Media errors"}, {"id": "critical_warning", "label": "Critical warning"},
    ]}
    (pool,) = state["pools"]
    assert pool["lab"]["replica"] == "shipped.zfs-pool-degraded"
    assert pool["lab"]["scenario"]["current"] == "degraded"
    assert [o["id"] for o in pool["lab"]["scenario"]["options"]] == ["healthy", "degraded", "faulted", "vanished"]
    assert client.get("/mock/state")["pools"] == state["pools"]


@pytest.mark.parametrize("preset, replica, state", [
    ("sata_hdd", "shipped.sata-hdd-healthy", "NO"),
    ("sata_hdd_devstat", "shipped.sata-hdd-devstat-uncorrectables", "NO"),
    ("nvme", "shipped.nvme-wear", "NO"),
    ("sata_ssd", "shipped.sata-ssd", "NO"),
    ("usb_blocked", "shipped.usb-blocked", "UNSUPPORTED"),
])
def test_an_old_preset_is_the_healthy_scenario_of_its_first_file(client, preset, replica, state):
    added = client.ok("POST", "/api/drives", {"preset": preset})
    assert added["replica"] == replica
    assert _drive_state(client, added["id"]) == state
    lab = client.get("/api/state")["drives"][0]["lab"]
    assert lab["preset"] == preset
    if state == "NO":
        assert lab["scenario"]["current"] == "healthy"


def test_preload_with_old_keys_and_file_ids(tmp_path):
    server, client = _start("--data-dir", str(tmp_path), "--preload", "sata_hdd,zfs_pool,shipped.smart-failed")
    try:
        drives = client.get("/api/drives")
        assert [d["serial"] for d in drives] == ["MOCK-SATA-0002", "MOCK-SATA-0001"]
        assert _pool_state(client, "tank") == "NO"
        assert client.get("/api/pools")[0]["state"] == "ONLINE"
    finally:
        _stop(server)


# --- Scenarios --------------------------------------------------------------------------


@pytest.mark.parametrize("kind, file_id", [(k, f) for k, files in SHIPPED_BY_KIND.items() for f in files])
def test_every_scenario_gives_its_verdict_and_healthy_undoes_it(client, kind, file_id):
    added = client.ok("POST", "/api/drives", {"replica": file_id})
    is_pool = added["kind"] == "pool"
    base = f"/api/pools/{added['id']}" if is_pool else f"/api/drives/{added['id']}"

    def state() -> str:
        return _pool_state(client, added["id"]) if is_pool else _drive_state(client, added["id"])

    promised = SCENARIO_STATES[kind]
    # Each scenario from every other one, so none leans on what came before.
    for before in promised:
        for scenario, expected in promised.items():
            client.ok("POST", f"{base}/scenario", {"id": before})
            result = client.ok("POST", f"{base}/scenario", {"id": scenario})
            assert result["scenario"] == scenario
            assert result["pool" if is_pool else "drive"]["lab"]["scenario"]["current"] == scenario
            assert state() == expected, (before, scenario)
    client.ok("POST", f"{base}/scenario", {"id": "healthy"})
    assert state() == "NO"


def test_scenario_details(client):
    drive_id = client.ok("POST", "/api/drives", {"replica": "shipped.sata-hdd-devstat-uncorrectables"})["id"]
    client.ok("POST", f"/api/drives/{drive_id}/scenario", {"id": "healthy"})
    drive = client.get(f"/api/drives/{drive_id}")
    assert drive["device_statistics"]["status"] == "present"
    page4 = next(p for p in drive["device_statistics"]["pages"] if p["number"] == 4)
    assert page4["table"][0]["value"] == 0

    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    faulted = client.ok("POST", "/api/pools/tank/scenario", {"id": "faulted"})["pool"]
    assert faulted["state"] == "FAULTED"
    assert [d["state"] for d in faulted["lab"]["devices"]] == ["UNAVAIL", "UNAVAIL"]
    degraded = client.ok("POST", "/mock/pools/tank/scenario", {"id": "degraded"})["pool"]
    assert [(d["name"], d["state"], d["read"]) for d in degraded["lab"]["devices"]] == [
        ("MOCK0001", "ONLINE", 0), ("MOCK0002", "FAULTED", 12)]
    assert ph.pool_problems(degraded) == [
        "State: DEGRADED", "Read errors: 12", "mirror-0: DEGRADED", "MOCK0002: FAULTED, 12 read errors"]

    code, payload = client.call("POST", f"/api/drives/{drive_id}/scenario", {"id": "nvme_wear"})
    assert code == 400 and payload["error"] == "unknown_scenario"
    assert payload["options"] == ["healthy", "devstat_uncorrectables"]
    assert client.call("POST", "/api/drives/nope/scenario", {"id": "healthy"})[0] == 404
    library = client.get("/api/scenarios")
    assert set(library) == {"ata", "ata_devstat", "nvme", "zfs_pool", "scsi", "unsupported"}
    assert library["scsi"] == [] and library["unsupported"] == []


def test_a_files_own_scenario_options(client, user_dir):
    doc = _load(REPLICAS / "nvme-wear.json")
    doc["name"] = "NVMe, hot"
    doc["scenario"] = {"current": "hot", "options": [
        {"id": "hot", "label": "Running hot", "steps": [{"patch": "smart", "body": {"temperature": 81}}]}]}
    client.ok("POST", "/api/replicas", {"file": doc})
    drive_id = client.ok("POST", "/api/drives", {"replica": "user.nvme-hot"})["id"]
    options = client.get("/api/state")["drives"][0]["lab"]["scenario"]["options"]
    assert [o["id"] for o in options] == ["hot", "healthy", "nvme_wear", "media_errors", "critical_warning"]
    client.ok("POST", f"/api/drives/{drive_id}/scenario", {"id": "healthy"})
    client.ok("POST", f"/api/drives/{drive_id}/scenario", {"id": "hot"})
    log = client.get(f"/api/drives/{drive_id}")["smart_data"]["nvme_smart_health_information_log"]
    assert log["temperature"] == 81


# --- Upload ----------------------------------------------------------------------------


def test_upload_writes_a_valid_file_to_the_user_folder(client, user_dir):
    doc = _load(REPLICAS / "smart-failed.json")
    entry = client.ok("POST", "/api/replicas", {"file": doc})
    assert entry == {
        "id": "user.smart-failed", "name": "SMART failed", "kind": "ata", "description": doc["description"],
        "source": "user", "order": None, "valid": True,
    }
    written = json.loads((user_dir / "smart-failed.json").read_text())
    assert written["origin"] == {"type": "uploaded"}
    assert written["smart"] == doc["smart"]
    # A second upload of the same name gets its own file.
    assert client.ok("POST", "/mock/replicas", {"file": doc})["id"] == "user.smart-failed-2"
    # The file itself, without the envelope, is taken too.
    assert client.ok("POST", "/api/replicas", doc)["id"] == "user.smart-failed-3"
    assert client.get("/api/replicas/user.smart-failed")["origin"] == {"type": "uploaded"}
    assert _drive_state(client, client.ok("POST", "/api/drives", {"replica": "user.smart-failed"})["id"]) == "YES"


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(format="something-else"), "not a replica file"),
    (lambda d: d.update(kind="floppy"), "kind must be one of"),
    (lambda d: d.update(version=2), "unsupported replica version"),
    (lambda d: d.pop("meta"), "needs a meta object"),
    (lambda d: d["devstat"].update(status="sideways"), "devstat.status"),
])
def test_upload_refuses_a_bad_file(client, user_dir, change, message):
    doc = _load(REPLICAS / "sata-hdd-healthy.json")
    change(doc)
    code, payload = client.call("POST", "/api/replicas", {"file": doc})
    assert code == 422 and payload["error"] == "invalid_replica"
    assert message in payload["message"]
    assert list(user_dir.iterdir()) == []


def test_upload_refuses_bad_json_and_big_files(client, user_dir):
    code, _, data = client.raw("POST", "/api/replicas", b"{not json")
    assert code == 422 and json.loads(data)["error"] == "invalid_replica"
    doc = _load(REPLICAS / "sata-hdd-healthy.json")
    doc["description"] = "x" * (mock.MAX_REPLICA_BYTES + 10)
    assert client.call("POST", "/api/replicas", {"file": doc}) == (
        413, {"error": "too_large", "message": "A replica file is at most 1 MB."})
    doc["description"] = "x" * (2 * mock.MAX_REPLICA_BYTES)
    code, _, data = client.raw("POST", "/api/replicas", json.dumps({"file": doc}).encode())
    assert code == 413
    assert client.call("POST", "/api/replicas", {"neither": 1})[0] == 400
    assert list(user_dir.iterdir()) == []


def test_upload_refused_when_the_folder_is_missing_or_read_only(tmp_path, monkeypatch):
    doc = _load(REPLICAS / "nvme-wear.json")
    server, client = _start("--user-replicas", str(tmp_path / "absent"))
    try:
        assert client.get("/api/replicas")["folder"]["status"] == "missing"
        code, payload = client.call("POST", "/api/replicas", {"file": doc})
        assert code == 503 and payload["error"] == "replica_folder_unavailable"
    finally:
        _stop(server)

    folder = tmp_path / "ro"
    folder.mkdir()
    real_open = open

    def read_only_open(path, mode="r", *args, **kwargs):
        if str(path).startswith(str(folder)) and any(m in mode for m in "wax+"):
            raise PermissionError(30, "Read-only file system")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(mock, "open", read_only_open, raising=False)
    server, client = _start("--user-replicas", str(folder))
    try:
        assert client.get("/api/replicas")["folder"] == {"path": str(folder), "status": "read_only"}
        code, payload = client.call("POST", "/api/replicas", {"file": doc})
        assert code == 503 and payload["error"] == "replica_folder_unavailable"
        assert payload["folder"]["status"] == "read_only"
    finally:
        _stop(server)


# --- Save from a real drive -------------------------------------------------------------

FAKE_WWN = {"naa": 5, "oui": 0xE5E5E5, "id": 0x1A2B3C4D5}
FAKE_WWN_TEXT = "5e5e5e51a2b3c4d5"
FAKE_EUI64 = {"oui": 0xE5E5E5, "ext_id": 0x9F8E7D6C5B}
FAKE_NGUID = "e5e5e5e5f00dfeedf00dfeedf00d0001"


def _no_trace(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle.lower() not in text.lower(), needle


def test_save_an_ata_drive_with_device_statistics(client, user_dir):
    payload = agent_drive("ata_devstat_wdc_unc")
    original = payload["serial"]
    payload["smart_data"]["wwn"] = copy.deepcopy(FAKE_WWN)
    payload["smart_data"]["device"]["info_name"] = f"/dev/fixture [wwn {FAKE_WWN_TEXT}]"
    payload["smart_data"]["smartctl"]["argv"] = ["-a", "-j", "/dev/fixture"]
    payload["smart_data"]["local_time"] = {"time_t": 1790000000, "asctime": "a day"}
    state, reasons = _verdict(payload)
    verdict = {"state": state, "severity": "critical", "reasons": reasons, "accepted": [], "rules_version": "v0.8.2"}

    entry = client.ok("POST", "/api/replicas", {"from_drive": payload, "verdict": verdict})
    assert entry["source"] == "user" and entry["kind"] == "ata_devstat"
    assert entry["name"].startswith(f"{payload['model']}, saved ")
    path = user_dir / (entry["id"].split(".", 1)[1] + ".json")
    text = path.read_text()
    _no_trace(text, original, FAKE_WWN_TEXT, '"argv"', '"local_time"')
    doc = json.loads(text)
    assert doc["description"].startswith("Saved from a real drive on ")
    assert doc["description"].endswith("Serial and WWN replaced.")
    assert doc["origin"]["type"] == "saved" and "saved_at" in doc["origin"]
    serial = doc["meta"]["serial"]
    assert serial.startswith("REPLICA-") and len(serial) == len("REPLICA-") + 8
    assert doc["smart"]["serial_number"] == serial
    assert doc["smart"]["wwn"]["naa"] == 5 and doc["smart"]["wwn"]["oui"] == 0
    assert doc["smart"]["ata_smart_attributes"] == payload["smart_data"]["ata_smart_attributes"]
    assert doc["devstat"]["pages"] == payload["device_statistics"]["pages"]
    assert doc["verdict"]["state"] == state and doc["verdict"]["reasons"] == reasons
    assert "accepted" not in doc["verdict"] and "at" in doc["verdict"]

    # The saved file reads the same as the drive it came from.
    added = client.ok("POST", "/api/drives", {"replica": entry["id"]})
    served = client.get(f"/api/drives/{added['id']}")
    assert _verdict(served) == (state, reasons)
    assert served["derived"] == payload["derived"]


def test_save_an_nvme_drive_with_namespaces(client, user_dir):
    payload = agent_drive("nvme_sabrent", status="not_applicable")
    original = payload["serial"]
    smart = payload["smart_data"]
    smart["nvme_ieee_oui_identifier"] = 0xE5E5E5
    smart["nvme_namespaces"] = [{"id": 1, "eui64": copy.deepcopy(FAKE_EUI64), "nguid": FAKE_NGUID},
                                {"id": 2, "eui64": copy.deepcopy(FAKE_EUI64)}]
    smart["device"]["info_name"] = f"/dev/fixture {original}"
    entry = client.ok("POST", "/api/replicas", {"from_drive": payload})
    assert entry["kind"] == "nvme"
    text = (user_dir / (entry["id"].split(".", 1)[1] + ".json")).read_text()
    _no_trace(text, original, FAKE_NGUID, str(FAKE_EUI64["ext_id"]), str(0xE5E5E5))
    doc = json.loads(text)
    assert "verdict" not in doc
    assert doc["smart"]["nvme_ieee_oui_identifier"] == 0
    assert doc["smart"]["nvme_namespaces"][0]["eui64"] == {"oui": 0, "ext_id": 0}
    assert set(doc["smart"]["nvme_namespaces"][0]["nguid"]) == {"0"}
    assert doc["smart"]["device"]["info_name"] == f"/dev/fixture {doc['meta']['serial']}"
    assert doc["smart"]["nvme_smart_health_information_log"] == smart["nvme_smart_health_information_log"]


def test_save_refuses_a_drive_with_no_smart_data(client, user_dir):
    payload = agent_drive("ata_devstat_wdc_unc", status="unavailable", reason="failed")
    payload["smart_data"] = {}
    code, body = client.call("POST", "/api/replicas", {"from_drive": payload})
    assert code == 409 and body["error"] == "unsupported_drive"
    assert client.call("POST", "/api/replicas", {"from_drive": "sda"})[0] == 400
    assert list(user_dir.iterdir()) == []


def test_kind_follows_the_data():
    a = read_fixture("ata_devstat_wdc_unc", "a")
    assert mock.drive_kind(a, {"status": "present"}, "ATA") == "ata_devstat"
    assert mock.drive_kind(a, {"status": "absent"}, "ATA") == "ata"
    assert mock.drive_kind(read_fixture("nvme_sabrent", "a"), {}, "NVMe") == "nvme"
    assert mock.drive_kind(read_fixture("scsi_view", "a"), {}, "SCSI") == "scsi"
    assert mock.drive_kind({}, {}, "ATA") == "unsupported"


# --- The user folder --------------------------------------------------------------------


def test_a_dropped_file_shows_up_and_a_bad_one_is_listed_invalid(client, user_dir):
    ids = {f["id"] for f in client.get("/api/replicas")["files"]}
    assert not any(i.startswith("user.") for i in ids)
    doc = _load(REPLICAS / "nvme-wear.json")
    doc["name"] = "Dropped in over Samba"
    (user_dir / "dropped.json").write_text(json.dumps(doc))
    (user_dir / "notes.json").write_text('{"shopping": ["milk"]}')
    (user_dir / "broken.json").write_text("{")
    (user_dir / "readme.txt").write_text("not listed")
    files = {f["id"]: f for f in client.get("/api/replicas")["files"] if f["source"] == "user"}
    assert files["user.dropped"]["valid"] is True and files["user.dropped"]["name"] == "Dropped in over Samba"
    assert files["user.notes"] == {"id": "user.notes", "source": "user", "valid": False,
                                   "error": 'not a replica file: format must be "smart-sniffer-replica"'}
    assert files["user.broken"]["error"] == "not valid JSON"
    assert set(files) == {"user.dropped", "user.notes", "user.broken"}
    assert client.call("POST", "/api/drives", {"replica": "user.notes"})[0] == 422
    assert client.call("GET", "/api/replicas/user.notes")[0] == 422

    # Edited in place: the next listing reads it again.
    doc["name"] = "Renamed"
    (user_dir / "dropped.json").write_text(json.dumps(doc) + " ")
    files = {f["id"]: f for f in client.get("/api/replicas")["files"]}
    assert files["user.dropped"]["name"] == "Renamed"
    (user_dir / "dropped.json").unlink()
    assert "user.dropped" not in {f["id"] for f in client.get("/api/replicas")["files"]}


# --- Export -----------------------------------------------------------------------------


def _without_time(drive: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in drive.items() if k != "last_updated"}


def test_export_carries_the_edits_and_loads_back_identical(client, user_dir):
    drive_id = client.ok("POST", "/api/drives", {"replica": "shipped.sata-hdd-devstat-uncorrectables"})["id"]
    client.ok("PATCH", f"/api/drives/{drive_id}/smart", {"Current_Pending_Sector": 4, "smart_passed": False})
    client.ok("PATCH", f"/api/drives/{drive_id}/devstat", {"reported_uncorrectable": 7})
    client.ok("PATCH", f"/api/drives/{drive_id}/derived", {"host_writes_tb": 90.5})
    before = client.get(f"/api/drives/{drive_id}")

    code, headers, data = client.raw("GET", f"/mock/drives/{drive_id}/replica")
    assert code == 200
    assert headers["Content-Disposition"] == (
        'attachment; filename="sata-hdd-with-device-statistics-uncorrectables-on-page-4.json"')
    doc = json.loads(data)
    assert doc["origin"] == {"type": "shipped"} and doc["scenario"] == {"current": "devstat_uncorrectables"}
    assert "verdict" not in doc and "order" not in doc and "preset" not in doc
    assert doc["meta"]["serial"] == before["serial"]
    table = {a["name"]: a for a in doc["smart"]["ata_smart_attributes"]["table"]}
    assert table["Current_Pending_Sector"]["raw"]["value"] == 4
    assert doc["smart"]["smart_status"] == {"passed": False}

    with_verdict = client.ok("POST", f"/api/drives/{drive_id}/replica", {"verdict": {"state": "YES"}})
    assert with_verdict["verdict"]["state"] == "YES"

    file_id = client.ok("POST", "/api/replicas", {"file": doc})["id"]
    client.ok("DELETE", f"/api/drives/{drive_id}")
    again = client.ok("POST", "/api/drives", {"replica": file_id})["id"]
    assert again == drive_id
    assert _without_time(client.get(f"/api/drives/{again}")) == _without_time(before)


def test_export_a_pool(client):
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    client.ok("PATCH", "/api/pools/tank", {"errors": "3 data errors, use '-v' for a list"})
    doc = json.loads(client.raw("GET", "/api/pools/tank/replica")[2])
    assert doc["kind"] == "zfs_pool" and doc["pool"]["data_errors"] == 3
    assert not any(k.startswith("_") or k == "vanished" for k in doc["pool"])
    assert mock.validate_replica(doc) == doc
    assert client.call("GET", "/api/pools/nope/replica")[0] == 404
    assert client.call("GET", "/api/drives/nope/replica")[0] == 404


def test_get_one_file(client):
    doc = client.get("/api/replicas/shipped.nvme-wear")
    assert doc == _load(REPLICAS / "nvme-wear.json")
    assert client.call("GET", "/api/replicas/shipped.nope")[0] == 404


# --- Persistence ------------------------------------------------------------------------


def test_data_dir_round_trip_with_replicas(tmp_path):
    server, client = _start("--data-dir", str(tmp_path))
    drive_id = client.ok("POST", "/api/drives", {"replica": "shipped.nvme-wear"})["id"]
    client.ok("POST", f"/api/drives/{drive_id}/scenario", {"id": "media_errors"})
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    client.ok("POST", "/api/pools/tank/scenario", {"id": "vanished"})
    before = client.get("/api/state")
    _stop(server)

    server, client = _start("--data-dir", str(tmp_path), "--preload", "sata_hdd")
    try:
        after = client.get("/api/state")
        assert [d["id"] for d in after["drives"]] == [drive_id]
        assert after["drives"][0]["lab"] == before["drives"][0]["lab"]
        assert after["drives"][0]["lab"]["scenario"]["current"] == "media_errors"
        assert after["pools"][0]["lab"] == before["pools"][0]["lab"]
        assert after["pools"][0]["lab"]["vanished"] is True
        assert _drive_state(client, drive_id) == "YES"
    finally:
        _stop(server)


@pytest.mark.parametrize("folders, usb_file", [
    ((), "shipped.usb-blocked"),
    (("--replicas", str(REPLICAS)), None),
])
def test_a_store_from_the_v080_mock_loads_and_maps_to_files(tmp_path, folders, usb_file):
    """tests/fixtures/mock_v080: what the mock-v080 mock saved after a few edits."""
    old = json.loads(OLD_STORE.read_text())
    (tmp_path / "mock-drives.json").write_text(json.dumps(old))
    server, client = _start("--data-dir", str(tmp_path), *folders)
    try:
        state = client.get("/api/state")
        assert [d["id"] for d in state["drives"]] == old["order"]
        labs = {d["lab"]["preset"]: d["lab"] for d in state["drives"]}
        assert labs["sata_hdd"]["replica"] == "shipped.sata-hdd-healthy"
        assert labs["sata_hdd_devstat"]["replica"] == "shipped.sata-hdd-devstat-uncorrectables"
        assert labs["nvme"]["replica"] == "shipped.nvme-wear"
        assert labs["usb_blocked"]["replica"] == usb_file
        assert labs["usb_blocked"]["kind"] == "unsupported"
        assert all(lab["scenario"]["current"] is None for lab in labs.values())

        # The edits made under the old mock are still there.
        hdd = next(d for d in state["drives"] if d["lab"]["preset"] == "sata_hdd")
        table = {a["name"]: a for a in hdd["smart_data"]["ata_smart_attributes"]["table"]}
        assert table["Reallocated_Sector_Ct"]["raw"]["value"] == 5
        assert labs["sata_hdd_devstat"]["devstat_counts"]["reported_uncorrectable"] == 7
        usb = client.get(f"/api/drives/{old['order'][3]}")
        assert usb["device_statistics"] == {"status": "unavailable", "reason": "failed"}

        (pool,) = state["pools"]
        assert pool["lab"]["replica"] == "shipped.zfs-pool-degraded"
        assert [d["name"] for d in pool["lab"]["devices"]] == ["sda", "sdb"]
        assert _pool_state(client, "tank") == "YES"
        # Scenarios name disks by position, so they work on the old disk names.
        client.ok("POST", "/api/pools/tank/scenario", {"id": "healthy"})
        assert _pool_state(client, "tank") == "NO"
        degraded = client.ok("POST", "/api/pools/tank/scenario", {"id": "degraded"})["pool"]
        assert [d["state"] for d in degraded["lab"]["devices"]] == ["ONLINE", "FAULTED"]
        hdd_id = hdd["id"]
        client.ok("POST", f"/api/drives/{hdd_id}/scenario", {"id": "smart_failed"})
        assert _drive_state(client, hdd_id) == "YES"
    finally:
        _stop(server)
