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
        state, _, _, _ = att.evaluate_attention(drive(name))
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
    state, severity, reasons, accepted = att.evaluate_attention({"smart_data": "not json"})
    assert (state, severity, reasons, accepted) == ("UNSUPPORTED", "none", [], [])


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
    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "UNSUPPORTED", label
    assert severity == "none", label
    assert reasons == [], label


def test_null_ata_attributes_with_a_passing_status_is_clear(att):
    """A null attribute table is not by itself "no data".

    smart_status is present and passing, so the drive is evaluable and the
    answer is NO. What matters here is that it does not raise: both the
    data-quality gate and the ATA evaluation dereference this key.
    """
    state, severity, reasons, accepted = att.evaluate_attention(
        {
            "smart_data": {
                "smart_status": {"passed": True},
                "ata_smart_attributes": None,
            }
        }
    )
    assert (state, severity, reasons, accepted) == ("NO", "none", [], [])


def test_null_ata_attributes_does_not_mask_a_failed_status(att):
    """A null attribute table must not swallow the overall SMART verdict.

    This is the second of the two null sites: the data-quality gate and the
    ATA evaluation each dereferenced ata_smart_attributes, so patching one
    still raised at the other.
    """
    state, severity, reasons, accepted = att.evaluate_attention(
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
    state, severity, reasons, accepted = att.evaluate_attention(drive("ata_healthy"))
    assert state == "NO"
    assert severity == "none"
    assert reasons == []


# --- 2. 147 reallocated sectors -------------------------------------------
def test_reallocated_sectors_are_critical(att, drive):
    state, severity, reasons, accepted = att.evaluate_attention(drive("ata_reallocated"))
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

    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert reasons == ["Reallocated Sector Count: 1516 (expected 0)"]


# --- 4. SK Hynix vendor name, zero ----------------------------------------
def test_skhynix_healthy_drive_is_clear(att, drive):
    """jackeichen's drive as actually submitted: 0 retired blocks."""
    state, severity, reasons, accepted = att.evaluate_attention(drive("ata_skhynix"))
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

    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "MAYBE"
    assert severity == "warning"
    assert reasons == ["Command Timeout: 250 (threshold 100)"]


def test_command_timeout_below_threshold_is_clear(att, drive, set_ata_raw):
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 50)

    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "NO"
    assert severity == "none"
    assert reasons == []


# --- 7. SMART overall status FAILED ---------------------------------------
def test_smart_status_failed_is_critical(att, drive):
    payload = drive("ata_healthy")
    payload["smart_data"]["smart_status"]["passed"] = False

    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    assert "SMART overall status: FAILED" in reasons


# --- 8. NVMe media errors --------------------------------------------------
def test_nvme_media_errors_are_critical(att, drive):
    state, severity, reasons, accepted = att.evaluate_attention(drive("nvme_media_errors"))
    assert state == "YES"
    assert severity == "critical"
    assert "NVMe media errors: 3 (expected 0)" in reasons


# --- 9. NVMe spare at the drive's own threshold ----------------------------
def test_nvme_spare_at_drive_threshold_is_critical(att, drive, set_nvme):
    """The <= boundary: equal to the drive's own threshold already counts."""
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=10, available_spare_threshold=10)

    state, severity, reasons, accepted = att.evaluate_attention(payload)
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

    state, severity, reasons, accepted = att.evaluate_attention(payload)
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

    state, severity, reasons, accepted = att.evaluate_attention(payload)
    assert state == "YES"
    assert severity == "critical"
    pending = [r for r in reasons if r.startswith("Current Pending Sector Count")]
    assert pending == ["Current Pending Sector Count: 5 (expected 0)"]


# ===========================================================================
# Configurable alert thresholds (#36)
# ===========================================================================


@pytest.fixture
def silence_everything(att):
    """Thresholds set so that nothing a user CAN configure would ever alert.

    Note the NVMe spare key counts down, so silencing it means setting it to
    zero, not to a huge number. Setting it high would make it alert harder,
    which is the trap the plan warns about.
    """

    def _build() -> dict[str, int]:
        limits = {label: 10**9 for label in att.threshold_labels()}
        limits[att.LABEL_NVME_SPARE_WARN] = 0
        return limits

    return _build


