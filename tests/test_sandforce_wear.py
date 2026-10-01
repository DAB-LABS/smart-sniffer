"""Which attribute the wear reading comes from (GH #55).

SandForce drives report 177 as Wear_Range_Delta: the spread between the most
and least worn blocks, not life remaining. A new drive reads 0 there, and both
wear paths took it as "0 % life left", so the sensor said 100 % used and the
attention check raised an SSD wear warning. Life remaining is in 231
SSD_Life_Left.

The Corsair Force GT fixture is CONSTRUCTED from the reporter's CrystalDiskInfo
dump, not captured with smartctl: the values are his, the attribute names are
the ones smartmontools drivedb.h gives the SandForce entry. See its _comment.

sensor.py imports Home Assistant, which this suite does not have, so the
sensor's name list is read from the source with ast and its selection rule
(first row in drive-table order whose name is in the list, reported as
100 - normalized value) is restated here. The attention side runs the real
module.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

_SENSOR_PY = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "smart_sniffer"
    / "sensor.py"
)


def _sensor_wear_names() -> list[str]:
    tree = ast.parse(_SENSOR_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == "ATA_NAME_MAP":
            return ast.literal_eval(node.value)["wear_leveling_count"]
    raise AssertionError("ATA_NAME_MAP not found in sensor.py")


SENSOR_WEAR_NAMES = _sensor_wear_names()


def _sensor_wear(payload: dict[str, Any]) -> int | None:
    """The ATA wear value as sensor._extract_attribute computes it."""
    table = (payload["smart_data"].get("ata_smart_attributes") or {}).get("table", [])
    for attr in table:
        if attr.get("name") in SENSOR_WEAR_NAMES:
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
