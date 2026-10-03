"""D11: one announcement per drive and reading (plan v3, 6.4)."""

from __future__ import annotations

from custom_components.smart_sniffer import attention as att
from custom_components.smart_sniffer.devstat import (
    ANNOUNCEMENT_LINE,
    DEVSTAT_KEY,
    announcement_records,
    baseline_announcement,
    forget_serial,
    merge_devstat,
)
from tests.devstat_payloads import agent_drive

UNC_18 = "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)"
SERIAL = "FIXTURE-m01-sdl"


def test_the_m01_sdl_reasons_announce_once():
    drive = agent_drive("ata_devstat_wdc_unc")
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)
    _, _, reasons, _ = att.evaluate_attention(drive)
    assert reasons == [UNC_18]
    announce, records = baseline_announcement(reasons, drive["serial"], [])
    assert announce is True
    assert records == [f"{SERIAL}|unc"]
    # After a restart the record is in the Store: no second announcement.
    assert baseline_announcement(reasons, drive["serial"], records) == (False, [])


def test_never_without_a_serial():
    assert baseline_announcement([UNC_18], "", []) == (False, [])
    assert baseline_announcement([UNC_18], None, []) == (False, [])
    assert announcement_records([UNC_18], "") == []


def test_never_for_a_reason_without_the_suffix():
    plain = "Reported Uncorrectable Errors: 9 (expected 0)"
    assert baseline_announcement([plain], SERIAL, []) == (False, [])


def test_a_second_reading_of_the_same_drive_still_announces():
    realloc = "Reallocated Sector Count: 4 (expected 0; from device statistics)"
    announce, records = baseline_announcement([UNC_18, realloc], SERIAL, [f"{SERIAL}|unc"])
    assert announce and records == [f"{SERIAL}|realloc"]


def test_wear_and_accepted_shapes_map_to_their_reading():
    wear = ("SSD wear at 95% of rated life -- consider scheduling replacement"
            " (from device statistics)")
    assert announcement_records([wear], SERIAL) == [f"{SERIAL}|wear_used"]
    accepted = "Reported Uncorrectable Errors: 18 (accepted 10; from device statistics)"
    assert announcement_records([accepted], SERIAL) == [f"{SERIAL}|unc"]


def test_records_are_per_serial_and_forgotten_with_the_drive():
    records = [f"{SERIAL}|unc", "OTHER|unc", f"{SERIAL}|realloc"]
    assert forget_serial(records, SERIAL) == ["OTHER|unc"]
    assert forget_serial(records, "") == records
    assert forget_serial(records, None) == records
    assert baseline_announcement([UNC_18], SERIAL, ["OTHER|unc"]) == (True, [f"{SERIAL}|unc"])


def test_the_announcement_line():
    assert ANNOUNCEMENT_LINE == "First reading from this drive's Device Statistics."
