"""Baseline regression tests for attention.py.

These pin behaviour that has already shipped, written before configurable
thresholds (#36) change any of it. Every expectation here describes what the
integration does today; if one of these starts failing, behaviour moved.

State and severity are asserted as string literals rather than through the
module's own constants, because those strings are the contract Home Assistant
sees in entity state. Comparing a constant to itself would not catch a change
to its value.
"""

from __future__ import annotations

import json
import logging

import pytest


def test_fixture_wrapper_canary(att, drive, fixture_names):
    """No fixture may read as UNSUPPORTED.

    UNSUPPORTED is what evaluate_attention returns when the payload wrapper is
    wrong, so a silent shape mistake would make several tests below pass while
    asserting nothing. This fails loudly instead.
    """
    assert fixture_names, "no fixture files were found at all"
    for name in fixture_names:
        state, _, _ = att.evaluate_attention(drive(name))
        assert state != "UNSUPPORTED", f"fixture {name!r} is not being parsed"


# ===========================================================================
# coerce_smart_data
# ===========================================================================
# Tested directly, not only through evaluate_attention. This helper is the
# single coercion point for attention.py, sensor.py and binary_sensor.py, and
# the call sites in the latter two are not reachable from tier 1 because those
# modules import Home Assistant.


def test_coerce_passes_a_dict_through_unchanged(att):
    payload = {"smart_status": {"passed": True}}
    assert att.coerce_smart_data({"smart_data": payload}) is payload


def test_coerce_parses_a_json_string(att):
    payload = {"smart_status": {"passed": True}}
    result = att.coerce_smart_data({"smart_data": json.dumps(payload)})
    assert result == payload


@pytest.mark.parametrize(
    ("label", "drive_data"),
    [
        ("null smart_data", {"smart_data": None}),
        ("missing key", {}),
        ("a list", {"smart_data": [1, 2, 3]}),
        ("a JSON string holding a list", {"smart_data": "[1, 2, 3]"}),
        ("an unparseable string", {"smart_data": "not json at all"}),
        ("an empty string", {"smart_data": ""}),
        ("an int", {"smart_data": 42}),
    ],
)
def test_coerce_turns_unusable_input_into_empty_dict(att, label, drive_data):
    assert att.coerce_smart_data(drive_data) == {}, label


def test_coerce_logs_the_arriving_type_for_unusable_input(att, caplog):
    """The debug line names the type, which is the whole diagnostic value:
    NoneType points at an old agent, str at JSON that would not parse."""
    with caplog.at_level(logging.DEBUG, logger=att.__name__):
        att.coerce_smart_data({"id": "sda-wfl3abcd", "smart_data": None})

    assert "sda-wfl3abcd" in caplog.text
    assert "NoneType" in caplog.text


def test_coerce_is_silent_on_the_happy_paths(att, caplog, drive):
    """A dict and a parseable JSON string are normal, not worth a line.

    v0.6.0 removed recurring log noise; this makes sure the coercion does not
    quietly reintroduce a per-poll line for healthy drives.
    """
    smart_data = drive("ata_healthy")["smart_data"]
    with caplog.at_level(logging.DEBUG, logger=att.__name__):
        att.coerce_smart_data({"smart_data": smart_data})
        att.coerce_smart_data({"smart_data": json.dumps(smart_data)})

    assert caplog.text == ""


# ===========================================================================
# Caller behaviour must not change
# ===========================================================================
# The helper returns {} for anything unusable, where each call site used to
# return its own value directly. Each caller's "no usable data" path has to
# produce the result it produced before.


def test_unparseable_string_still_returns_unsupported(att):
    """The exact regression the refactor could have broken."""
    state, severity, reasons = att.evaluate_attention({"smart_data": "not json"})
    assert (state, severity, reasons) == ("UNSUPPORTED", "none", [])


def test_valid_json_string_still_parses(att, drive):
    """Some agents send smart_data as a JSON string. That must keep working."""
    as_dict = drive("ata_reallocated")
    as_string = {"smart_data": json.dumps(as_dict["smart_data"])}

    assert att.evaluate_attention(as_string) == att.evaluate_attention(as_dict)
    assert att.evaluate_attention(as_string)[0] == "YES"


# --- 11. Empty and malformed payloads never raise -------------------------
# Every one of these raised AttributeError before this change.
@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("empty drive payload", {}),
        ("smart_data missing", {"other": 1}),
        ("smart_data empty dict", {"smart_data": {}}),
        ("smart_data null", {"smart_data": None}),
        ("smart_data unparseable string", {"smart_data": "not json at all"}),
        ("smart_data empty string", {"smart_data": ""}),
        ("smart_data a JSON list", {"smart_data": "[1, 2, 3]"}),
        ("smart_data a real list", {"smart_data": [1, 2, 3]}),
        ("empty ata table", {"smart_data": {"ata_smart_attributes": {"table": []}}}),
        ("ata_smart_attributes null, nothing else", {"smart_data": {"ata_smart_attributes": None}}),
    ],
)
def test_unusable_smart_data_is_unsupported(att, label, payload):
    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "UNSUPPORTED", label
    assert severity == "none", label
    assert reasons == [], label


def test_null_ata_attributes_with_a_passing_status_is_clear(att):
    """A null attribute table is not by itself "no data".

    smart_status is present and passing, so the drive is evaluable and the
    answer is NO. What matters here is that it does not raise: both the
    data-quality gate and the ATA evaluation dereference this key.
    """
    state, severity, reasons = att.evaluate_attention(
        {
            "smart_data": {
                "smart_status": {"passed": True},
                "ata_smart_attributes": None,
            }
        }
    )
    assert (state, severity, reasons) == ("NO", "none", [])


