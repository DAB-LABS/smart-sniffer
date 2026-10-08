"""Filesystems in the mock agent: /api/filesystems, its PATCH route and --extra-fs.

A drive or pool replica may carry the filesystems on it. The mock serves
them as the agent does (agent/filesystem.go: a bare list of FilesystemInfo,
advertised in /api/health only while there is one), which is what the
integration's coordinator stores under FILESYSTEMS_KEY.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from custom_components.smart_sniffer.const import FILESYSTEMS_KEY
from custom_components.smart_sniffer.entity_plan import KIND_FILESYSTEM, sensor_specs
from tests.test_mock_agent import mock
from tests.test_mock_replicas import REPLICAS, Client, _start, _stop

REPO = Path(__file__).resolve().parents[1]

# The use_percent and status each shipped file promises, by mountpoint.
PROMISED = {
    "sata-hdd-healthy": {"/": (40.0, "ok"), "/data": (82.0, "ok")},
    "sata-hdd-reallocated": {"/srv/media": (91.0, "ok")},
    "sata-hdd-devstat-uncorrectables": {"/mnt/archive": (0.0, "unavailable")},
    "nvme-wear": {"/": (97.0, "ok"), "/home": (30.0, "ok")},
    "zfs-pool-degraded": {"/tank": (21.0, "ok"), "/tank/media": (88.0, "ok"), "/tank/backups": (55.0, "ok")},
    "smart-failed": {"/mnt/backup": (47.0, "ok")},
}


def _agent_keys() -> list[str]:
    """The JSON keys of the agent's FilesystemInfo, in order."""
    source = (REPO / "agent" / "filesystem.go").read_text()
    struct = source.split("type FilesystemInfo struct {", 1)[1].split("}", 1)[0]
    return re.findall(r'json:"([a-z_]+)"', struct)


@pytest.fixture
def started(tmp_path):
    servers = []

    def start(*args: str) -> Client:
        server, client = _start("--data-dir", str(tmp_path / f"data{len(servers)}"), *args)
        servers.append(server)
        return client

    yield start
    for server in servers:
        _stop(server)


def _by_mount(items: list[dict]) -> dict[str, dict]:
    return {fs["mountpoint"]: fs for fs in items}


# --- The files ----------------------------------------------------------------------


@pytest.mark.parametrize("stem", sorted(PROMISED))
def test_each_shipped_file_carries_the_promised_filesystems(stem):
    doc = mock.validate_replica(json.loads((REPLICAS / f"{stem}.json").read_text()))
    root = doc["pool"]["name"] if doc["kind"] == "zfs_pool" else doc["meta"]["device_path"]
    served = {}
    for item in doc["filesystems"]:
        assert list(item) == _agent_keys()
        assert item["device"].startswith(root)
        if doc["kind"] == "zfs_pool":
            assert item["fstype"] == "zfs"
        served[item["mountpoint"]] = mock.fs_payload(mock._fs_record(item))
        # The file is written in the agent's shape, the bytes kept for later.
        assert item == mock.fs_payload(mock._fs_record(item), stored=True)
    assert {m: (fs["use_percent"], fs["status"]) for m, fs in served.items()} == PROMISED[stem]


def test_a_file_whose_filesystem_is_on_another_device_is_refused():
    doc = json.loads((REPLICAS / "smart-failed.json").read_text())
    bad = copy.deepcopy(doc)
    bad["filesystems"][0]["device"] = "/dev/sda1"
    with pytest.raises(mock.InvalidReplica, match="device must be /dev/sdx"):
        mock.validate_replica(bad)
    pool = json.loads((REPLICAS / "zfs-pool-degraded.json").read_text())
    pool["filesystems"][1]["device"] = "tankmedia"
    with pytest.raises(mock.InvalidReplica, match="tank/<dataset>"):
        mock.validate_replica(pool)
    over = copy.deepcopy(doc)
    over["filesystems"][0]["used_bytes"] = over["filesystems"][0]["total_bytes"] + 1
    with pytest.raises(mock.InvalidReplica, match="more than total_bytes"):
        mock.validate_replica(over)