# --- get_thresholds reads entry.data, never entry.options -----------------
class _FakeEntry:
    """Stand-in for a ConfigEntry. Only .data and .options are touched."""

    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


def test_get_thresholds_reads_entry_data(att):
    entry = _FakeEntry({"thresholds": {"drive-1": {"Reallocated Sector Count": 147}}})
    assert att.get_thresholds(entry, "drive-1") == {"Reallocated Sector Count": 147}


def test_get_thresholds_ignores_entry_options(att):
    """The v1 plan's defect, pinned.

    This integration's options flow merges into entry.data and calls
    async_create_entry(data={}), so options is permanently empty. A lookup that
    read options would return {} forever and the feature would silently do
    nothing, with no error anywhere.
    """
    entry = _FakeEntry(
        data={},
        options={"thresholds": {"drive-1": {"Reallocated Sector Count": 147}}},
    )
    assert att.get_thresholds(entry, "drive-1") == {}


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("no thresholds key", _FakeEntry({})),
        ("thresholds null", _FakeEntry({"thresholds": None})),
        ("drive absent", _FakeEntry({"thresholds": {"other": {"x": 1}}})),
        ("drive entry null", _FakeEntry({"thresholds": {"drive-1": None}})),
        ("drive entry not a dict", _FakeEntry({"thresholds": {"drive-1": 5}})),
    ],
)
def test_get_thresholds_tolerates_absence_and_junk(att, label, entry):
    assert att.get_thresholds(entry, "drive-1") == {}, label


# --- 1. No thresholds means no accepted entries, on every fixture ---------
def test_no_thresholds_produces_no_accepted(att, drive, fixture_names):
    """The byte-identical guarantee, across every fixture at once.

    Accepted entries exist only where the user moved a threshold. A reading
    that was never going to alert is not "accepted", and emitting it would put
    noise on healthy drives.
    """
    for name in fixture_names:
        _, _, _, accepted = att.evaluate_attention(drive(name))
        assert accepted == [], f"{name} produced accepted entries with no thresholds"


# --- 3, 4, 5. A threshold on the reallocated sector count ------------------
def test_threshold_equal_to_reading_accepts_it(att, drive):
    """nsleigh's case: 147 reallocated, accepted as the known baseline."""
    state, severity, reasons, accepted = att.evaluate_attention(
        drive("ata_reallocated"),
        {"Reallocated Sector Count": 147, "Reallocated Event Count": 47},
    )
    assert state == "NO"
    assert severity == "none"
    assert reasons == []
    assert accepted == [
        "Reallocated Sector Count: 147 (accepted 147)",
        "Reallocated Event Count: 47 (accepted 47)",
    ]


def test_reading_above_threshold_still_alerts(att, drive, set_ata_raw):
    """The drive got worse. That is exactly what a threshold must not hide."""
    payload = drive("ata_reallocated")
    set_ata_raw(payload, "Reallocated_Sector_Ct", 152)

    state, severity, reasons, _ = att.evaluate_attention(
        payload, {"Reallocated Sector Count": 147}
    )
    assert state == "YES"
    assert severity == "critical"
    assert "Reallocated Sector Count: 152 (accepted 147)" in reasons


def test_reading_below_threshold_is_accepted(att, drive, set_ata_raw):
    payload = drive("ata_reallocated")
    set_ata_raw(payload, "Reallocated_Sector_Ct", 100)

    state, _, _, accepted = att.evaluate_attention(
        payload, {"Reallocated Sector Count": 147, "Reallocated Event Count": 47}
    )
    assert state == "NO"
    assert "Reallocated Sector Count: 100 (accepted 147)" in accepted


