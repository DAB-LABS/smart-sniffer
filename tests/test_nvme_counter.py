"""An NVMe 128-bit counter at or above 2^64 is unreadable, not a count.

Seen on a Windows laptop: smartctl 7.5 reported media_errors as 1.4494e33 with
a 16-byte little-endian form whose low twelve bytes are zero and whose bytes 12
and 13 hold 18294, the drive's own num_err_log_entries. That is the next field
bleeding into this one, not a count of anything. The old code took any value
above zero as critical, so the drive read Attention YES for an impossible
number. One rule covers every drive: such a counter shows as unknown and never
counts toward Attention. No per-model lists.
"""

from __future__ import annotations

import importlib
import logging

from custom_components.smart_sniffer.attention import NVME_COUNTER_LIMIT, nvme_counter

extract = importlib.import_module("custom_components.smart_sniffer.extract")

LABEL = "NVMe Media Errors"
# Under fixtures/v080 on purpose: the top-level fixtures are the v0.7.0 golden
# set, recorded at 36ebe65, and this payload reads differently by design.
UNREADABLE = "v080/nvme_media_errors_unreadable"


# --- the helper ---------------------------------------------------------------


def test_sane_values_read_as_ints():
    assert nvme_counter({"media_errors": 0}, "media_errors") == 0
    assert nvme_counter({"media_errors": 3}, "media_errors") == 3
    assert nvme_counter({"media_errors": 3.0}, "media_errors") == 3
    assert nvme_counter({"media_errors": NVME_COUNTER_LIMIT - 1}, "media_errors") == 2**64 - 1


def test_the_digits_form_wins_over_the_float():
    # smartctl adds "<key>_s" when the float would lose precision.
    log = {"media_errors": 9.007199254740993e15, "media_errors_s": "9007199254740993"}
    assert nvme_counter(log, "media_errors") == 9007199254740993


def test_at_or_above_2_to_the_64_is_unreadable():
    assert nvme_counter({"media_errors": NVME_COUNTER_LIMIT}, "media_errors") is None
    assert nvme_counter({"media_errors": 1.4494000050359518e33}, "media_errors") is None
    assert nvme_counter(
        {"media_errors": 1.4494000050359518e33, "media_errors_s": "1449400005035951791936293027446784"},
        "media_errors",
    ) is None


def test_the_le_byte_array_alone_marks_it_unreadable():
    log = {"media_errors": 5, "media_errors_le": [5, 0, 0, 0, 0, 0, 0, 0, 1]}
    assert nvme_counter(log, "media_errors") is None


def test_absent_or_junk_is_none():
    assert nvme_counter({}, "media_errors") is None
    assert nvme_counter({"media_errors": None}, "media_errors") is None
    assert nvme_counter({"media_errors": True}, "media_errors") is None
    assert nvme_counter({"media_errors": "n/a"}, "media_errors") is None
    assert nvme_counter({"media_errors": -1}, "media_errors") is None
    assert nvme_counter({"media_errors": float("inf")}, "media_errors") is None


# --- Attention ----------------------------------------------------------------


def test_the_laptop_pattern_is_not_a_reason(att, drive):
    state, severity, reasons, accepted = att.evaluate_attention(drive(UNREADABLE))
    assert state == "NO"
    assert severity == "none"
    assert reasons == []
    assert accepted == []


def test_a_threshold_cannot_accept_what_was_never_read(att, drive):
    state, _, reasons, accepted = att.evaluate_attention(
        drive(UNREADABLE), {LABEL: 5}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == []


def test_a_real_count_on_the_same_drive_still_alerts(att, drive):
    payload = drive(UNREADABLE)
    log = payload["smart_data"]["nvme_smart_health_information_log"]
    del log["media_errors_s"], log["media_errors_le"]
    log["media_errors"] = 1
    state, severity, reasons, _ = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert "NVMe media errors: 1 (expected 0)" in reasons


def test_the_thresholds_form_shows_it_as_not_reported(att, drive):
    readings = att.current_readings(drive(UNREADABLE))
    assert LABEL not in readings
    assert readings["SSD Wear Percent Used"] == 8
    placeholders = att.reading_placeholders(readings, "drive")
    assert placeholders[att.threshold_slug(LABEL)] == "not reported"


def test_unreadable_is_logged_once_per_drive(att, drive, caplog):
    att._UNREADABLE_LOGGED.clear()
    payload = drive(UNREADABLE)
    payload["id"] = "fx-nvme-unreadable"
    with caplog.at_level(logging.DEBUG, logger=att.__name__):
        att.evaluate_attention(payload)
        att.evaluate_attention(payload)
    lines = [r for r in caplog.records if "not readable" in r.getMessage()]
    assert len(lines) == 1
    assert "fx-nvme-unreadable" in lines[0].getMessage()
    assert "1449400005035951791936293027446784" in lines[0].getMessage()


# --- the sensors --------------------------------------------------------------


def test_the_media_errors_sensor_reads_unknown(drive):
    payload = drive(UNREADABLE)
    assert extract._extract_attribute(payload, "media_errors") is None
    assert extract._extract_attribute(payload, "reported_uncorrectable_errors") is None
    # The other NVMe readings on the same drive are untouched.
    assert extract._extract_attribute(payload, "critical_warning") == 0
    assert extract._extract_attribute(payload, "power_on_hours") == 10733


def test_the_media_errors_sensor_still_reads_a_count(drive):
    payload = drive("nvme_media_errors")
    assert extract._extract_attribute(payload, "media_errors") == 3
    assert extract._extract_attribute(payload, "reported_uncorrectable_errors") == 3
