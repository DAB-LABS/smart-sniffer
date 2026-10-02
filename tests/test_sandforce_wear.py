"""Which attribute the wear reading comes from (GH #55).

SandForce drives report 177 as Wear_Range_Delta: the spread between the most
and least worn blocks, not life remaining. A new drive reads 0 there, and both
wear paths took it as "0 % life left", so the sensor said 100 % used and the
attention check raised an SSD wear warning. Life remaining is in 231
SSD_Life_Left.

The Corsair Force GT fixture is CONSTRUCTED from the reporter's CrystalDiskInfo
dump, not captured with smartctl: the values are his, the attribute names are
the ones smartmontools drivedb.h gives the SandForce entry. See its _comment.

The sensor's name list (ATA_NAME_MAP, in extract.py since v0.8.0, re-imported
by sensor.py) is read from the source with ast and its selection rule
(first row in drive-table order whose name is in the list and which is not a
dead row, reported as 100 - normalized value) is restated here, using the
shared predicate from attention.py. The attention side runs the real module.

Round 2: the reporter's real smartctl JSON (ata_sandforce_force_gt_smartctl,
serial replaced) shows his drive is not in drivedb, so 177 arrives as
Wear_Leveling_Count and 233 as Media_Wearout_Indicator, both 0/0/0 with flags
0. Such a row is not a gauge and is skipped (attention.is_dead_wear_attr).
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"
# ATA_NAME_MAP and _extract_attribute moved here from sensor.py in v0.8.0.
_SENSOR_PY = _COMPONENT / "extract.py"
# The covered-row logic moved from sensor.async_setup_entry in v0.8.0.
_PLAN_PY = _COMPONENT / "entity_plan.py"


def _sensor_wear_names() -> list[str]:
    tree = ast.parse(_SENSOR_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == "ATA_NAME_MAP":
            return ast.literal_eval(node.value)["wear_leveling_count"]
    raise AssertionError("ATA_NAME_MAP not found in extract.py")


SENSOR_WEAR_NAMES = _sensor_wear_names()


def _load_att():
    from tests.conftest import attention

    return attention


def _sensor_wear(payload: dict[str, Any]) -> int | None:
    """The ATA wear value as sensor._extract_attribute computes it."""
    is_dead = _load_att().is_dead_wear_attr
    table = (payload["smart_data"].get("ata_smart_attributes") or {}).get("table", [])
    for attr in table:
        if attr.get("name") in SENSOR_WEAR_NAMES:
            if is_dead(attr):
                continue
            normalized = attr.get("value")
            return None if normalized is None else max(0, 100 - normalized)
    return None


def _wear_reasons(att, payload: dict[str, Any]) -> list[str]:
    _, _, reasons, _ = att.evaluate_attention(payload)
    return [r for r in reasons if "wear" in r.lower()]


def test_the_two_wear_lists_agree(att):
    """sensor.py and attention.py hold separate literals; they must match."""
    assert set(SENSOR_WEAR_NAMES) == att._ATA_WEAR_NAMES


def test_wear_range_delta_is_in_neither_list(att):
    assert "Wear_Range_Delta" not in SENSOR_WEAR_NAMES
    assert "Wear_Range_Delta" not in att._ATA_WEAR_NAMES


def test_drive_life_protection_stat_is_in_neither_list(att):
    """In no drivedb entry and over smartctl's 23-character name limit, so no
    drive can report it (v0.8.0 tidy list)."""
    assert "Drive_Life_Protection_Stat" not in SENSOR_WEAR_NAMES
    assert "Drive_Life_Protection_Stat" not in att._ATA_WEAR_NAMES


def test_sandforce_force_gt_reads_new(att, drive):
    """Constructed fixture (CrystalDiskInfo values, drivedb names): 177 at 0,
    231 at 100. The drive is new, so 0 % used and no wear warning."""
    payload = drive("ata_sandforce_force_gt")
    assert _sensor_wear(payload) == 0
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 0
    state, _, reasons, _ = att.evaluate_attention(payload)
    assert state == "NO", reasons
    assert _wear_reasons(att, payload) == []


def test_sandforce_wear_follows_ssd_life_left(att, drive):
    """The reading moves with 231, and 177 at 0 no longer decides anything."""
    payload = drive("ata_sandforce_force_gt")
    for attr in payload["smart_data"]["ata_smart_attributes"]["table"]:
        if attr["name"] == "SSD_Life_Left":
            attr["value"] = 5
    assert _sensor_wear(payload) == 95
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 95
    assert _wear_reasons(att, payload) == [
        "SSD wear at 95% of rated life -- consider scheduling replacement"
    ]


def test_wear_range_delta_alone_gives_no_wear_reading(att, drive, add_ata_attr):
    """A drive whose only wear-ish attribute is 177 Wear_Range_Delta has no
    wear reading at all now, rather than a wrong one."""
    payload = drive("ata_healthy")
    add_ata_attr(payload, 177, "Wear_Range_Delta", 8)
    payload["smart_data"]["ata_smart_attributes"]["table"][-1]["value"] = 0
    assert _sensor_wear(payload) is None
    assert att.LABEL_SSD_WEAR not in att.current_readings(payload)
    assert _wear_reasons(att, payload) == []


def test_samsung_wear_leveling_count_unchanged(att, drive, add_ata_attr):
    """Samsung 177 Wear_Leveling_Count still reads as before. No Samsung
    fixture exists, so the row is added to a healthy ATA payload."""
    payload = drive("ata_healthy")
    add_ata_attr(payload, 177, "Wear_Leveling_Count", 41)
    payload["smart_data"]["ata_smart_attributes"]["table"][-1]["value"] = 97
    assert _sensor_wear(payload) == 3
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 3


def test_existing_fixtures_keep_their_wear_value(att, drive):
    """Values recorded on the base commit, before the list changed."""
    expected = {
        "ata_sandforce_force_gt": 0,  # round 1 constructed fixture
        "ata_healthy": None,
        "ata_reallocated": None,
        "ata_skhynix": 1,  # 231 SSD_Life_Left at 99
        "nvme_healthy": 2,
        "nvme_media_errors": 31,
    }
    for name, value in expected.items():
        payload = drive(name)
        assert att.current_readings(payload).get(att.LABEL_SSD_WEAR) == value, name
        if not name.startswith("nvme"):
            assert _sensor_wear(payload) == value, name


# ---------------------------------------------------------------------------
# Round 2: a wear-named row at 0/0/0 with flags 0 is not a gauge
# ---------------------------------------------------------------------------

_DEAD = {
    "id": 177,
    "name": "Wear_Leveling_Count",
    "value": 0,
    "worst": 0,
    "thresh": 0,
    "flags": {"value": 0, "string": "------ "},
    "raw": {"value": 8, "string": "8"},
}


def test_predicate_all_zero_is_dead(att):
    assert att.is_dead_wear_attr(_DEAD) is True


def test_predicate_any_nonzero_is_kept(att):
    for field in ("value", "worst", "thresh"):
        row = copy.deepcopy(_DEAD)
        row[field] = 1
        assert att.is_dead_wear_attr(row) is False, field
    row = copy.deepcopy(_DEAD)
    row["flags"]["value"] = 0x13  # Samsung 177 flags, also when worn out
    assert att.is_dead_wear_attr(row) is False


def test_predicate_missing_keys_are_kept(att):
    for field in ("value", "worst", "thresh", "flags"):
        row = copy.deepcopy(_DEAD)
        del row[field]
        assert att.is_dead_wear_attr(row) is False, field
    row = copy.deepcopy(_DEAD)
    del row["flags"]["value"]
    assert att.is_dead_wear_attr(row) is False
    row = copy.deepcopy(_DEAD)
    row["flags"] = None
    assert att.is_dead_wear_attr(row) is False
    row = copy.deepcopy(_DEAD)
    row["value"] = False  # a bool is not the integer 0
    assert att.is_dead_wear_attr(row) is False


def test_real_force_gt_capture_has_no_wear_reading(att, drive):
    """The reporter's real smartctl JSON: 177 and 233 are dead, 231 is
    unnamed, so there is no wear reading and no wear warning."""
    payload = drive("ata_sandforce_force_gt_smartctl")
    assert _sensor_wear(payload) is None
    readings = att.current_readings(payload)
    assert att.LABEL_SSD_WEAR not in readings
    assert _wear_reasons(att, payload) == []
    state, _, reasons, _ = att.evaluate_attention(payload)
    assert state == "NO", reasons
    assert reasons == []


def test_real_force_gt_capture_attention_otherwise_unchanged(att, drive):
    """Apart from wear, the readings are the ones the base code produced."""
    payload = drive("ata_sandforce_force_gt_smartctl")
    readings = att.current_readings(payload)
    assert readings == {
        "Reallocated Sector Count": 0,
        "Reported Uncorrectable Errors": 0,
        "Reallocated Event Count": 0,
    }


def test_dead_row_is_skipped_for_the_next_candidate(att, drive, add_ata_attr):
    """A dead 177 followed by a live 231 SSD_Life_Left reads from 231."""
    payload = drive("ata_healthy")
    table = payload["smart_data"].setdefault(
        "ata_smart_attributes", {"revision": 16, "table": []}
    )["table"]
    table.append(copy.deepcopy(_DEAD))
    add_ata_attr(payload, 231, "SSD_Life_Left", 0)
    table[-1]["value"] = 92
    table[-1]["thresh"] = 10
    assert _sensor_wear(payload) == 8
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 8
    assert _wear_reasons(att, payload) == []
    table[-1]["value"] = 4
    assert _sensor_wear(payload) == 96
    assert _wear_reasons(att, payload) == [
        "SSD wear at 96% of rated life -- consider scheduling replacement"
    ]


def test_worn_samsung_177_still_reads_worn(att, drive):
    """A worn-out Samsung 177 (850 EVO and PM830 reports: flags 0x0013,
    value 1, thresh 0 or 10) stays a reading, and value 0 with those flags
    would too."""
    payload = drive("ata_healthy")
    table = payload["smart_data"].setdefault(
        "ata_smart_attributes", {"revision": 16, "table": []}
    )["table"]
    row = copy.deepcopy(_DEAD)
    row["flags"] = {"value": 0x13, "string": "PO--C- "}
    table.append(row)
    assert _sensor_wear(payload) == 100
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 100
    assert len(_wear_reasons(att, payload)) == 1


def test_sensor_py_uses_the_shared_predicate():
    """Check by source that both the value lookup (extract.py, re-imported by
    sensor.py) and the covered-row logic (entity_plan.py, moved out of
    sensor.async_setup_entry) call the shared predicate."""
    users = set()
    for path in (_SENSOR_PY, _PLAN_PY):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and sub.id == "is_dead_wear_attr":
                        users.add(node.name)
    assert {"_extract_attribute", "_covered_rows"} <= users