# --- 6. The threshold is keyed by label, so vendor aliases inherit it ------
def test_threshold_applies_through_the_skhynix_alias(att, drive, set_ata_raw):
    """The drive reports Retired_Block_Count; the threshold names the label.

    This is why keying is by label and not by raw attribute name. Without it a
    user would need a separate entry per vendor spelling of the same attribute.
    """
    payload = drive("ata_skhynix")
    set_ata_raw(payload, "Retired_Block_Count", 1516)

    state, _, reasons, accepted = att.evaluate_attention(
        payload, {"Reallocated Sector Count": 1516}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == ["Reallocated Sector Count: 1516 (accepted 1516)"]


# --- 7, 8. Command Timeout has a non-zero default -------------------------
def test_command_timeout_uses_its_default_when_unset(att, drive, set_ata_raw):
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 250)

    state, _, reasons, accepted = att.evaluate_attention(payload)
    assert state == "MAYBE"
    assert reasons == ["Command Timeout: 250 (threshold 100)"]
    assert accepted == []


def test_command_timeout_threshold_raises_the_bar(att, drive, set_ata_raw):
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 250)

    state, _, reasons, accepted = att.evaluate_attention(
        payload, {"Command Timeout": 300}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == ["Command Timeout: 250 (accepted 300)"]


# --- 9. NVMe media errors --------------------------------------------------
def test_nvme_media_errors_threshold_accepts_the_reading(att, drive):
    state, _, reasons, accepted = att.evaluate_attention(
        drive("nvme_media_errors"), {"NVMe Media Errors": 3}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == ["NVMe media errors: 3 (accepted 3)"]


# --- 10. SAFETY GUARD: a FAILED drive cannot be silenced ------------------
def test_failed_smart_status_cannot_be_silenced(att, drive, silence_everything):
    """A drive that says it has failed stays critical, whatever the user set.

    SMART overall status is not a threshold. If this test ever goes green on a
    NO, the feature has become capable of hiding a dying drive.
    """
    payload = drive("ata_reallocated")
    payload["smart_data"]["smart_status"]["passed"] = False

    state, severity, reasons, _ = att.evaluate_attention(payload, silence_everything())
    assert state == "YES"
    assert severity == "critical"
    assert "SMART overall status: FAILED" in reasons


# --- 16. SAFETY GUARD: out of spare blocks cannot be silenced -------------
def test_spare_below_the_drives_own_threshold_cannot_be_silenced(
    att, drive, set_nvme, silence_everything
):
    """The drive reporting its own manufacturer limit is not configurable.

    Only the earlier warning tier is. This is the device declaring its limit,
    the same category as SMART FAILED.
    """
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=5, available_spare_threshold=10)

    state, severity, reasons, _ = att.evaluate_attention(payload, silence_everything())
    assert state == "YES"
    assert severity == "critical"
    assert reasons == [
        "NVMe available spare (5%) at or below drive threshold (10%)"
    ]


# --- 15. The NVMe spare WARNING tier is configurable, and counts down -----
def test_nvme_spare_warn_threshold_is_configurable(att, drive, set_nvme):
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=15, available_spare_threshold=10)

    state, _, reasons, accepted = att.evaluate_attention(
        payload, {"NVMe Spare Warn Below": 10}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == ["NVMe available spare: 15% (accepted down to 10%)"]


def test_nvme_spare_warn_direction_is_not_inverted(att, drive, set_nvme):
    """Raising the spare threshold makes it alert SOONER, not later.

    Every other key alerts above its threshold; this one alerts below. Pasting
    the comparison from a neighbouring row would invert this silently.
    """
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=30, available_spare_threshold=10)

    quiet, _, _, _ = att.evaluate_attention(payload)
    loud, _, reasons, _ = att.evaluate_attention(payload, {"NVMe Spare Warn Below": 50})
    assert quiet == "NO"
    assert loud == "MAYBE"
    assert reasons == [
        "NVMe available spare low: 30% remaining (accepted down to 50%)"
    ]


# --- 11. A threshold naming an attribute the drive does not report --------
def test_threshold_on_an_absent_attribute_is_ignored(att, drive):
    before = att.evaluate_attention(drive("ata_reallocated"))
    after = att.evaluate_attention(
        drive("ata_reallocated"), {"Spin Retry Count": 500}
    )
    assert before == after


