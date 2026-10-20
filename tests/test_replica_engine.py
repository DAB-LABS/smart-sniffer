"""The Replica machine's engine (replica_engine.py), without Home Assistant.

- The payloads: for every seed file (and a board of four) the engine serves
  what tools/mock-agent.py served over HTTP for the same files, recorded
  before the engine was written (tests/fixtures/replica/mock_payloads.json,
  record_mock_payloads.py). Only what must differ is mapped: each pick's
  own drive id, serial and filesystem ids, and the times.
- The copy: the payload helpers are the mock agent's own, unchanged.
- Drift: power-on hours and temperature move; no verdict ever does.
- Ids: every pick has its own, never the file's serial.
- Files: JSON only, checked, at most 1 MB, names made here, never a path
  from outside; the folder caps; the drive and pool caps.
"""

from __future__ import annotations

import ast
import copy
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "custom_components" / "smart_sniffer" / "replicas"
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "replica" / "mock_payloads.json").read_text())

eng = importlib.import_module("custom_components.smart_sniffer.replica_engine")
panel_view = importlib.import_module("custom_components.smart_sniffer.panel_view")
devstat = importlib.import_module("custom_components.smart_sniffer.devstat")

ENTRY = "01ENTRYABCDEFGHJKMNPQRSTVW"
NOW = 1_800_000_000.0


def _mock_source() -> str:
    return (ROOT / "tools" / "mock-agent.py").read_text()


def _lib(tmp_path=None, **folders):
    paths = {"seed": str(SEED)}
    if tmp_path is not None:
        paths["user"] = str(tmp_path / "user")
    paths.update(folders)
    return eng.ReplicaLibrary(paths)


def _picks(lib, files, now=NOW):
    picks = []
    for f in files:
        picks.append(eng.new_pick(lib.find(f), picks, ENTRY, now=now))
    return picks


# ── The seed set ───────────────────────────────────────────────────────────


def test_every_seed_file_is_valid_and_renders():
    lib = _lib()
    listing = lib.listing()
    seeds = [f for f in listing["files"] if f["source"] == "seed"]
    assert len(seeds) == len(list(SEED.glob("*.json"))) >= 10
    assert all(f["valid"] for f in seeds), [f for f in seeds if not f["valid"]]
    for f in seeds:
        doc = json.loads((SEED / f"{f['id'][5:]}.json").read_text())
        assert "scenario" not in doc and "preset" not in doc  # no scenarios (owner)
        assert doc["origin"] == {"type": "shipped"}
        snap = eng.render(_picks(lib, [f["id"]]), lib, agent_id="replica-x", started_at=0, now=NOW)
        assert len(snap["drives"]) + len(snap["pools"]) == 1 and not snap["missing"]


def test_the_seed_set_is_a_modest_mix():
    kinds = [json.loads(p.read_text())["kind"] for p in SEED.glob("*.json")]
    # Every SATA seed carries Device Statistics (v0.9 Details shows them).
    assert {"ata_devstat", "nvme", "zfs_pool", "unsupported"} <= set(kinds)
    assert "ata" not in kinds
    assert len(kinds) <= 12


