"""merge_devstat: the held copy and the per-reading merge (plan v3, 6.3)."""

from __future__ import annotations

import pytest

from custom_components.smart_sniffer.devstat import (
    DEVSTAT_KEY,
    data_volume,
    devstat_entry,
    merge_devstat,
    normalize_store,
    page_attributes,
)
from tests.devstat_payloads import (
    HGST_READS,
    HGST_WRITES,
    WDC_UNC_WRITES,
    agent_drive,
    drop_entry,
    pages_of,
)


def _merged(drive, held=None):
    effective, new_held = merge_devstat(drive, held)
    drive[DEVSTAT_KEY] = effective
    return effective, new_held


# --- reading one entry ---------------------------------------------------------


def test_entry_value_needs_valid_flag_and_integer():
    pages = pages_of("ata_devstat_wdc_unc")
    assert devstat_entry(pages, 4, 0x008) == 18
    entry = pages[[p["number"] for p in pages].index(4)]["table"][0]
    entry["flags"]["valid"] = False
    assert devstat_entry(pages, 4, 0x008) is None
    entry["flags"]["valid"] = True
    del entry["value"]
    assert devstat_entry(pages, 4, 0x008) is None
    entry["value"] = True  # a bool is not a count
    assert devstat_entry(pages, 4, 0x008) is None
    entry["value"] = -1
    assert devstat_entry(pages, 4, 0x008) is None


def test_missing_page_or_entry_is_none():
    pages = pages_of("ata_devstat_hgst_nopoh")
    assert devstat_entry(pages, 7, 0x008) is None  # an HDD has no P7
    assert devstat_entry(pages, 1, 0x010) is None  # m02-sdd has no POH entry
    assert devstat_entry(pages, 1, 0x018) == 341_937_863_940


# --- each 6.3 row ---------------------------------------------------------------


def test_present_complete_is_fresh_and_held():
    drive = agent_drive("ata_devstat_wdc_unc")
    effective, held = _merged(drive)
    assert effective["unc"] == 18 and effective["realloc"] == 0
    assert effective["wear_used"] is None
    assert effective["held"] == []
    assert held == {"unc": 18, "realloc": 0, "host_writes": WDC_UNC_WRITES,
                    "host_reads": held["host_reads"]}


def test_present_partial_keeps_the_held_reading():
    """M1: a partial read (bit 2, pages before the failure kept) lacking P4
    keeps unc 18 from the hold; the pages it has are fresh."""
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    pages = pages_of("ata_devstat_wdc_unc")
    drop_entry(pages, 4)
    drop_entry(pages, 5)
    drive = agent_drive("ata_devstat_wdc_unc", complete=False, pages=pages)
    effective, new_held = _merged(drive, held)
    assert effective["unc"] == 18
    assert effective["held"] == ["unc"]
    assert effective["realloc"] == 0
    assert new_held == held


def test_present_with_an_invalid_entry_keeps_the_held_reading():
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    pages = pages_of("ata_devstat_wdc_unc")
    for page in pages:
        if page["number"] == 4:
            page["table"][0]["flags"]["valid"] = False
    effective, _ = _merged(agent_drive("ata_devstat_wdc_unc", pages=pages), held)
    assert effective["unc"] == 18 and "unc" in effective["held"]


def test_a_fresh_value_replaces_the_held_one():
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    pages = pages_of("ata_devstat_wdc_unc")
    for page in pages:
        if page["number"] == 4:
            page["table"][0]["value"] = 19
    effective, new_held = _merged(agent_drive("ata_devstat_wdc_unc", pages=pages), held)
    assert effective["unc"] == 19 and new_held["unc"] == 19


@pytest.mark.parametrize("reason", ["failed", "timeout", "standby", "mismatch", "stopped"])
def test_unavailable_serves_every_held_reading(reason):
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    drive = agent_drive("ata_devstat_wdc_unc", status="unavailable", reason=reason)
    effective, new_held = _merged(drive, held)
    assert effective["unc"] == 18 and effective["realloc"] == 0
    assert sorted(effective["held"]) == ["realloc", "unc"]
    assert new_held == held
    assert data_volume(drive, "host_writes") == (
        WDC_UNC_WRITES, {"source": "ata_device_statistics", "held": True}
    )