# --- 13. Malformed stored thresholds never raise --------------------------
@pytest.mark.parametrize(
    "bad",
    # 3.7 is deliberately absent: a float is coercible, not malformed, and
    # test_float_threshold_is_coerced pins that separately. Bools are here
    # because bool subclasses int, so int(True) would otherwise be 1.
    ["abc", None, "", [], {}, True, False],
)
def test_malformed_threshold_falls_back_to_the_default(att, drive, bad):
    """Stored config is user-writable and survives downgrades.

    Anything unusable degrades to the built-in default, which for this label is
    zero tolerance, so the drive keeps alerting rather than going quiet.
    """
    state, _, reasons, _ = att.evaluate_attention(
        drive("ata_reallocated"), {"Reallocated Sector Count": bad}
    )
    assert state == "YES"
    assert "Reallocated Sector Count: 147 (expected 0)" in reasons


def test_float_threshold_is_coerced(att, drive, set_ata_raw):
    payload = drive("ata_reallocated")
    set_ata_raw(payload, "Reallocated_Sector_Ct", 100)
    state, _, _, accepted = att.evaluate_attention(
        payload, {"Reallocated Sector Count": 147.0, "Reallocated Event Count": 47}
    )
    assert state == "NO"
    assert "Reallocated Sector Count: 100 (accepted 147)" in accepted


# --- 13b. Unusable payload with thresholds set ----------------------------
def test_unusable_payload_with_thresholds_is_unsupported(att):
    assert att.evaluate_attention(
        {"smart_data": None}, {"Reallocated Sector Count": 147}
    ) == ("UNSUPPORTED", "none", [], [])


# --- 14. seen_labels must dedupe accepted entries too ---------------------
def test_duplicate_label_accepts_once(att, drive, set_ata_raw, add_ata_attr):
    """Three raw names map to Current Pending Sector Count.

    Before #36 the dedup only had to cover reasons, because nothing was emitted
    when nothing fired. Accepted entries break that assumption: two aliases both
    under threshold would each emit an identical line.
    """
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Current_Pending_Sector", 5)
    add_ata_attr(payload, 197, "Total_Pending_Sectors", 5)

    state, _, reasons, accepted = att.evaluate_attention(
        payload, {"Current Pending Sector Count": 10}
    )
    assert state == "NO"
    assert reasons == []
    assert accepted == ["Current Pending Sector Count: 5 (accepted 10)"]


# ===========================================================================
# compose_reasons_text: the Attention Reasons entity state
# ===========================================================================
# Lives in attention.py rather than sensor.py precisely so these can run. The
# plan put the 255-character cap inline in sensor.py, which imports Home
# Assistant and is therefore unreachable from tier 1.


def test_reasons_text_clear_drive(att):
    assert att.compose_reasons_text("NO", [], []) == "No issues detected"


def test_reasons_text_shows_accepted_on_a_clear_drive(att):
    """Before #36 a STATE_NO drive discarded its reasons entirely.

    If this regressed, an accepted baseline would become invisible and the
    feature would look like it had done nothing.
    """
    assert att.compose_reasons_text(
        "NO", [], ["Reallocated Sector Count: 147 (accepted 147)"]
    ) == "No issues detected (accepted: Reallocated Sector Count: 147 (accepted 147))"


def test_reasons_text_joins_actionable_reasons(att):
    assert att.compose_reasons_text("YES", ["one", "two"], []) == "one; two"


def test_reasons_text_appends_accepted_to_actionable(att):
    assert (
        att.compose_reasons_text("MAYBE", ["one"], ["two", "three"])
        == "one (accepted: two; three)"
    )


def test_reasons_text_unsupported(att):
    assert att.compose_reasons_text("UNSUPPORTED", ["ignored"], ["ignored"]) == (
        "No usable SMART data"
    )


