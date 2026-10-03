"""Gap-fill from Device Statistics (D4) and parity with v0.7.0 (plan v3, 6.4)."""

from __future__ import annotations

import json

import pytest

from custom_components.smart_sniffer import attention as att
from custom_components.smart_sniffer.devstat import (
    DEVSTAT_KEY,
    devstat_gap_readings,
    merge_devstat,
)
from tests.devstat_payloads import agent_drive, pages_of
from tests.golden_payloads import GOLDEN, golden_payload

UNC_18 = "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)"


def _merged(drive, held=None):
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, held)
    return drive


def _remove_attr(drive, attr_id):
    table = drive["smart_data"]["ata_smart_attributes"]["table"]
    drive["smart_data"]["ata_smart_attributes"]["table"] = [
        a for a in table if a["id"] != attr_id
    ]


# --- v0.7.0 parity -----------------------------------------------------------------


def _golden_attention():
    with (GOLDEN / "v070-attention.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def test_every_existing_payload_is_byte_identical_without_device_statistics():
    """Recorded from attention.py at 36ebe65: state, severity, reasons,
    accepted, readings, labels, placeholders and reasons text, with default
    thresholds and with overrides, for every fixture. A v0.7.0 agent payload
    (no device_statistics) must give exactly that, merged or not."""
    golden = _golden_attention()
    for drive_id, drive in golden_payload().items():
        if drive_id.startswith("_"):
            continue
        _merged(drive)  # what the coordinator does: an empty merge
        assert drive[DEVSTAT_KEY] == {}
        want = golden["drives"][drive_id]
        assert list(att.evaluate_attention(drive)) == want["default"], drive_id
        assert list(att.evaluate_attention(drive, golden["overrides"])) == want["overrides"]
        assert att.current_readings(drive) == want["readings"], drive_id
        assert att.labels_for_drive(drive) == want["labels"]
        assert att.reading_placeholders(att.current_readings(drive), "d") == want["placeholders"]
        state, _, reasons, accepted = att.evaluate_attention(drive)
        assert att.compose_reasons_text(state, reasons, accepted) == want["reasons_text"]


@pytest.mark.parametrize("name", [
    "ata_devstat_hgst", "ata_devstat_seagate", "ata_devstat_samsung_ssd",
    "ata_devstat_hgst_nopoh",
])
def test_devstat_changes_no_reason_where_there_is_no_gap(name):
    """Drives whose devstat counts are zero, or whose table has the
    attribute, read exactly as v0.7.0 with a present devstat block."""
    golden = _golden_attention()["drives"][f"fx-{name.replace('_', '-')}"]
    drive = _merged(agent_drive(name))
    assert list(att.evaluate_attention(drive)) == golden["default"]
    assert list(att.evaluate_attention(drive, _golden_attention()["overrides"])) == golden["overrides"]


# --- m01-sdl: the blind spot ----------------------------------------------------------


def test_m01_sdl_goes_from_no_to_yes_with_the_exact_reason():
    before = agent_drive("ata_devstat_wdc_unc", status=None)
    assert att.evaluate_attention(before)[:3] == ("NO", "none", [])
    drive = _merged(agent_drive("ata_devstat_wdc_unc"))
    assert att.evaluate_attention(drive) == ("YES", "critical", [UNC_18], [])
    assert att.compose_reasons_text("YES", [UNC_18], []) == UNC_18


def test_current_readings_shows_18():
    drive = _merged(agent_drive("ata_devstat_wdc_unc"))
    assert att.current_readings(drive)["Reported Uncorrectable Errors"] == 18
    assert "Reported Uncorrectable Errors" not in att.current_readings(
        agent_drive("ata_devstat_wdc_unc", status=None)
    )


def test_held_gives_the_same_text_as_fresh():
    """No "reasons updated" when a poll serves the held value."""
    fresh = _merged(agent_drive("ata_devstat_wdc_unc"))
    _, held = merge_devstat(fresh, None)
    stale = _merged(
        agent_drive("ata_devstat_wdc_unc", status="unavailable", reason="timeout"), held
    )
    assert att.evaluate_attention(stale) == att.evaluate_attention(fresh)
    assert att.current_readings(stale) == att.current_readings(fresh)


def test_accepted_and_threshold_variants_carry_the_suffix():
    drive = _merged(agent_drive("ata_devstat_wdc_unc"))
    label = "Reported Uncorrectable Errors"
    assert att.evaluate_attention(drive, {label: 18}) == (
        "NO", "none", [],
        ["Reported Uncorrectable Errors: 18 (accepted 18; from device statistics)"],
    )
    assert att.evaluate_attention(drive, {label: 10})[2] == [
        "Reported Uncorrectable Errors: 18 (accepted 10; from device statistics)"
    ]


def test_turning_devstat_off_removes_the_gap_reason():
    drive = _merged(agent_drive("ata_devstat_wdc_unc", status="off", reason="config"))
    assert att.evaluate_attention(drive)[:3] == ("NO", "none", [])


# --- the id gates -------------------------------------------------------------------


def test_attribute_187_blocks_p4_by_id_whatever_its_name():
    """Seagate 187 Reported_Uncorrect and Samsung 187 Uncorrectable_Error_Cnt
    both block the devstat count; the attribute keeps deciding."""
    seagate = _merged(agent_drive("ata_devstat_seagate"))
    assert [r for r in att.evaluate_attention(seagate)[2] if "device statistics" in r] == []
    samsung = agent_drive("ata_devstat_samsung_ssd")
    pages = pages_of("ata_devstat_samsung_ssd")
    for page in pages:
        if page["number"] == 4:
            page["table"][0]["value"] = 7
    samsung = _merged(agent_drive("ata_devstat_samsung_ssd", pages=pages))
    names = {a["id"]: a["name"] for a in samsung["smart_data"]["ata_smart_attributes"]["table"]}
    assert names[187] == "Uncorrectable_Error_Cnt"
    assert devstat_gap_readings(samsung["smart_data"], samsung[DEVSTAT_KEY]) == []
    assert att.evaluate_attention(samsung)[:3] == ("NO", "none", [])


def test_attribute_5_blocks_p3_and_its_absence_lets_it_fill():
    pages = pages_of("ata_devstat_hgst")
    for page in pages:
        if page["number"] == 3:
            for entry in page["table"]:
                if entry["offset"] == 0x020:
                    entry["value"] = 4
    drive = _merged(agent_drive("ata_devstat_hgst", pages=pages))
    assert att.evaluate_attention(drive)[:3] == ("NO", "none", [])
    _remove_attr(drive, 5)
    assert att.evaluate_attention(drive)[2] == [
        "Reallocated Sector Count: 4 (expected 0; from device statistics)"
    ]
    assert att.current_readings(drive)["Reallocated Sector Count"] == 4


def test_wear_fills_only_without_a_usable_wear_attribute():
    """P7 never fills on the corpus (every P7 drive has a usable 177); with
    177 gone it does, as percent used with no inversion."""
    pages = pages_of("ata_devstat_samsung_ssd")
    for page in pages:
        if page["number"] == 7:
            page["table"][0]["value"] = 95
    drive = _merged(agent_drive("ata_devstat_samsung_ssd", pages=pages))
    assert att.current_readings(drive)[att.LABEL_SSD_WEAR] == 1  # 177 at 99
    _remove_attr(drive, 177)
    assert att.current_readings(drive)[att.LABEL_SSD_WEAR] == 95
    assert att.evaluate_attention(drive)[2] == [
        "SSD wear at 95% of rated life -- consider scheduling replacement"
        " (from device statistics)"
    ]
    assert att.evaluate_attention(drive, {att.LABEL_SSD_WEAR: 97})[3] == [
        "SSD wear at 95% of rated life (accepted 97%; from device statistics)"
    ]


def test_a_dead_wear_row_does_not_block_the_wear_fill():
    pages = pages_of("ata_devstat_samsung_ssd")
    for page in pages:
        if page["number"] == 7:
            page["table"][0]["value"] = 3
    drive = _merged(agent_drive("ata_devstat_samsung_ssd", pages=pages))
    for attr in drive["smart_data"]["ata_smart_attributes"]["table"]:
        if attr["id"] == 177:
            attr.update(value=0, worst=0, thresh=0, flags={"value": 0, "string": "------ "})
    assert att.current_readings(drive)[att.LABEL_SSD_WEAR] == 3


def test_no_gap_fill_on_nvme_or_scsi():
    for name in ("nvme_sabrent", "scsi_view"):
        drive = _merged(agent_drive(name, status="not_applicable"))
        golden = _golden_attention()["drives"][f"fx-{name.replace('_', '-')}"]
        assert list(att.evaluate_attention(drive)) == golden["default"]
        assert att.current_readings(drive) == golden["readings"]
