"""WD Blue / Red / Green SSDs count attribute 230 up (GH #63).

On WD's Marvell-based SATA SSDs (drivedb family "WD Blue / Red / Green
SSDs": WD Blue 3D, WD Red SA500, WD Blue SA510, WD Green) the normalized
value of 230 Media_Wearout_Indicator is percent of life USED. Inverting it,
as for every other family, showed a near-new WD Red SA500 as 99 % used and
raised an SSD wear warning.

The fixture is built from the reporter's smartctl JSON with the serial, WWN,
device path, platform and time invented. Its other readings agree with about
1 % used: Average_PE_Cycles_TLC 4, Maximum 8, 4560 GiB host writes.

Device Statistics' Percentage Used Endurance Indicator wins when held, and a
bad disagreement between 230 and the average P/E cycles makes the reading
unknown rather than trusting either.
"""

from __future__ import annotations

from typing import Any

from custom_components.smart_sniffer.devstat import DEVSTAT_KEY
from custom_components.smart_sniffer.extract import _extract_attribute

FIXTURE = "v083/ata_wd_red_sa500"


def _row(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for attr in payload["smart_data"]["ata_smart_attributes"]["table"]:
        if attr["name"] == name:
            return attr
    raise KeyError(name)


def _wear_reasons(att, payload: dict[str, Any]) -> list[str]:
    _, _, reasons, _ = att.evaluate_attention(payload)
    return [r for r in reasons if "wear" in r.lower()]


def test_the_fixture_is_the_wd_family(drive, att):
    payload = drive(FIXTURE)
    assert payload["smart_data"]["model_family"] == att.WD_COUNT_UP_WEAR_FAMILY
    row = _row(payload, "Media_Wearout_Indicator")
    assert (row["id"], row["value"]) == (230, 1)


def test_wd_red_sa500_reads_one_percent_used(drive, att):
    """The reported drive: 1 % used, no wear warning, no note."""
    payload = drive(FIXTURE)
    assert _extract_attribute(payload, "wear_leveling_count") == 1
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 1
    state, _, reasons, _ = att.evaluate_attention(payload)
    assert state == "NO", reasons
    assert att.ata_wear_note(payload) is None


def test_wd_wear_still_warns_when_really_worn(drive, att):
    """230 at 95 with average P/E cycles agreeing (950 of 1000) warns."""
    payload = drive(FIXTURE)
    _row(payload, "Media_Wearout_Indicator")["value"] = 95
    _row(payload, "Average_PE_Cycles_TLC")["raw"] = {"value": 950, "string": "950"}
    assert _extract_attribute(payload, "wear_leveling_count") == 95
    assert _wear_reasons(att, payload) == [
        "SSD wear at 95% of rated life -- consider scheduling replacement"
    ]


def test_wd_high_wear_with_low_pe_cycles_is_unknown(drive, att):
    """230 says 99 % used but 4 average cycles say about 0 %: unknown."""
    payload = drive(FIXTURE)
    _row(payload, "Media_Wearout_Indicator")["value"] = 99
    assert _extract_attribute(payload, "wear_leveling_count") is None
    assert att.LABEL_SSD_WEAR not in att.current_readings(payload)
    assert _wear_reasons(att, payload) == []
    note = att.ata_wear_note(payload)
    assert note is not None
    assert "wear unknown" in note and "99%" in note and "(4)" in note


def test_wd_low_wear_with_high_pe_cycles_is_unknown(drive, att):
    """230 says 2 % used but 800 average cycles say about 80 %: unknown."""
    payload = drive(FIXTURE)
    _row(payload, "Media_Wearout_Indicator")["value"] = 2
    _row(payload, "Average_PE_Cycles_TLC")["raw"] = {"value": 800, "string": "800"}
    assert _extract_attribute(payload, "wear_leveling_count") is None
    assert _wear_reasons(att, payload) == []
    assert "about 80%" in att.ata_wear_note(payload)


def test_wd_without_pe_cycles_trusts_230(drive, att):
    payload = drive(FIXTURE)
    table = payload["smart_data"]["ata_smart_attributes"]["table"]
    table[:] = [a for a in table if a["name"] != "Average_PE_Cycles_TLC"]
    _row(payload, "Media_Wearout_Indicator")["value"] = 7
    assert _extract_attribute(payload, "wear_leveling_count") == 7
    assert att.ata_wear_note(payload) is None


def test_device_statistics_win_for_the_wd_family(drive, att):
    """A held Percentage Used Endurance Indicator is preferred, even over a
    230 reading the cross-check would reject."""
    payload = drive(FIXTURE)
    payload[DEVSTAT_KEY] = {"wear_used": 3}
    _row(payload, "Media_Wearout_Indicator")["value"] = 99
    assert _extract_attribute(payload, "wear_leveling_count") == 3
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 3
    assert _wear_reasons(att, payload) == []
    assert att.ata_wear_note(payload) == "from device statistics"


def test_device_statistics_warn_once_for_the_wd_family(drive, att):
    payload = drive(FIXTURE)
    payload[DEVSTAT_KEY] = {"wear_used": 93}
    assert _wear_reasons(att, payload) == [
        "SSD wear at 93% of rated life -- consider scheduling replacement"
    ]


def test_230_outside_the_family_is_still_inverted(drive, att):
    """Same rows without the WD family string: the usual rule, 99 % used."""
    payload = drive(FIXTURE)
    payload["smart_data"].pop("model_family")
    assert _extract_attribute(payload, "wear_leveling_count") == 99
    assert att.ata_wear_note(payload) is None
    payload["smart_data"]["model_family"] = "Intel 53x and Pro 1500/2500 Series SSDs"
    assert _extract_attribute(payload, "wear_leveling_count") == 99


def test_other_wd_family_wear_attributes_are_inverted(drive, att, add_ata_attr):
    """Only attribute 230 counts up: a remaining-life gauge at another id on
    the same family keeps the inversion."""
    payload = drive(FIXTURE)
    table = payload["smart_data"]["ata_smart_attributes"]["table"]
    table[:] = [a for a in table if a["name"] != "Media_Wearout_Indicator"]
    add_ata_attr(payload, 169, "Remaining_Lifetime_Perc", 97)
    table[-1]["value"] = 97
    assert _extract_attribute(payload, "wear_leveling_count") == 3


def test_other_fixtures_are_unchanged(drive, att, fixture_names):
    """Every top-level fixture reads the wear value it did before #63."""
    for name in fixture_names:
        payload = drive(name)
        table = (payload["smart_data"].get("ata_smart_attributes") or {}).get("table", [])
        expected = None
        for attr in table:
            if attr.get("name") in att._ATA_WEAR_NAMES and not att.is_dead_wear_attr(attr):
                expected = max(0, 100 - attr["value"])
                break
        if payload["smart_data"].get("nvme_smart_health_information_log"):
            continue
        assert _extract_attribute(payload, "wear_leveling_count") == expected, name


def test_an_unknown_wd_wear_keeps_its_entity(drive):
    """The plan keeps the Wear Level sensor when the reading is unknown, so an
    existing entity shows unknown with its note instead of disappearing, and
    230 does not reappear as a diagnostic entity."""
    from custom_components.smart_sniffer import entity_plan as ep

    for value in (1, 99):
        payload = drive(FIXTURE)
        payload.update({"id": "fx-wd", "serial": "WD0000TEST63", "readable": True})
        _row(payload, "Media_Wearout_Indicator")["value"] = value
        specs = ep.sensor_specs({"fx-wd": payload}, "entry", [], {})
        assert "wear_leveling_count" in [s.key for s in specs], value
        assert 230 not in [s.attr_id for s in specs], value