# --- 12. The 255-character cap --------------------------------------------
def test_reasons_text_truncates_accepted_first(att):
    """Home Assistant truncates a state over 255 chars and logs an error.

    Actionable reasons survive; the accepted section is what gets trimmed,
    because that is the part the user does not need to act on. Full detail
    stays in the entity attributes either way.
    """
    reasons = ["Reallocated Sector Count: 147 (expected 0)"]
    accepted = [f"Some Accepted Attribute Number {i}: {i} (accepted {i})" for i in range(20)]

    composed = att.compose_reasons_text("YES", reasons, accepted)

    assert len(composed) <= 255
    assert composed.startswith("Reallocated Sector Count: 147 (expected 0)")
    assert composed.endswith("...)")


def test_reasons_text_truncates_reasons_when_they_alone_overflow(att):
    reasons = ["x" * 400]
    composed = att.compose_reasons_text("YES", reasons, ["accepted thing"])
    assert len(composed) == 255
    assert composed.endswith("...")


def test_reasons_text_at_the_boundary_is_not_truncated(att):
    """Exactly 255 must pass through untouched; only 256 gets cut."""
    exact = att.compose_reasons_text("YES", ["y" * 255], [])
    assert len(exact) == 255
    assert "..." not in exact

    over = att.compose_reasons_text("YES", ["y" * 256], [])
    assert len(over) == 255
    assert over.endswith("...")


def test_reasons_text_end_to_end_stays_within_the_limit(att, drive, set_ata_raw):
    """Composed from a real evaluation rather than hand-built lists."""
    payload = drive("ata_reallocated")
    set_ata_raw(payload, "Reallocated_Sector_Ct", 147)
    state, _, reasons, accepted = att.evaluate_attention(
        payload, {"Reallocated Sector Count": 147}
    )
    composed = att.compose_reasons_text(state, reasons, accepted)
    assert len(composed) <= 255
    assert "accepted" in composed


# ===========================================================================
# Options-flow support helpers
# ===========================================================================
# These back the threshold editor. They live in attention.py so tier 1 can
# reach them; config_flow.py imports Home Assistant and cannot be tested here.


def test_labels_for_an_ata_drive_exclude_nvme_only_keys(att, drive):
    labels = att.labels_for_drive(drive("ata_reallocated"))
    assert "Spin Retry Count" in labels
    assert att.LABEL_SSD_WEAR in labels
    assert att.LABEL_NVME_SPARE_WARN not in labels
    assert att.LABEL_NVME_MEDIA_ERRORS not in labels


def test_labels_for_an_nvme_drive_exclude_ata_only_keys(att, drive):
    labels = att.labels_for_drive(drive("nvme_healthy"))
    assert att.LABEL_NVME_SPARE_WARN in labels
    assert att.LABEL_NVME_MEDIA_ERRORS in labels
    assert att.LABEL_SSD_WEAR in labels
    assert "Spin Retry Count" not in labels


def test_labels_for_an_unusable_payload_do_not_raise(att):
    assert att.labels_for_drive({"smart_data": None})


def test_current_readings_ata(att, drive):
    readings = att.current_readings(drive("ata_reallocated"))
    assert readings["Reallocated Sector Count"] == 147
    assert readings["Reallocated Event Count"] == 47
    # Zero readings are kept, unlike evaluation, so the form can show them.
    assert readings["Current Pending Sector Count"] == 0


def test_current_readings_nvme(att, drive, set_nvme):
    payload = drive("nvme_healthy")
    set_nvme(payload, media_errors=4, available_spare=77, percentage_used=12)
    readings = att.current_readings(payload)
    assert readings[att.LABEL_NVME_MEDIA_ERRORS] == 4
    assert readings[att.LABEL_NVME_SPARE_WARN] == 77
    assert readings[att.LABEL_SSD_WEAR] == 12


def test_current_readings_decodes_packed_command_timeout(att, drive, set_ata_raw):
    """Seagate packs three counters into the raw value; the form must not show
    the packed integer."""
    payload = drive("ata_healthy")
    set_ata_raw(payload, "Command_Timeout", 0x00DB00DB00DE)
    assert att.current_readings(payload)["Command Timeout"] == 0xDE


# ===========================================================================
# The threshold form (round 5: one flat form)
# ===========================================================================
# One form per drive. Each field is prefilled with the value in force and shows
# "Currently: N. Default: M." under it. Typing the default clears an override.
# These helpers live in attention.py because config_flow.py cannot be imported
# without Home Assistant.