# --- /api/filesystems and /api/health -----------------------------------------------


def test_no_filesystems_means_no_endpoint(started):
    client = started("--preload", "nvme_usb")
    health = client.get("/api/health")
    assert health["filesystems"] == 0
    assert "/api/filesystems" not in health["endpoints"]
    assert client.call("GET", "/api/filesystems")[0] == 404


def test_the_served_list_is_the_agents_bare_list(started):
    client = started("--preload", "sata_hdd,nvme,zfs_pool,sata_hdd_devstat")
    items = client.get("/api/filesystems")
    assert isinstance(items, list)
    assert all(list(fs) == _agent_keys() for fs in items)
    assert client.get("/mock/filesystems") == items
    health = client.get("/api/health")
    assert health["filesystems"] == len(items) == 8
    assert health["endpoints"][-2:] == ["/api/filesystems", "/api/pools"]
    assert len({fs["id"] for fs in items}) == len(items)
    # The old preset keys get the same filesystems as their files.
    assert [fs["mountpoint"] for fs in items] == [
        "/", "/data", "/", "/home", "/mnt/archive", "/tank", "/tank/media", "/tank/backups"]
    archive = _by_mount(items)["/mnt/archive"]
    assert archive["status"] == "unavailable"
    assert (archive["total_bytes"], archive["used_bytes"], archive["use_percent"]) == (0, 0, 0.0)
    # The control state carries them too: all of them, and each record's own.
    state = client.get("/api/state")
    assert state["filesystems"] == items
    nvme = next(d for d in state["drives"] if d["protocol"] == "NVMe")
    assert [fs["mountpoint"] for fs in nvme["lab"]["filesystems"]] == ["/", "/home"]
    assert [fs["device"] for fs in state["pools"][0]["lab"]["filesystems"]] == ["tank", "tank/media", "tank/backups"]


def test_the_integration_reads_the_list(started):
    """The coordinator fetches /api/filesystems only when health counts some,
    and stores the list as is under FILESYSTEMS_KEY: one usage sensor each."""
    client = started("--preload", "nvme,zfs_pool", "--extra-fs")
    assert client.get("/api/health").get("filesystems", 0) > 0
    items = client.get("/api/filesystems")
    specs = sensor_specs({FILESYSTEMS_KEY: items}, "entry", [], {})
    usage = [s for s in specs if s.kind == KIND_FILESYSTEM]
    assert [s.unique_id for s in usage] == [f"entry_{fs['id']}_usage" for fs in items]
    assert {s.device for s in usage} == {"entry_filesystems"}


def test_a_second_copy_gets_its_own_ids_and_a_renamed_pool_its_datasets(started):
    client = started()
    client.ok("POST", "/api/drives", {"replica": "shipped.nvme-wear"})
    client.ok("POST", "/api/drives", {"replica": "shipped.nvme-wear"})
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    client.ok("POST", "/api/pools", {"replica": "shipped.zfs-pool-degraded"})
    items = client.get("/api/filesystems")
    assert len(items) == 10 and len({fs["id"] for fs in items}) == 10
    assert [fs["device"] for fs in items[-3:]] == ["tank2", "tank2/media", "tank2/backups"]


def test_a_vanished_pools_datasets_read_unavailable(started):
    client = started("--preload", "zfs_pool")
    client.ok("POST", "/api/pools/tank/vanish")
    assert {fs["status"] for fs in client.get("/api/filesystems")} == {"unavailable"}
    client.ok("POST", "/api/pools/tank/restore")
    assert {fs["status"] for fs in client.get("/api/filesystems")} == {"ok"}


# --- PATCH /api/filesystems/{id} ----------------------------------------------------