@pytest.mark.parametrize("status", ["absent", "off", "not_applicable"])
def test_absent_off_not_applicable_use_no_hold_and_keep_it(status):
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    drive = agent_drive("ata_devstat_wdc_unc", status=status)
    effective, new_held = _merged(drive, held)
    assert effective["unc"] is None and effective["realloc"] is None
    assert effective["held"] == []
    assert new_held == held  # kept in storage, unused
    assert data_volume(drive, "host_writes") == (None, {})


def test_an_older_agent_gives_an_empty_merge_and_keeps_the_hold():
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    drive = agent_drive("ata_devstat_wdc_unc", status=None)
    effective, new_held = _merged(drive, held)
    assert effective == {}
    assert new_held == held


def test_held_is_never_cleared_by_a_missing_page():
    _, held = _merged(agent_drive("ata_devstat_wdc_unc"))
    for _ in range(3):
        pages = pages_of("ata_devstat_wdc_unc")
        drop_entry(pages, 4)
        drop_entry(pages, 3)
        _, held = _merged(agent_drive("ata_devstat_wdc_unc", complete=False, pages=pages), held)
    assert held["unc"] == 18 and held["realloc"] == 0


def test_a_serial_less_drive_holds_nothing():
    drive = agent_drive("ata_devstat_wdc_unc")
    drive["serial"] = ""
    effective, new_held = _merged(drive, {"unc": 99})
    assert effective["unc"] == 18  # fresh only
    assert new_held == {}
    gone = agent_drive("ata_devstat_wdc_unc", status="unavailable", reason="failed")
    gone["serial"] = ""
    effective, new_held = _merged(gone, {"unc": 99})
    assert effective["unc"] is None and new_held == {}


# --- Data Written / Read from the merge ------------------------------------------


def test_data_written_from_devstat():
    drive = agent_drive("ata_devstat_hgst")
    _merged(drive)
    assert data_volume(drive, "host_writes") == (HGST_WRITES, {"source": "ata_device_statistics"})
    assert data_volume(drive, "host_reads") == (HGST_READS, {"source": "ata_device_statistics"})


def test_data_written_held_when_derived_omits_it():
    _, held = _merged(agent_drive("ata_devstat_hgst"))
    drive = agent_drive(
        "ata_devstat_hgst",
        derived={"host_writes_omitted": "entry_invalid", "host_reads_omitted": "entry_invalid"},
    )
    _merged(drive, held)
    assert data_volume(drive, "host_writes") == (
        HGST_WRITES, {"source": "ata_device_statistics", "held": True}
    )


def test_a_vendor_attribute_volume_is_shown_but_never_held():
    drive = agent_drive("ata_no_devstat_samsung850", status="absent")
    effective, held = _merged(drive, {})
    assert held == {}
    assert data_volume(drive, "host_writes")[1] == {
        "source": "ata_attribute", "attribute_id": 241, "attribute_name": "Total_LBAs_Written"
    }
    assert data_volume(drive, "host_reads") == (None, {})


def test_store_payload_is_normalized():
    assert normalize_store(None) == {"announced": [], "held": {}}
    assert normalize_store({"announced": ["a|unc", 3], "held": {"d": {"unc": 1}, "x": 2}}) == {
        "announced": ["a|unc"], "held": {"d": {"unc": 1}},
    }


# --- attributes from this poll's pages ---------------------------------------------


def test_page_attributes_from_this_poll_only():
    drive = agent_drive("ata_devstat_hgst")
    assert page_attributes(drive, "temperature") == {
        "lifetime_max": 46, "lifetime_min": 17, "time_over_limit_minutes": 0,
    }
    assert page_attributes(drive, "reported_uncorrectable_errors") == {"device_statistics_count": 0}
    assert page_attributes(drive, "reallocated_sector_count") == {
        "device_statistics_logical_sectors": 0
    }
    assert page_attributes(drive, "power_on_hours") == {}
    stale = agent_drive("ata_devstat_hgst", status="unavailable", reason="failed")
    assert page_attributes(stale, "temperature") == {}
    assert page_attributes(agent_drive("ata_devstat_hgst", status=None), "temperature") == {}