def test_the_label_set_is_exactly_ten_counters_and_two_gauges(att):
    """If a label is added without deciding which kind it is, this fails.
    Gauges sort last in the form and show their values as percentages."""
    labels = set(att.threshold_labels())
    assert len(labels) == 12
    assert len(att.GAUGE_LABELS) == 2
    assert att.GAUGE_LABELS <= labels


# --- Accepting known damage by typing the current reading -----------------
# The form tells the user to set a damage count to its current reading to
# accept it. That only works because every counter compares strictly.


def test_every_counter_compares_strictly(att):
    """147 > 147 is false, so a counter set to its own reading does not alert.
    A non-strict counter would alert at exactly the value the form told the
    user to type."""
    for label in att.threshold_labels():
        if label not in att.GAUGE_LABELS:
            compare = att._THRESHOLD_COMPARE.get(label, att._COMPARE_GT)
            assert compare == att._COMPARE_GT, f"{label} compares {compare}"


def test_counters_set_to_their_readings_silence_the_drive(att, drive):
    payload = drive("ata_reallocated")
    readings = att.current_readings(payload)
    typed = {
        label: readings[label]
        for label in att.labels_for_drive(payload)
        if label in readings and label not in att.GAUGE_LABELS
    }
    state, _, reasons, accepted = att.evaluate_attention(payload, typed)
    assert state == "NO", reasons
    assert "Reallocated Sector Count: 147 (accepted 147)" in accepted


def test_wear_set_to_its_reading_alerts_at_once(att, drive, add_ata_attr):
    """Why the form tells the user to leave wear and spare near their defaults.
    Wear compares inclusively, so typing today's reading alerts immediately."""
    payload = drive("ata_healthy")
    add_ata_attr(payload, 231, "SSD_Life_Left", 88)
    payload["smart_data"]["ata_smart_attributes"]["table"][-1]["value"] = 88
    assert att.current_readings(payload)[att.LABEL_SSD_WEAR] == 12

    state, _, _, _ = att.evaluate_attention(payload, {att.LABEL_SSD_WEAR: 12})
    assert state == "MAYBE"


# --- Prefill: the stored override, else the default ------------------------


def test_prefill_uses_overrides_then_defaults(att, drive):
    values = att.prefill_thresholds(
        drive("ata_reallocated"), {"Reallocated Sector Count": 147}
    )
    assert values["Reallocated Sector Count"] == 147
    assert values["Reallocated Event Count"] == 0
    assert values["Command Timeout"] == 100
    assert values[att.LABEL_SSD_WEAR] == 90


def test_prefill_tolerates_a_malformed_override(att, drive):
    values = att.prefill_thresholds(
        drive("ata_reallocated"), {"Reallocated Sector Count": "abc"}
    )
    assert values["Reallocated Sector Count"] == 0


def test_prefill_covers_every_applicable_label(att, drive, fixture_names):
    """The form shows all of them, so the prefill must not leave a hole."""
    for name in fixture_names:
        payload = drive(name)
        assert set(att.prefill_thresholds(payload, {})) == set(
            att.labels_for_drive(payload)
        ), name


# --- Submit: typing the default clears an override --------------------------


def test_typing_the_default_clears_an_override(att, drive):
    """The owner's case: wear set to 50, then back to 90. The second submit
    must store nothing for wear, not 90."""
    labels = att.labels_for_drive(drive("ata_healthy"))
    assert att.overrides_from_form(labels, {att.LABEL_SSD_WEAR: 50}) == {
        att.LABEL_SSD_WEAR: 50
    }
    assert att.overrides_from_form(labels, {att.LABEL_SSD_WEAR: 90}) == {}


def test_submitting_the_prefill_unchanged_stores_only_the_overrides(att, drive):
    payload = drive("ata_reallocated")
    labels = att.labels_for_drive(payload)
    stored = {"Reallocated Sector Count": 147, "Command Timeout": 150}
    submitted = att.prefill_thresholds(payload, stored)
    assert att.overrides_from_form(labels, submitted) == stored