def test_patch_used_bytes_percent_and_status(started):
    client = started("--preload", "sata_hdd")
    data = _by_mount(client.get("/api/filesystems"))["/data"]
    path = f"/api/filesystems/{data['id']}"

    got = client.ok("PATCH", path, {"used_bytes": 1_000_000_000_000})["filesystem"]
    assert got["used_bytes"] == 1_000_000_000_000
    assert got["available_bytes"] == 700_000_000_000
    assert got["use_percent"] == 58.8

    got = client.ok("PATCH", "/mock/filesystems/" + data["id"], {"use_percent": 95})["filesystem"]
    assert (got["used_bytes"], got["use_percent"]) == (1_615_000_000_000, 95.0)

    got = client.ok("PATCH", path, {"status": "unavailable"})["filesystem"]
    assert got["status"] == "unavailable" and got["used_bytes"] == 0 and got["use_percent"] == 0.0
    got = client.ok("PATCH", path, {"status": "ok"})["filesystem"]
    assert (got["status"], got["use_percent"]) == ("ok", 95.0)
    assert _by_mount(client.get("/api/filesystems"))["/data"] == got


@pytest.mark.parametrize("body, text", [
    ({"used_bytes": 1, "use_percent": 2}, "not both"),
    ({"used_bytes": 10**15}, "not be more than total_bytes"),
    ({"used_bytes": -1}, "non-negative"),
    ({"use_percent": 101}, "from 0 to 100"),
    ({"use_percent": True}, "from 0 to 100"),
    ({"status": "gone"}, "status must be one of ok, unavailable"),
    ({"total_bytes": 5}, "unknown keys"),
])
def test_patch_refuses_a_bad_body_and_changes_nothing(started, body, text):
    client = started("--preload", "sata_hdd")
    before = client.get("/api/filesystems")
    code, payload = client.call("PATCH", f"/api/filesystems/{before[1]['id']}", body)
    assert code == 400 and text in payload["error"]
    assert client.get("/api/filesystems") == before


def test_patch_an_unknown_filesystem(started):
    client = started("--preload", "sata_hdd")
    assert client.call("PATCH", "/api/filesystems/fs-nope", {"used_bytes": 1})[0] == 404


def test_a_patch_is_kept_across_a_restart_and_in_an_export(tmp_path):
    server, client = _start("--data-dir", str(tmp_path), "--preload", "nvme")
    root = _by_mount(client.get("/api/filesystems"))["/"]
    client.ok("PATCH", f"/api/filesystems/{root['id']}", {"use_percent": 50, "status": "unavailable"})
    drive_id = client.get("/api/drives")[0]["id"]
    doc = json.loads(client.raw("GET", f"/api/drives/{drive_id}/replica")[2])
    _stop(server)
    exported = _by_mount(doc["filesystems"])["/"]
    assert (exported["status"], exported["use_percent"]) == ("unavailable", 50.0)
    assert mock.validate_replica(doc) == doc

    server, client = _start("--data-dir", str(tmp_path))
    try:
        again = _by_mount(client.get("/api/filesystems"))["/"]
        assert again["status"] == "unavailable"
        got = client.ok("PATCH", f"/api/filesystems/{root['id']}", {"status": "ok"})["filesystem"]
        assert got["use_percent"] == 50.0
    finally:
        _stop(server)


# --- --extra-fs -------------------------------------------------------------------------


def test_preload_alone_has_no_extra_filesystem(started):
    client = started("--preload", "nvme")
    assert "/mnt/nas" not in _by_mount(client.get("/api/filesystems"))


def test_extra_fs_adds_the_nas_share(started):
    client = started("--extra-fs")
    health = client.get("/api/health")
    assert health["drives"] == 0 and health["filesystems"] == 1
    assert "/api/filesystems" in health["endpoints"]
    [nas] = client.get("/api/filesystems")
    assert (nas["mountpoint"], nas["device"], nas["fstype"], nas["status"]) == (
        "/mnt/nas", "nas:/export", "nfs", "ok")
    assert nas["use_percent"] == 64.0
    got = client.ok("PATCH", f"/api/filesystems/{nas['id']}", {"use_percent": 99.5})["filesystem"]
    assert got["use_percent"] == 99.5


def test_extra_fs_comes_after_the_drives(started):
    client = started("--preload", "sata_hdd", "--extra-fs")
    assert [fs["mountpoint"] for fs in client.get("/api/filesystems")] == ["/", "/data", "/mnt/nas"]