def test_null_ata_attributes_does_not_mask_a_failed_status(att):
    """A null attribute table must not swallow the overall SMART verdict.

    This is the second of the two null sites: the data-quality gate and the
    ATA evaluation each dereferenced ata_smart_attributes, so patching one
    still raised at the other.
    """
    state, severity, reasons = att.evaluate_attention(
        {
            "smart_data": {
                "smart_status": {"passed": False},
                "ata_smart_attributes": None,
            }
        }
    )
    assert state == "YES"
    assert severity == "critical"
    assert "SMART overall status: FAILED" in reasons


# ===========================================================================
# Baseline cases 1 to 12
# ===========================================================================


# --- 1. Healthy ATA drive --------------------------------------------------
def test_healthy_ata_drive_is_clear(att, drive):
    state, severity, reasons = att.evaluate_attention(drive("ata_healthy"))
    assert state == "NO"
    assert severity == "none"
    assert reasons == []


# --- 2. 147 reallocated sectors -------------------------------------------
def test_reallocated_sectors_are_critical(att, drive):
    state, severity, reasons = att.evaluate_attention(drive("ata_reallocated"))
    assert state == "YES"
    assert severity == "critical"
    # The critical reason leads, with the warning-tier event count appended.
    assert reasons == [
        "Reallocated Sector Count: 147 (expected 0)",
        "Reallocated Event Count: 47 (expected 0)",
    ]


# --- 3. SK Hynix vendor name, non-zero (the #27 fix) -----------------------
def test_skhynix_retired_block_count_is_critical(att, drive, set_ata_raw):
    """Retired_Block_Count must alert exactly as Reallocated_Sector_Ct does.

    The 1516 figure is the count from the SK Hynix drive in the first EDR
    (issue-app3-response-gbravery.md), which reported under the standard
    attribute name. Pairing it with jackeichen's vendor spelling reproduces the
    before/after check recorded in the #27 response doc, where this payload
    returned NO before the fix and YES after it. It is a deliberate synthetic
    case, not a captured dump: jackeichen's own drive reads 0.
    """
    payload = drive("ata_skhynix")
    set_ata_raw(payload, "Retired_Block_Count", 1516)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert reasons == ["Reallocated Sector Count: 1516 (expected 0)"]


# --- 4. SK Hynix vendor name, zero ----------------------------------------
def test_skhynix_healthy_drive_is_clear(att, drive):
    """jackeichen's drive as actually submitted: 0 retired blocks."""
    state, severity, reasons = att.evaluate_attention(drive("ata_skhynix"))
    assert state == "NO"
    assert severity == "none"
    assert reasons == []


def test_vendor_and_standard_names_agree(att, drive, set_ata_raw):
    """Both spellings of attribute 5 must produce identical output."""
    vendor = drive("ata_skhynix")
    set_ata_raw(vendor, "Retired_Block_Count", 1516)

    standard = drive("ata_skhynix")
    for attr in standard["smart_data"]["ata_smart_attributes"]["table"]:
        if attr["name"] == "Retired_Block_Count":
            attr["name"] = "Reallocated_Sector_Ct"
    set_ata_raw(standard, "Reallocated_Sector_Ct", 1516)

    assert att.evaluate_attention(vendor) == att.evaluate_attention(standard)


# --- 5 and 6. Command_Timeout uses a threshold, not zero tolerance ---------
def test_command_timeout_above_threshold_warns(att, drive, set_ata_raw):
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 250)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "MAYBE"
    assert severity == "warning"
    assert reasons == ["Command Timeout: 250 (threshold 100)"]


def test_command_timeout_below_threshold_is_clear(att, drive, set_ata_raw):
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 50)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "NO"
    assert severity == "none"
    assert reasons == []


# --- 7. SMART overall status FAILED ---------------------------------------
def test_smart_status_failed_is_critical(att, drive):
    payload = drive("ata_healthy")
    payload["smart_data"]["smart_status"]["passed"] = False

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert "SMART overall status: FAILED" in reasons


# --- 8. NVMe media errors --------------------------------------------------
def test_nvme_media_errors_are_critical(att, drive):
    state, severity, reasons = att.evaluate_attention(drive("nvme_media_errors"))
    assert state == "YES"
    assert severity == "critical"
    assert "NVMe media errors: 3 (expected 0)" in reasons


# --- 9. NVMe spare at the drive's own threshold ----------------------------
def test_nvme_spare_at_drive_threshold_is_critical(att, drive, set_nvme):
    """The <= boundary: equal to the drive's own threshold already counts."""
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=10, available_spare_threshold=10)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert reasons == [
        "NVMe available spare (10%) at or below drive threshold (10%)"
    ]


# --- 10. NVMe spare above the drive threshold but under the warn tier ------
def test_nvme_spare_low_warns(att, drive, set_nvme):
    """15% clears the drive's own threshold of 10 but trips the hardcoded 20."""
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=15, available_spare_threshold=10)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "MAYBE"
    assert severity == "warning"
    assert reasons == ["NVMe available spare low: 15% remaining"]


# --- 12. One logical attribute reported under two names -------------------
def test_duplicate_label_reports_once(att, drive, set_ata_raw, add_ata_attr):
    """Three raw names map to Current Pending Sector Count.

    A drive reporting two of them must produce one reason, not two. The
    threshold work changes this path, so the current behaviour is pinned here
    first.
    """
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Current_Pending_Sector", 5)
    add_ata_attr(payload, 197, "Total_Pending_Sectors", 5)

    state, severity, reasons = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    pending = [r for r in reasons if r.startswith("Current Pending Sector Count")]
    assert pending == ["Current Pending Sector Count: 5 (expected 0)"]
