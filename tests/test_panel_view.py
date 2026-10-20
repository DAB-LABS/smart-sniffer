"""The panel's card view, moved from the HA App 0.3.1 proxy (v0.9).

tests/fixtures/panel/app031_golden.json holds what the App 0.3.1
proxy (judge_drive, judge_pool, attach_filesystems) made of every replica
file the repo ships, with the integration's v0.8.5 rules, with and without
threshold overrides. The integration's panel_view must make the same: the
Control Center's cards read the same in the integration as in the App.

Also: the card download (replica_file.py) is the mock agent's own
replica_from_drive, copied; it must still match the tool. No Home
Assistant needed.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "panel" / "app031_golden.json").read_text())

panel_view = importlib.import_module("custom_components.smart_sniffer.panel_view")
replica_file = importlib.import_module("custom_components.smart_sniffer.replica_file")


def _mock():
    name = "ss_mock_agent_view"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "mock-agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", GOLDEN["drives"], ids=lambda c: f"{c['file']}-{len(c['overrides'])}")
def test_drive_card_is_the_app_031_card(case):
    work = copy.deepcopy(case["drive"])
    verdict = panel_view.evaluate(work, case["overrides"])
    view = panel_view.drive_view(work, verdict)
    verdict.pop("rules_version")
    assert json.loads(json.dumps(verdict)) == case["verdict"]
    assert json.loads(json.dumps(view)) == case["view"]


@pytest.mark.parametrize("case", GOLDEN["pools"], ids=lambda c: c["file"])
def test_pool_card_is_the_app_031_card(case):
    verdict = panel_view.pool_verdict(case["pool"])
    assert verdict == case["verdict"]
    assert panel_view.pool_view(case["pool"], verdict, None) == case["view"]


def test_filesystems_go_where_the_app_put_them():
    data = GOLDEN["attach"]
    items = copy.deepcopy(data["items"])
    left = panel_view.attach_filesystems("host", items, copy.deepcopy(data["filesystems"]))
    assert items == data["result_items"]
    assert left == data["leftover"]


def test_build_items_gives_card_ids_and_leaves_the_coordinator_data_alone():
    drive = copy.deepcopy(GOLDEN["drives"][0]["drive"])
    drives = {drive["id"]: drive}
    before = copy.deepcopy(drives)
    items, fs_cards = panel_view.build_items(
        "E1", drives, lambda _d: {}, "0.9.0-dev", [], [{"id": "x", "device": "/dev/zzz9",
                                                         "mountpoint": "/x"}])
    assert drives == before
    assert items[0]["card_id"] == f"E1|{drive['serial']}"
    assert "_devstat" not in items[0]["drive"]
    assert fs_cards[0]["card_id"] == "E1|fs:x"


def test_a_drive_that_could_not_be_read_is_kept_unjudged():
    items, _ = panel_view.build_items("E1", {"sdq": {"id": "sdq", "serial": "S1"}},
                                      lambda _d: {}, None, None, None)
    assert items[0]["verdict"] is None and items[0]["error"] == "not_answering"


def test_removed_cards_and_the_trash_modes():
    response = {"status": "ok", "items": [
        {"type": "drive", "id": "sda", "card_id": "E|S1"},
        {"type": "drive", "id": "sdb", "card_id": "E|S2"},
    ], "filesystem_cards": [], "pools_status": "ok"}
    target = {"devices": {"S1": "d1", "S2": "d2", "S9": "d9"},
              "identifiers": {"S1": "sda", "S2": "sdb_old"}}
    modes = panel_view.ha_removals("E", target, response, set())
    assert modes == {"E|S1": "disable", "E|S2": "delete"}
    assert panel_view.ha_removals("E", target, response, {"E|S1"}) == {"E|S2": "entry"}
    stale = panel_view.mark_stale(response, "unreachable")
    assert all(i["stale"] for i in stale["items"]) and stale["summary"]["header"] == "unknown"


def _normal(doc):
    text = json.dumps(doc, sort_keys=True)
    text = re.sub(r"REPLICA-[0-9A-F]{8}", "REPLICA-X", text)
    text = re.sub(r'"id": \d+', '"id": 0', text)
    return re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "T", text)


def test_card_download_matches_the_mock_agents_replica_file():
    mock = _mock()
    for case in GOLDEN["drives"]:
        if case["overrides"] or case["verdict"]["state"] == "UNSUPPORTED":
            continue
        verdict = {"state": case["verdict"]["state"], "at": "2026-10-18T00:00:00Z"}
        ours = replica_file.replica_from_drive(copy.deepcopy(case["drive"]), verdict)
        theirs = mock.replica_from_drive(copy.deepcopy(case["drive"]), verdict)
        assert _normal(ours) == _normal(theirs)
        assert ours["meta"]["serial"].startswith("REPLICA-")
        assert case["drive"]["serial"] not in json.dumps(ours)
        assert replica_file.file_name(ours).endswith(".json")


def test_replica_file_functions_are_the_tools_own():
    """The copy is verbatim: each function's source equals the tool's."""
    import inspect

    mock = _mock()
    for name in ("replica_from_drive", "validate_replica", "drive_kind", "_file_devstat",
                 "_replace_strings", "_fake_like", "_wwn_text", "_file_verdict", "_file_slug"):
        assert inspect.getsource(getattr(replica_file, name)) == inspect.getsource(getattr(mock, name)), name


def _seed_drive(stem):
    doc = json.loads((ROOT / "custom_components" / "smart_sniffer" / "replicas" / f"{stem}.json").read_text())
    return {"id": "x", "model": doc["meta"]["model"], "serial": doc["meta"]["serial"],
            "protocol": doc["meta"]["protocol"], "smart_data": copy.deepcopy(doc["smart"]),
            "device_statistics": copy.deepcopy(doc["devstat"])}


def test_device_statistics_rows_only_stand_in_for_a_missing_attribute():
    """Why a Samsung 870 EVO shows none of the three Device Statistics rows:
    it reports attribute 5, 187 and a usable wear attribute (177), so the
    integration has nothing to stand in for. The pages are still there for
    the Details table."""
    drive = _seed_drive("sata-ssd-healthy")
    table = drive["smart_data"]["ata_smart_attributes"]["table"]
    # The seed has no 187: its page 4 count stands in for it.
    keys = [r["key"] for r in panel_view.drive_view(drive, None)["rows"]]
    assert "reported_unc_devstat" in keys and "reallocated_devstat" not in keys and "wear_devstat" not in keys
    # As the real drive: with 187, no stand-in at all.
    table.append({"id": 187, "name": "Uncorrectable_Error_Cnt", "value": 100, "worst": 100, "thresh": 0,
                  "raw": {"value": 0, "string": "0"}})
    view = panel_view.drive_view(drive, None)
    assert not [r for r in view["rows"] if r["key"].endswith("_devstat")]
    assert view["devstat"] == "present"
    assert {p["number"] for p in drive["device_statistics"]["pages"]} == {1, 4, 5, 6, 7}