# ── Payloads: the mock agent's, for the same files ─────────────────────────


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_payloads_match_the_mock_agent(case):
    golden = GOLDEN[case]
    lib = _lib()
    files = [f.replace("shipped.", "seed.") for f in golden["files"]]
    snap = eng.render(_picks(lib, files), lib, agent_id="replica-golden-0001",
                      started_at=NOW, now=NOW, drift=False)
    # What must differ: each pick's identity, and times.
    names: dict[str, str] = {}
    for ours, theirs in zip(snap["drives"], golden["drives"], strict=True):
        names[ours["id"]] = theirs["id"]
        names[ours["serial"]] = theirs["serial"]
    want_fs = [] if "_status" in golden["filesystems"] else golden["filesystems"]
    for ours, theirs in zip(snap["filesystems"], want_fs, strict=True):
        names[ours["id"]] = theirs["id"]
        names[ours["uuid"]] = theirs["uuid"]
    timed = {"last_updated", "uptime_seconds", "port", "last_scrub_end"}

    def norm(value):
        if isinstance(value, dict):
            return {k: norm(v) for k, v in value.items() if k not in timed}
        if isinstance(value, list):
            return [norm(v) for v in value]
        if isinstance(value, str):
            return names.get(value, value)
        return value

    assert norm(snap["health"]) == norm(golden["health"])
    assert norm(snap["drives"]) == norm(golden["drives"])
    for ours, theirs in zip(snap["drives"], golden["drives"], strict=True):
        assert norm(snap["drive"][ours["id"]]) == norm(golden["drive"][theirs["id"]])
    want_pools = [] if "_status" in golden["pools"] else golden["pools"]
    assert norm(snap["filesystems"]) == norm(want_fs)
    assert norm(snap["pools"]) == norm(want_pools)
    # The scrub time is an age, as the mock serves it.
    for ours, theirs in zip(snap["pools"], want_pools, strict=True):
        assert (ours["last_scrub_end"] is None) == (theirs["last_scrub_end"] is None)


COPIED = ("_rfc3339", "_parse_uint", "_num_field", "_attr_table", "_power_on_hours",
          "_devstat_volume", "_vendor_volume", "_nvme_volume", "_protocol", "derive_volumes",
          "_mirror_state", "pool_payload", "_fs_percent", "fs_payload", "_natural_from_file",
          "_parse_time", "_WRITE_SPEC", "_READ_SPEC", "_SPECS", "NVME_DATA_UNIT", "_RATE_BOUND",
          "_MIN_POH_FOR_BOUND", "_KIOXIA_EXCERIA", "_U64")


