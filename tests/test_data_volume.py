"""Data Written / Data Read values (plan v3, 6.1, 6.3, 7.3, 7.7)."""

from __future__ import annotations

import pytest

from custom_components.smart_sniffer.devstat import DEVSTAT_KEY, data_volume, merge_devstat
from tests.devstat_payloads import (
    HGST_READS,
    HGST_WRITES,
    SABRENT_WRITES,
    SAMSUNG850_WRITES,
    WDC_UNC_WRITES,
    agent_drive,
)


def _volume(drive, key="host_writes", held=None):
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, held)
    return data_volume(drive, key)


def _tb(value):
    """What the card shows: suggested unit TB (decimal), precision 2."""
    return f"{value / 1e12:.2f}"


@pytest.mark.parametrize(
    ("name", "status", "expected", "shown"),
    [
        ("ata_devstat_hgst", "present", HGST_WRITES, "163.66"),
        ("ata_devstat_wdc_unc", "present", WDC_UNC_WRITES, "107.83"),
        ("ata_no_devstat_samsung850", "absent", SAMSUNG850_WRITES, "105.96"),
        ("nvme_sabrent", "not_applicable", SABRENT_WRITES, "206.18"),
    ],
)
def test_corpus_figures(name, status, expected, shown):
    value, _ = _volume(agent_drive(name, status=status))
    assert value == expected
    assert _tb(value) == shown


def test_hgst_reads():
    value, attrs = _volume(agent_drive("ata_devstat_hgst"), "host_reads")
    assert value == HGST_READS and _tb(value) == "184.22"
    assert attrs == {"source": "ata_device_statistics"}


def test_seagate_devstat_over_241():
    """m01-sdd is in drivedb with 241; the agent prefers devstat (within
    1e-7 of 241 x 512 on this drive), and the integration shows what it got."""
    drive = agent_drive("ata_devstat_seagate")
    value, attrs = _volume(drive)
    assert attrs == {"source": "ata_device_statistics"}
    raw241 = next(
        a["raw"]["value"] for a in drive["smart_data"]["ata_smart_attributes"]["table"]
        if a["id"] == 241
    )
    assert abs(value - raw241 * 512) / value < 1e-7


def test_missing_poh_entry_is_tolerated():
    value, _ = _volume(agent_drive("ata_devstat_hgst_nopoh"))
    assert value == 341_937_863_940 * 512


def test_scsi_view_has_none():
    assert _volume(agent_drive("scsi_view", status="not_applicable")) == (None, {})
    assert _volume(agent_drive("scsi_view", status="not_applicable"), "host_reads") == (None, {})


def test_nvme_source():
    _, attrs = _volume(agent_drive("nvme_sabrent", status="not_applicable"))
    assert attrs == {"source": "nvme"}


def test_vendor_attribute_attributes():
    _, attrs = _volume(agent_drive("ata_no_devstat_samsung850", status="absent"))
    assert attrs == {
        "source": "ata_attribute", "attribute_id": 241, "attribute_name": "Total_LBAs_Written",
    }


def test_older_agent_has_no_value():
    assert _volume(agent_drive("ata_devstat_hgst", status=None)) == (None, {})


def test_stopped_holds_with_held_true():
    _, held = merge_devstat(agent_drive("ata_devstat_hgst"), None)
    drive = agent_drive("ata_devstat_hgst", status="unavailable", reason="stopped")
    assert _volume(drive, held=held) == (
        HGST_WRITES, {"source": "ata_device_statistics", "held": True}
    )
    assert _volume(drive, "host_reads", held=held)[0] == HGST_READS


def test_junk_bytes_are_unknown():
    for junk in (None, "12", -5, True, 1.5):
        drive = agent_drive(
            "ata_devstat_hgst",
            derived={"host_writes": {"bytes": junk, "source": "ata_device_statistics"}},
        )
        assert _volume(drive) == (None, {}), junk