def test_empty_and_junk_fields_are_not_stored(att, drive):
    labels = att.labels_for_drive(drive("ata_healthy"))
    submitted = {"Reallocated Sector Count": "abc", "Spin Retry Count": None}
    assert att.overrides_from_form(labels, submitted) == {}


def test_a_label_the_drive_cannot_report_is_not_stored(att, drive):
    labels = att.labels_for_drive(drive("nvme_healthy"))
    assert att.overrides_from_form(labels, {"Spin Retry Count": 5}) == {}


def test_submitted_floats_are_stored_as_ints(att, drive):
    """NumberSelector returns floats."""
    labels = att.labels_for_drive(drive("ata_healthy"))
    assert att.overrides_from_form(labels, {"Command Timeout": 150.0}) == {
        "Command Timeout": 150
    }


# --- "Currently: N. Default: M." ------------------------------------------


def test_placeholders_carry_units_on_the_gauges(att, drive, set_nvme):
    payload = drive("nvme_healthy")
    set_nvme(payload, available_spare=77, percentage_used=12, media_errors=4)
    placeholders = att.reading_placeholders(
        att.current_readings(payload), "Some Drive (SERIAL)"
    )
    wear = att.threshold_slug(att.LABEL_SSD_WEAR)
    spare = att.threshold_slug(att.LABEL_NVME_SPARE_WARN)
    media = att.threshold_slug(att.LABEL_NVME_MEDIA_ERRORS)

    assert (placeholders[wear], placeholders[f"{wear}_default"]) == ("12%", "90%")
    assert (placeholders[spare], placeholders[f"{spare}_default"]) == ("77%", "20%")
    assert (placeholders[media], placeholders[f"{media}_default"]) == ("4", "0")
    assert placeholders["drive"] == "Some Drive (SERIAL)"


def test_placeholders_say_not_reported_for_absent_labels(att, drive):
    """An NVMe drive has no Command Timeout, and the form must say so rather
    than leaving an unfilled translation slot."""
    placeholders = att.reading_placeholders(
        att.current_readings(drive("nvme_healthy")), "d"
    )
    slug = att.threshold_slug("Command Timeout")
    assert placeholders[slug] == "not reported"
    assert placeholders[f"{slug}_default"] == "100"


def test_every_label_has_both_placeholders_whatever_the_drive(att, drive, fixture_names):
    """A missing placeholder renders as a translation error in the form."""
    for name in fixture_names:
        placeholders = att.reading_placeholders(
            att.current_readings(drive(name)), "d"
        )
        for label in att.threshold_labels():
            slug = att.threshold_slug(label)
            assert slug in placeholders, (name, label)
            assert f"{slug}_default" in placeholders, (name, label)


# --- Field order ------------------------------------------------------------


def test_a_drive_with_damage_lists_the_damaged_labels_first(att, drive):
    payload = drive("ata_reallocated")
    order = att.form_order(
        att.labels_for_drive(payload), att.current_readings(payload)
    )
    assert order[:2] == ["Reallocated Event Count", "Reallocated Sector Count"]
    assert order[-1] == att.LABEL_SSD_WEAR


def test_a_clean_drive_keeps_label_order_with_gauges_last(att, drive):
    payload = drive("ata_healthy")
    labels = att.labels_for_drive(payload)
    order = att.form_order(labels, att.current_readings(payload))
    counters = [label for label in labels if label not in att.GAUGE_LABELS]
    assert order == counters + [att.LABEL_SSD_WEAR]


def test_nvme_order_puts_both_gauges_last(att, drive):
    payload = drive("nvme_media_errors")
    order = att.form_order(
        att.labels_for_drive(payload), att.current_readings(payload)
    )
    assert order[0] == att.LABEL_NVME_MEDIA_ERRORS
    assert set(order[-2:]) == att.GAUGE_LABELS


def test_form_order_is_a_permutation_of_the_labels(att, drive, fixture_names):
    for name in fixture_names:
        payload = drive(name)
        labels = att.labels_for_drive(payload)
        order = att.form_order(labels, att.current_readings(payload))
        assert sorted(order) == sorted(labels), name