def _defs(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = ast.dump(node.value)
    return out


def test_payload_helpers_are_the_mock_agents_own():
    ours = _defs((ROOT / "custom_components" / "smart_sniffer" / "replica_engine.py").read_text())
    theirs = _defs(_mock_source())
    for name in COPIED:
        assert ours[name] == theirs[name], name


# ── Drift ──────────────────────────────────────────────────────────────────


def _judge(payload):
    drive = copy.deepcopy(payload)
    drive[devstat.DEVSTAT_KEY], _ = devstat.merge_devstat(drive, None)
    return panel_view.evaluate(drive, {}, "x")


def test_drift_moves_hours_and_temperature_never_a_verdict():
    lib = _lib()
    files = [f["id"] for f in lib.listing()["files"] if f["valid"] and not f["pool"]]
    picks = _picks(lib, files, now=NOW)
    base = eng.render(picks, lib, agent_id="a", started_at=NOW, now=NOW, drift=False)
    moved_hours = moved_temp = False
    for hours in (0, 1, 5, 100, 5000, 50000):
        for step in range(6):
            now = NOW + hours * 3600 + step * eng.DRIFT_STEP
            snap = eng.render(picks, lib, agent_id="a", started_at=NOW, now=now)
            for drive_id, payload in snap["drive"].items():
                was = base["drive"][drive_id]
                a, b = _judge(was), _judge(payload)
                assert (a["state"], a["reasons"]) == (b["state"], b["reasons"]), drive_id
                sd, sw = payload["smart_data"], was["smart_data"]
                if isinstance(sw.get("power_on_time"), dict):
                    assert sd["power_on_time"]["hours"] == sw["power_on_time"]["hours"] + hours
                    moved_hours |= hours > 0
                if isinstance(sw.get("temperature"), dict):
                    diff = sd["temperature"]["current"] - sw["temperature"]["current"]
                    assert abs(diff) <= 1
                    moved_temp |= diff != 0
                table = {a["id"]: a for a in (sd.get("ata_smart_attributes") or {}).get("table", [])}
                if 9 in table and isinstance(sw.get("power_on_time"), dict):
                    assert table[9]["raw"]["value"] == sd["power_on_time"]["hours"]
                    assert table[9]["raw"]["string"].startswith(str(table[9]["raw"]["value"]))
                # Device Statistics follow: power-on hours and the temperature.
                ds = payload["device_statistics"]
                if ds["status"] == "present" and 9 in table:
                    assert _devstat(ds, 1, 16) == table[9]["raw"]["value"]
                    temp = table.get(194) or table.get(190)
                    assert _devstat(ds, 5, 8) == temp["raw"]["value"] & 0xFF
    assert moved_hours and moved_temp


def _devstat(block, page, offset):
    for p in block.get("pages") or []:
        if p["number"] == page:
            for e in p["table"]:
                if e["offset"] == offset:
                    return e["value"]
    return None


_SATA_SEEDS = sorted(p.stem for p in SEED.glob("sata-*.json"))


@pytest.mark.parametrize("stem", _SATA_SEEDS)
def test_sata_seeds_carry_device_statistics_that_agree_with_their_attributes(stem):
    doc = json.loads((SEED / f"{stem}.json").read_text())
    ds = doc["devstat"]
    assert doc["kind"] == "ata_devstat" and ds["status"] == "present" and ds["complete"] is True
    assert all(e["flags"]["valid"] is True for p in ds["pages"] for e in p["table"])
    smart = doc["smart"]
    attrs = {a["id"]: a["raw"]["value"] & (0xFF if a["id"] in (190, 194) else 2 ** 48 - 1)
             for a in smart["ata_smart_attributes"]["table"]}
    pages = {p["number"] for p in ds["pages"]}
    for attr_id, page, offset in ((9, 1, 16), (12, 1, 8), (241, 1, 24), (242, 1, 40), (5, 3, 32),
                                  (197, 3, 56), (187, 4, 8), (194, 5, 8), (199, 6, 24)):
        if attr_id in attrs and page in pages:
            assert _devstat(ds, page, offset) == attrs[attr_id], (stem, attr_id)
    if "ssd" in stem:
        # An SSD: page 7, its endurance used as its wear attribute says.
        wear = next(a for a in smart["ata_smart_attributes"]["table"] if a["name"] == "Wear_Leveling_Count")
        assert 3 not in pages and _devstat(ds, 7, 8) == 100 - wear["value"]
    else:
        assert 7 not in pages and {1, 3, 4, 5, 6} <= pages
    # What the Details top shows: firmware, capacity, the SATA link.
    assert smart["firmware_version"] and smart["user_capacity"]["bytes"] > 0
    assert smart["interface_speed"]["current"]["string"] == "6.0 Gb/s"
    # Never a real serial or WWN.
    assert doc["meta"]["serial"].startswith("MOCK-") and "wwn" not in json.dumps(doc).lower()


def test_failing_seeds_say_the_same_in_device_statistics():
    failed = json.loads((SEED / "sata-hdd-smart-failed.json").read_text())["devstat"]
    assert (_devstat(failed, 3, 32), _devstat(failed, 3, 56), _devstat(failed, 4, 8)) == (24, 3, 1)
    assert _devstat(failed, 3, 40) > 0  # read recovery attempts
    unc = json.loads((SEED / "sata-hdd-devstat-uncorrectables.json").read_text())
    assert _devstat(unc["devstat"], 4, 8) == 18
    assert not any(a["id"] == 187 for a in unc["smart"]["ata_smart_attributes"]["table"])


def test_drift_is_the_same_for_every_read_in_one_step():
    lib = _lib()
    picks = _picks(lib, ["seed.sata-hdd-healthy"])
    a = eng.render(picks, lib, agent_id="a", started_at=NOW, now=NOW + 7200)
    b = eng.render(picks, lib, agent_id="a", started_at=NOW, now=NOW + 7201)
    da, db = next(iter(a["drive"].values())), next(iter(b["drive"].values()))
    assert da["smart_data"] == db["smart_data"]


# ── Ids: namespaced per pick ───────────────────────────────────────────────


def test_each_pick_has_its_own_ids_never_the_files_serial():
    lib = _lib()
    picks = _picks(lib, ["seed.sata-hdd-healthy", "seed.sata-hdd-healthy",
                         "seed.zfs-pool-degraded", "seed.zfs-pool-degraded"])
    snap = eng.render(picks, lib, agent_id="a", started_at=NOW, now=NOW)
    file_serial = json.loads((SEED / "sata-hdd-healthy.json").read_text())["meta"]["serial"]
    ids = [d["id"] for d in snap["drives"]]
    serials = [d["serial"] for d in snap["drives"]]
    assert len(set(ids)) == 2 and len(set(serials)) == 2
    assert all(i.startswith(f"replica-{ENTRY[:8].lower()}-") for i in ids)
    assert file_serial not in serials and file_serial not in json.dumps(snap["drive"])
    assert len({d["device_path"] for d in snap["drives"]}) == 2
    assert [p["name"] for p in snap["pools"]] == ["tank", "tank2"]
    fs_ids = [f["id"] for f in snap["filesystems"]]
    assert len(fs_ids) == len(set(fs_ids))
    # Each filesystem sits on its own pick's drive or pool.
    devices = {d["device_path"] for d in snap["drives"]} | {p["name"] for p in snap["pools"]}
    for fs in snap["filesystems"]:
        assert any(fs["device"] == d or fs["device"].startswith(d) for d in devices)
    assert set(snap["cards"]) == set(serials) | {"pool:tank", "pool:tank2"}


def test_free_device_paths():
    assert eng._free_path("/dev/sda", {"/dev/sda"}) == "/dev/sdb"
    assert eng._free_path("/dev/nvme0n1", {"/dev/nvme0n1", "/dev/nvme1n1"}) == "/dev/nvme2n1"
    assert eng._free_path("/dev/disk", {"/dev/disk"}) == "/dev/disk-2"
    assert eng._free_path("", set()) == "/dev/replica-2"


# ── Caps ───────────────────────────────────────────────────────────────────


def test_twelve_drives_and_four_pools_at_most():
    lib = _lib()
    picks = _picks(lib, ["seed.sata-hdd-healthy"] * 12 + ["seed.zfs-pool-healthy"] * 4)
    for file_id in ("seed.nvme-wear", "seed.zfs-pool-degraded"):
        with pytest.raises(eng.ReplicaError) as err:
            eng.new_pick(lib.find(file_id), picks, ENTRY)
        assert err.value.code == "replica_cap"
    assert eng.pick_counts(picks) == (12, 4)


# ── Files: checked, never run, names made here ─────────────────────────────


def _doc():
    return json.loads((SEED / "sata-hdd-healthy.json").read_text())


@pytest.mark.parametrize(("raw", "code"), [
    (b"x" * (eng.MAX_REPLICA_BYTES + 1), "too_large"),
    (b"\xff\xfe{}", "invalid_replica"),
    (b"{not json", "invalid_replica"),
    (b"[" * 100000 + b"]" * 100000, "invalid_replica"),
    (b'{"format": "smart-sniffer-replica", "version": NaN}', "invalid_replica"),
    (b'{"n": ' + b"9" * 5000 + b"}", "invalid_replica"),
    (b'{"format": "something-else"}', "invalid_replica"),
    (b"null", "invalid_replica"),
])
def test_bad_files_are_refused(raw, code):
    with pytest.raises(eng.ReplicaError) as err:
        eng.parse_document(raw)
    assert err.value.code == code


def test_a_file_with_odd_keys_is_only_data():
    doc = _doc()
    doc["__proto__"] = {"polluted": True}
    doc["constructor"] = "x"
    doc["name"] = "../../etc/passwd <script>"
    parsed = eng.parse_document(json.dumps(doc).encode())
    assert parsed["__proto__"] == {"polluted": True}


def test_write_user_names_files_itself_atomically(tmp_path):
    lib = _lib(tmp_path)
    doc = _doc()
    doc["name"] = "../../etc/passwd <script>"
    first = lib.write_user(copy.deepcopy(doc))
    second = lib.write_user(copy.deepcopy(doc))
    assert first["id"] == "user.etc-passwd-script" and second["id"] == "user.etc-passwd-script-2"
    names = sorted(os.listdir(tmp_path / "user"))
    assert names == ["etc-passwd-script-2.json", "etc-passwd-script.json"]  # no temp file left
    assert lib.find("user.etc-passwd-script")["doc"]["name"] == doc["name"]


def test_ids_are_looked_up_never_paths(tmp_path):
    lib = _lib(tmp_path)
    for bad in ("../seed.sata-hdd-healthy", "seed./etc/passwd", "user.", "/etc/passwd", 7, None):
        assert lib.find(bad) is None


def test_the_library_skips_links_hidden_and_odd_names(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "good.json").write_text(json.dumps(_doc()))
    (user / ".hidden.json").write_text(json.dumps(_doc()))
    (user / "sp ace.json").write_text(json.dumps(_doc()))
    (user / "notes.txt").write_text("x")
    (user / "link.json").symlink_to(SEED / "nvme-wear.json")
    (user / "big.json").write_bytes(b" " * (eng.MAX_REPLICA_BYTES + 1))
    (user / "bad.json").write_text("{")
    files = {f["id"]: f for f in _lib(tmp_path).listing()["files"] if f["source"] == "user"}
    assert set(files) == {"user.good", "user.big", "user.bad"}
    assert files["user.big"]["reason"] == "too_large" and not files["user.big"]["valid"]
    assert files["user.bad"]["reason"] == "invalid"


def test_a_hand_edit_is_read_again(tmp_path):
    lib = _lib(tmp_path)
    entry = lib.write_user(_doc())
    path = tmp_path / "user" / f"{entry['id'][5:]}.json"
    doc = json.loads(path.read_text())
    doc["name"] = "Edited by hand"
    path.write_text(json.dumps(doc))
    os.utime(path, ns=(1, 10**18))
    assert lib.find(entry["id"])["doc"]["name"] == "Edited by hand"


def test_folder_caps_and_states(tmp_path, monkeypatch):
    lib = _lib(tmp_path)
    assert lib.listing()["folders"]["user"] == "missing"
    monkeypatch.setattr(eng, "MAX_FOLDER_FILES", 2)
    lib.write_user(_doc())
    lib.write_user(_doc())
    with pytest.raises(eng.ReplicaError) as err:
        lib.write_user(_doc())
    assert err.value.code == "replica_folder_full"
    assert lib.listing()["folders"]["user"] == "full"
    none = eng.ReplicaLibrary({"seed": str(SEED)})
    with pytest.raises(eng.ReplicaError) as err:
        none.write_user(_doc())
    assert err.value.code == "replica_folder_unavailable"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_read_only_folder(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    user.chmod(0o555)
    try:
        lib = _lib(tmp_path)
        assert lib.listing()["folders"]["user"] == "read_only"
        with pytest.raises(eng.ReplicaError):
            lib.write_user(_doc())
    finally:
        user.chmod(0o755)


def test_the_drop_folder_is_listed_and_never_written(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "lab.json").write_text(json.dumps(_doc()))
    lib = _lib(tmp_path, share=str(share))
    listing = lib.listing()
    assert [f["id"] for f in listing["files"] if f["source"] == "share"] == ["share.lab"]
    lib.write_user(_doc())
    assert os.listdir(share) == ["lab.json"]


def test_a_missing_file_is_reported_not_served(tmp_path):
    lib = _lib(tmp_path)
    entry = lib.write_user(_doc())
    picks = _picks(lib, [entry["id"], "seed.nvme-wear"])
    (tmp_path / "user" / f"{entry['id'][5:]}.json").unlink()
    snap = eng.render(picks, lib, agent_id="a", started_at=NOW, now=NOW)
    assert len(snap["drives"]) == 1 and [m["file_id"] for m in snap["missing"]] == [entry["id"]]


def test_clean_picks_keeps_only_whole_picks():
    good = {"pick_id": "a" * 32, "file_id": "seed.x", "added_at": 1.0, "kind": "drive",
            "drive_id": "replica-x", "serial": "REPLICA-1", "device_path": "/dev/sda"}
    pool = {"pick_id": "b" * 32, "file_id": "seed.y", "added_at": 1.0, "kind": "pool",
            "pool_name": "tank"}
    bad = [{"pick_id": 1}, {**good, "kind": "disk"}, {**good, "added_at": True}, "x", good]
    assert eng.clean_picks([good, pool, *bad]) == [good, pool]
    assert eng.clean_picks(None) == []


def test_serve_answers_like_the_agent():
    lib = _lib()
    snap = eng.render(_picks(lib, ["seed.nvme-wear"]), lib, agent_id="a", started_at=NOW, now=NOW)
    assert eng.serve(snap, "/api/pools")[0] == 404  # no pool: not advertised
    assert "/api/pools" not in snap["health"]["endpoints"]
    assert eng.serve(snap, "/api/drives/nope")[0] == 404
    assert snap["health"]["mock"] is True and "hostname" not in snap["health"]


def test_a_saved_pool_reads_the_same(tmp_path):
    lib = _lib()
    pool = eng.render(_picks(lib, ["seed.zfs-pool-degraded"]), lib, agent_id="a",
                      started_at=NOW, now=NOW)["pools"][0]
    doc = eng.pool_document(pool)
    again = eng.render(_picks(eng.ReplicaLibrary({"user": str(tmp_path)}), []), lib,
                       agent_id="a", started_at=NOW, now=NOW)
    assert again["pools"] == []
    served = eng.pool_payload(doc["pool"])
    assert panel_view.pool_verdict(served) == panel_view.pool_verdict(pool)
    healthy = eng.pool_document({"name": "fast", "state": "ONLINE", "problem_vdevs": []})
    assert healthy["pool"]["devices"][0]["state"] == "ONLINE"


def test_the_mock_agent_is_unchanged_and_still_loads():
    spec = importlib.util.spec_from_file_location("ss_mock_engine_check", ROOT / "tools" / "mock-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ss_mock_engine_check"] = module
    spec.loader.exec_module(module)
    assert module.VERSION == eng.VERSION


# ── Raw edits and deleting your own files (v0.9 round 3) ───────────────────


def _seed(name):
    return json.loads((SEED / f"{name}.json").read_text())


def test_edit_document_changes_raw_values_and_what_follows_them():
    doc = _seed("sata-hdd-reallocated")
    out = eng.edit_document(doc, {"5": 0, "9": 100, "194": 47, "241": 123456789}, {})
    attrs = {a["id"]: a for a in out["smart"]["ata_smart_attributes"]["table"]}
    assert attrs[5]["raw"] == {"value": 0, "string": "0"}
    assert attrs[9]["raw"]["value"] == 100 and out["smart"]["power_on_time"]["hours"] == 100
    assert attrs[194]["raw"]["value"] & 0xFF == 47 and out["smart"]["temperature"]["current"] == 47
    assert attrs[241]["raw"]["value"] == 123456789
    # The input is never changed; identity and the rest stay.
    assert {a["id"]: a for a in doc["smart"]["ata_smart_attributes"]["table"]}[5]["raw"]["value"] == 8
    assert out["meta"] == doc["meta"] and out["name"] == doc["name"]
    nv = eng.edit_document(_seed("nvme-wear"), {}, {"available_spare": 90, "temperature": 30})
    log = nv["smart"]["nvme_smart_health_information_log"]
    assert (log["available_spare"], log["temperature"]) == (90, 30)


@pytest.mark.parametrize("ata,nvme", [
    ({"7": 1}, {}), ({"194": 200}, {}), ({"5": -1}, {}), ({"5": True}, {}), ({"5": 2 ** 48}, {}),
    ({"x": 1}, {}), ({}, {"media_errors": 1}),
])
def test_edit_document_refuses_bad_values(ata, nvme):
    with pytest.raises(eng.ReplicaError) as err:
        eng.edit_document(_seed("sata-hdd-healthy"), ata, nvme)
    assert err.value.code == "bad_value"


def test_edit_document_changes_device_statistics_and_attributes_carry_over():
    unc = eng.edit_document(_seed("sata-hdd-devstat-uncorrectables"), {}, {}, {"4:8": 0})
    assert _devstat(unc["devstat"], 4, 8) == 0
    # An attribute edit carries over to the entry that reads the same thing.
    out = eng.edit_document(_seed("sata-hdd-reallocated"), {"5": 30, "9": 25000, "194": 44}, {})
    assert (_devstat(out["devstat"], 3, 32), _devstat(out["devstat"], 1, 16),
            _devstat(out["devstat"], 5, 8)) == (30, 25000, 44)
    # A Device Statistics value given for the same entry wins.
    both = eng.edit_document(_seed("sata-hdd-reallocated"), {"5": 30}, {}, {"3:32": 2})
    assert _devstat(both["devstat"], 3, 32) == 2
    ssd = eng.edit_document(_seed("sata-ssd-healthy"), {}, {}, {"7:8": 97})
    assert _devstat(ssd["devstat"], 7, 8) == 97


@pytest.mark.parametrize("seed,devstat", [
    ("sata-hdd-healthy", {"1:16": 5}),       # not one of the three
    ("sata-hdd-healthy", {"7:8": 5}),        # a hard disk has no page 7
    ("sata-ssd-healthy", {"7:8": 256}),      # out of range
    ("sata-ssd-healthy", {"4:8": -1}),
    ("nvme-wear", {"4:8": 1}),               # NVMe: no Device Statistics
])
def test_edit_document_refuses_bad_device_statistics(seed, devstat):
    with pytest.raises(eng.ReplicaError) as err:
        eng.edit_document(_seed(seed), {}, {}, devstat)
    assert err.value.code == "bad_value"


def test_edit_document_never_edits_a_pool():
    with pytest.raises(eng.ReplicaError):
        eng.edit_document(_seed("zfs-pool-degraded"), {"5": 1}, {})


def test_delete_and_update_only_in_the_user_folder(tmp_path):
    user, share = tmp_path / "user", tmp_path / "share"
    user.mkdir()
    share.mkdir()
    doc = _seed("sata-hdd-healthy")
    (share / "lab.json").write_text(json.dumps({**doc, "name": "Lab"}))
    lib = eng.ReplicaLibrary({eng.SEED: str(SEED), eng.USER: str(user), eng.SHARE: str(share)})
    saved = lib.write_user({**doc, "name": "Mine"})
    changed = lib.update_user(saved["id"], eng.edit_document({**doc, "name": "Mine"}, {"5": 3}, {}))
    assert changed["id"] == saved["id"]
    on_disk = json.loads((user / "mine.json").read_text())
    assert on_disk["smart"]["ata_smart_attributes"]["table"][0]["raw"]["value"] == 3
    for file_id in ("seed.sata-hdd-healthy", "share.lab"):
        with pytest.raises(eng.ReplicaError) as err:
            lib.delete_user(file_id)
        assert err.value.code == "not_deletable"
        with pytest.raises(eng.ReplicaError):
            lib.update_user(file_id, doc)
    # A link in the user folder is not listed, so never deleted through it.
    os.symlink(share / "lab.json", user / "link.json")
    assert lib.find("user.link") is None
    assert lib.delete_user(saved["id"])["name"] == "Mine"
    assert not (user / "mine.json").exists() and (share / "lab.json").exists()
    with pytest.raises(eng.ReplicaError) as err:
        lib.delete_user(saved["id"])
    assert err.value.code == "not_found"
