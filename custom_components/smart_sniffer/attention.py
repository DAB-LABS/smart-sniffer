"""SMART attention evaluation — shared module.

Contains all severity classification logic so it can be imported by both
sensor.py / binary_sensor.py (to expose state as entity attributes) and
coordinator.py (to fire persistent notifications on state transitions)
without creating a circular dependency.

Attention states
----------------
  STATE_YES          Critical — data integrity at risk. Back up immediately.
  STATE_MAYBE        Warning — early degradation signal. Plan replacement.
  STATE_NO           All monitored indicators clear.
  STATE_UNSUPPORTED  Drive returned no usable SMART data (e.g., USB bridge
                     blocking SMART passthrough). Monitoring is not possible.

Severity constants (used in attributes and notification formatting)
-------------------------------------------------------------------
  SEVERITY_CRITICAL   Maps to STATE_YES.
  SEVERITY_WARNING    Maps to STATE_MAYBE.
  SEVERITY_NONE       Maps to STATE_NO.

Vendor-specific attribute handling
-----------------------------------
Seagate and some other vendors pack compound data into the raw 48-bit value
for certain ATA attributes.  The most common case is attribute 188
(Command_Timeout) where the full raw value can appear as hundreds of
billions when the actual timeout count is only in the lower bytes.

References:
  - Backblaze Hard Drive Stats methodology
  - smartmontools wiki on vendor-specific raw values
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# --- Attention states (the sensor's primary state value) ---
STATE_YES         = "YES"
STATE_MAYBE       = "MAYBE"
STATE_NO          = "NO"
STATE_UNSUPPORTED = "UNSUPPORTED"

# All valid states for HA enum device_class registration.
ATTENTION_STATES: list[str] = [STATE_NO, STATE_MAYBE, STATE_YES, STATE_UNSUPPORTED]

# --- Severity constants (used in attributes and notifications) ---
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING  = "warning"
SEVERITY_NONE     = "none"


# ---------------------------------------------------------------------------
# ATA attribute classification tables
# ---------------------------------------------------------------------------
# Key → human-readable label for the reasons list.
# Multiple name variants for the same logical attribute are deduplicated.

# CRITICAL: any non-zero value = data integrity at risk.
_CRITICAL_ATA: dict[str, str] = {
    "Reallocated_Sector_Ct":     "Reallocated Sector Count",
    # SK Hynix SATA SSDs name attribute 5 Retired_Block_Count. Same meaning,
    # same severity. Without this the most predictive attribute we track was
    # invisible on those drives. Found in a dump from @jackeichen (#27).
    "Retired_Block_Count":       "Reallocated Sector Count",
    "Current_Pending_Sector":    "Current Pending Sector Count",
    "Current_Pending_Sector_Ct": "Current Pending Sector Count",
    "Total_Pending_Sectors":     "Current Pending Sector Count",
    "Offline_Uncorrectable":     "Offline Uncorrectable Errors",
    "Reported_Uncorrect":        "Reported Uncorrectable Errors",
    "Uncorrectable_Error_Cnt":   "Uncorrectable Error Count",
    "Total_Offl_Uncorrectabl":   "Total Offline Uncorrectable",
}

# WARNING: non-zero = monitor and plan replacement.
_WARNING_ATA: dict[str, str] = {
    "Reallocated_Event_Count": "Reallocated Event Count",
    "Spin_Retry_Count":        "Spin Retry Count",
    "Command_Timeout":         "Command Timeout",
}

# Command_Timeout uses a threshold instead of zero-tolerance.  Low counts
# (1-100) are common on healthy drives from USB sleep/wake cycles, SATA
# power management (ALPM), and NCQ reordering.  Backblaze data shows 84%
# of drives accumulate non-zero Command_Timeout over their lifetime.
# Counts above this threshold suggest real controller or interconnect issues.
_COMMAND_TIMEOUT_WARN_THRESHOLD = 100

# WARNING: SSD wear leveling -- percentage of rated life consumed.
# ATA normalized VALUE is "life remaining" (100 = new, 0 = worn); we
# invert to "percentage used" and warn at >= 90%.  Matches the NVMe
# percentage_used threshold.
_ATA_WEAR_NAMES: set[str] = {
    "Wear_Leveling_Count",
    "Wear_Range_Delta",
    "Media_Wearout_Indicator",
    "SSD_Life_Left",
    "Remaining_Lifetime_Perc",
    "Percent_Lifetime_Remain",
    "Perc_Rated_Life_Remain",
    "Percent_Life_Remaining",
    "Drive_Life_Protection_Stat",
}
_ATA_WEAR_WARN_THRESHOLD = 90  # percentage used

# NVMe available_spare warning tier. The drive's own available_spare_threshold
# is a separate, non-configurable critical check; this is the earlier heads-up.
_NVME_SPARE_WARN_BELOW = 20  # percent remaining


# ---------------------------------------------------------------------------
# Configurable thresholds (#36)
# ---------------------------------------------------------------------------
# Thresholds are keyed by the human-readable LABEL, not the raw smartctl
# attribute name, because one logical attribute arrives under several vendor
# names: Reallocated_Sector_Ct and SK Hynix's Retired_Block_Count both map to
# "Reallocated Sector Count" (#27). Keying on the label means one stored value
# covers every alias and reads the same as the UI shows it.
#
# The ATA labels are derived from _CRITICAL_ATA and _WARNING_ATA at runtime, so
# there is no second copy to drift. Only the three labels that have no entry in
# those dicts are named here.
LABEL_SSD_WEAR = "SSD Wear Percent Used"
LABEL_NVME_MEDIA_ERRORS = "NVMe Media Errors"
LABEL_NVME_SPARE_WARN = "NVMe Spare Warn Below"

# Every label defaults to 0 (zero tolerance) unless listed here.
_THRESHOLD_DEFAULTS: dict[str, int] = {
    "Command Timeout":      _COMMAND_TIMEOUT_WARN_THRESHOLD,
    LABEL_SSD_WEAR:         _ATA_WEAR_WARN_THRESHOLD,
    LABEL_NVME_SPARE_WARN:  _NVME_SPARE_WARN_BELOW,
}

# Comparison direction per label. Anything absent alerts when the value is
# strictly above its threshold.
#
# NVMe available spare counts DOWN, so it is the one key that alerts when the
# value falls BELOW its threshold. Wear is inclusive because 90% used has
# always alerted at exactly 90.
_COMPARE_GT = "gt"
_COMPARE_GE = "ge"
_COMPARE_LT = "lt"

_THRESHOLD_COMPARE: dict[str, str] = {
    LABEL_SSD_WEAR:        _COMPARE_GE,
    LABEL_NVME_SPARE_WARN: _COMPARE_LT,
}

# Labels that no threshold may silence. A drive declaring its own failure is
# not a preference. Listed for the options flow, which must not offer them.
NEVER_CONFIGURABLE: frozenset[str] = frozenset({
    "SMART overall status",
    "NVMe critical warning",
    "NVMe available spare below drive threshold",
})


def threshold_labels() -> list[str]:
    """Every configurable label, derived from the attribute maps at runtime."""
    return sorted(_ata_labels() | _nvme_labels())


def _ata_labels() -> set[str]:
    return set(_CRITICAL_ATA.values()) | set(_WARNING_ATA.values()) | {LABEL_SSD_WEAR}


def _nvme_labels() -> set[str]:
    return {LABEL_NVME_MEDIA_ERRORS, LABEL_NVME_SPARE_WARN, LABEL_SSD_WEAR}


def labels_for_drive(drive_data: dict[str, Any]) -> list[str]:
    """Configurable labels that apply to this drive's protocol.

    An NVMe drive has no Spin Retry Count and an ATA drive has no available
    spare, so offering either in the form would invite a setting that can never
    do anything. Wear applies to both.
    """
    smart_data = coerce_smart_data(drive_data)
    if smart_data.get("nvme_smart_health_information_log"):
        return sorted(_nvme_labels())
    return sorted(_ata_labels())


def current_readings(drive_data: dict[str, Any]) -> dict[str, int]:
    """Current value per configurable label, for display and for snapshotting.

    Unlike evaluation this keeps zero readings, because the form should show a
    current value of 0 rather than omitting the row.
    """
    smart_data = coerce_smart_data(drive_data)
    readings: dict[str, int] = {}

    nvme_log = smart_data.get("nvme_smart_health_information_log") or {}
    if nvme_log:
        readings[LABEL_NVME_MEDIA_ERRORS] = int(nvme_log.get("media_errors", 0) or 0)
        spare = nvme_log.get("available_spare")
        if spare is not None:
            readings[LABEL_NVME_SPARE_WARN] = int(spare)
        readings[LABEL_SSD_WEAR] = int(nvme_log.get("percentage_used", 0) or 0)
        return readings

    ata_attrs = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
    seen: set[str] = set()
    for attr in ata_attrs:
        name = attr.get("name", "")
        raw = attr.get("raw", {})
        raw_value = raw.get("value", 0) if isinstance(raw, dict) else 0
        if not isinstance(raw_value, (int, float)):
            continue
        label = _CRITICAL_ATA.get(name) or _WARNING_ATA.get(name)
        if label and label not in seen:
            readings[label] = _decode_ata_raw(name, int(raw_value))
            seen.add(label)

    for attr in ata_attrs:
        if attr.get("name", "") in _ATA_WEAR_NAMES:
            normalized = attr.get("value")
            if normalized is not None:
                readings[LABEL_SSD_WEAR] = max(0, 100 - normalized)
            break

    return readings


# The two gauges measure how much life the drive has left and are meant to
# move, where the ten counters record damage that has already happened. The
# form lists them last and shows their values as percentages.
GAUGE_LABELS: frozenset[str] = frozenset({LABEL_SSD_WEAR, LABEL_NVME_SPARE_WARN})


def form_order(labels: list[str], readings: dict[str, int]) -> list[str]:
    """The order the threshold form lists a drive's labels in.

    Counters with a non-zero reading first, so damage is the first thing seen;
    then counters reading zero or not reported; then the two gauges. Each group
    keeps the order it was given in.
    """
    gauges = [label for label in labels if label in GAUGE_LABELS]
    counters = [label for label in labels if label not in GAUGE_LABELS]
    damaged = [label for label in counters if readings.get(label, 0) > 0]
    rest = [label for label in counters if label not in damaged]
    return damaged + rest + gauges


def reading_placeholders(
    readings: dict[str, int],
    drive_label: str,
) -> dict[str, str]:
    """Placeholders behind each field's "Currently: N. Default: M." line.

    Home Assistant sources data_description from translations, so a per-drive
    value cannot be passed directly and has to arrive as a placeholder. Each
    label gets two: {slug} for the reading and {slug_default} for the built-in
    default. Both carry their own unit, and an absent reading is
    "not reported".

    Every label gets both, including ones absent from this drive's protocol, so
    no translation is left with an unfilled slot.
    """
    placeholders = {"drive": drive_label}
    for label in threshold_labels():
        unit = "%" if label in GAUGE_LABELS else ""
        slug = threshold_slug(label)
        placeholders[slug] = (
            f"{readings[label]}{unit}" if label in readings else "not reported"
        )
        placeholders[f"{slug}_default"] = f"{default_threshold(label)}{unit}"
    return placeholders


def prefill_thresholds(
    drive_data: dict[str, Any],
    stored: dict[str, int],
) -> dict[str, int]:
    """Values to prefill the threshold form with: the stored override, else the
    default, for every label the drive can report.

    A stored value that cannot be read as a number falls back to the default
    rather than raising, since stored config survives downgrades and hand edits.
    """
    values: dict[str, int] = {}
    for label in labels_for_drive(drive_data):
        default = default_threshold(label)
        values[label] = (
            _coerce_threshold(stored.get(label), default)
            if label in stored
            else default
        )
    return values


def overrides_from_form(labels: list[str], user_input: dict[str, Any]) -> dict[str, int]:
    """What to store from a submitted threshold form: only genuine overrides.

    A value equal to the built-in default is dropped, which is how typing the
    default clears an override. Keeping it would also freeze today's default in
    place, so a future change to it would silently not reach this user. A field
    left empty or holding junk is dropped too, leaving that label at its default.
    """
    chosen: dict[str, int] = {}
    for label in labels:
        if label not in user_input:
            continue
        try:
            value = int(user_input[label])
        except (TypeError, ValueError):
            continue
        if value != default_threshold(label):
            chosen[label] = value
    return chosen


def default_threshold(label: str) -> int:
    """The built-in threshold for a label, used when the user sets none."""
    return _THRESHOLD_DEFAULTS.get(label, 0)


def threshold_slug(label: str) -> str:
    """Translation-placeholder key for a label.

    The options form shows each field's current reading through
    data_description, which Home Assistant sources from translations, so the
    per-drive value has to arrive as a placeholder keyed by this slug.
    """
    return label.lower().replace(" ", "_")


def get_thresholds(entry: Any, drive_id: str) -> dict[str, int]:
    """Threshold overrides for one drive.

    Reads ``entry.data``, NOT ``entry.options``. This integration's options flow
    merges into data and calls async_create_entry(data={}), so options is
    permanently empty; reading it would return {} forever and the feature would
    silently do nothing. There is a warning comment to the same effect in
    sensor.py, added after that trap caught issue #40.

    Module level rather than a method because three of the seven callers
    (__init__.py, coordinator.py, diagnostics.py) have no drive-scoped self to
    hang one off.
    """
    all_thresholds = entry.data.get("thresholds") or {}
    drive = all_thresholds.get(drive_id) or {}
    return drive if isinstance(drive, dict) else {}


def _coerce_threshold(raw: Any, default: int) -> int:
    """Best effort int from a stored threshold, falling back to the default.

    Stored config is user-writable and survives downgrades, so a string or a
    None must never raise here; it degrades to the built-in default instead.
    """
    if isinstance(raw, bool):  # bool is an int subclass; not a threshold
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _trips(value: float, threshold: int, compare: str) -> bool:
    if compare == _COMPARE_GE:
        return value >= threshold
    if compare == _COMPARE_LT:
        return value < threshold
    return value > threshold


def _breach_text(label: str, value: int, limit: int) -> str:
    """Reason text for a label that tripped its threshold.

    With no override in force the wording is exactly what it was before #36, so
    a drive with no thresholds set produces byte-identical output. Only when the
    user has moved the threshold does the text say so.
    """
    default = default_threshold(label)
    if limit != default:
        return f"{label}: {value} (accepted {limit})"
    if default:
        return f"{label}: {value} (threshold {default})"
    return f"{label}: {value} (expected 0)"


def _evaluate_label(
    label: str,
    value: float,
    thresholds: dict[str, int],
) -> tuple[bool, bool, int]:
    """Judge one reading against its threshold.

    Returns (breached, accepted, threshold_in_force).

    ``accepted`` is true only when the user set an override, the value does not
    trip it, and the value WOULD have tripped the built-in default. That is what
    an accepted baseline means: something that would otherwise be alerting. A
    value that was never going to alert is not "accepted", it is just fine, and
    emitting it would fill the list with noise on healthy drives.
    """
    default = default_threshold(label)
    compare = _THRESHOLD_COMPARE.get(label, _COMPARE_GT)

    overridden = label in thresholds
    threshold = (
        _coerce_threshold(thresholds.get(label), default) if overridden else default
    )

    breached = _trips(value, threshold, compare)
    accepted = overridden and not breached and _trips(value, default, compare)
    return breached, accepted, threshold

# ---------------------------------------------------------------------------
# Vendor-specific raw value decoding
# ---------------------------------------------------------------------------
# Several drive vendors — most notably Seagate, but also OEM/rebadged drives
# (e.g., OOS-prefixed models) — pack compound data into the 48-bit raw value
# for certain ATA attributes.  Command_Timeout (attribute 188) is the most
# common case: the full raw value can be hundreds of billions when the actual
# timeout count is only in the lower 16 bits.
#
# Example: a raw value of 940,612,190,430 (0x00DB00DB00DE) on a Seagate
# ST12000NM0558 breaks down as three 16-bit counters packed together.  The
# meaningful error count is in the lowest 16 bits: 0x00DE = 222.
#
# Detection: rather than relying solely on vendor identification (which fails
# for OEM/rebadged drives), we detect compound encoding by the value itself.
# No drive should have more than 65,535 actual command timeouts and still be
# responding to smartctl.  A raw value >0xFFFF for Command_Timeout is almost
# certainly compound-encoded.
#
# References:
#   - Backblaze Hard Drive Stats methodology (uses lower 16 bits)
#   - smartmontools wiki on vendor-specific raw values

# Seagate model prefixes and identifiers for vendor detection.
_SEAGATE_PREFIXES: tuple[str, ...] = ("st", "seagate")


def _is_seagate(model: str) -> bool:
    """Return True if the drive model string indicates a Seagate drive."""
    lower = model.lower()
    return any(lower.startswith(p) for p in _SEAGATE_PREFIXES) or "seagate" in lower


def _decode_command_timeout(raw_value: int) -> int:
    """Extract the actual timeout count from a Command_Timeout raw value.

    Seagate and OEM drives pack three 16-bit counters into the 48-bit raw
    value.  The lowest 16 bits hold the meaningful error count.  Values
    above 0xFFFF are always compound-encoded — a drive with 65K+ real
    timeouts would be unresponsive.
    """
    if raw_value > 0xFFFF:
        return raw_value & 0xFFFF
    return raw_value


def _decode_ata_raw(attr_name: str, raw_value: int) -> int:
    """Decode the actual error count from a raw ATA attribute value.

    For most attributes, the raw value IS the count.  For attributes with
    known compound encoding (e.g., Command_Timeout), we extract the real
    counter from the appropriate byte position.
    """
    if attr_name == "Command_Timeout":
        return _decode_command_timeout(raw_value)
    return raw_value


def _has_usable_smart_data(smart_data: dict[str, Any]) -> bool:
    """Return True if the SMART data dict contains anything we can evaluate.

    A drive is considered to have usable data if ANY of:
      - smart_status.passed is present (even if False)
      - ata_smart_attributes.table has at least one entry
      - nvme_smart_health_information_log has any keys
    """
    # Check SMART status
    status = smart_data.get("smart_status", {})
    if isinstance(status, dict) and "passed" in status:
        return True

    # Check ATA attributes
    ata_table = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
    if ata_table:
        return True

    # Check NVMe health log
    nvme_log = smart_data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        return True

    return False


def coerce_smart_data(drive_data: dict[str, Any]) -> dict[str, Any]:
    """Return the drive's smart_data as a dict, or {} if unusable.

    The agent sends smart_data as raw JSON. It can arrive as a dict, as a
    JSON string, or as null when the agent could not read the drive (fixed
    agent-side in v0.6.2, but older agents are still in the field). Anything
    that is not a usable dict becomes {}, which every caller already handles
    as "no data".

    This replaces four hand-maintained copies of the same coercion, each of
    which dereferenced the result without a type check and raised
    AttributeError on a null payload.
    """
    raw = drive_data.get("smart_data")

    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    # Only the unusable branch logs. Debug level, so it is off unless someone
    # has turned on debug for this integration, at which point per-poll detail
    # is the point. The type name is the diagnostic: NoneType means an old
    # agent or a drive it could not read, str means JSON that would not parse,
    # anything else means something unexpected upstream.
    _LOGGER.debug(
        "Drive %s: unusable smart_data (%s), treating as no data",
        drive_data.get("id", "unknown"),
        type(raw).__name__,
    )
    return {}


def evaluate_attention(
    drive_data: dict[str, Any],
    thresholds: dict[str, int] | None = None,
) -> tuple[str, str, list[str], list[str]]:
    """Evaluate early-warning SMART indicators for a single drive.

    Args:
        drive_data: Full drive payload from the coordinator (as returned by
                    the agent's /api/drives/{id} endpoint).
        thresholds: Per-label overrides for this drive, from
                    get_thresholds(entry, drive_id). None or {} means every
                    label uses its built-in default, which behaves exactly as
                    the integration did before #36.

    Returns:
        (state, severity, reasons, accepted)

        - state:    one of STATE_YES, STATE_MAYBE, STATE_NO, STATE_UNSUPPORTED
        - severity: one of SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_NONE
        - reasons:  human-readable list of what triggered the alert. Actionable
                    items only. Empty when state is STATE_NO or UNSUPPORTED.
        - accepted: readings the user has accepted as a known baseline. Kept
                    SEPARATE from reasons on purpose: coordinator.py compares
                    sorted(reasons) to decide whether anything changed, so an
                    accepted value drifting 147 -> 148 under a threshold of 150
                    would otherwise look like a change, dismiss a notification
                    that does not exist, and log a line on every poll. That is
                    the recurring log noise v0.6.0 removed.
    """
    thresholds = thresholds or {}
    smart_data = coerce_smart_data(drive_data)

    # --- Data-quality gate ---
    if not _has_usable_smart_data(smart_data):
        return STATE_UNSUPPORTED, SEVERITY_NONE, [], []

    critical_reasons: list[str] = []
    warning_reasons:  list[str] = []
    accepted:         list[str] = []

    # ------------------------------------------------------------------
    # SMART overall status (applies to all protocols)
    # ------------------------------------------------------------------
    status = smart_data.get("smart_status", {})
    if isinstance(status, dict) and status.get("passed") is False:
        critical_reasons.append("SMART overall status: FAILED")

    # ------------------------------------------------------------------
    # NVMe evaluation
    # ------------------------------------------------------------------
    nvme_log = smart_data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        # CRITICAL — critical_warning bitmask
        cw = nvme_log.get("critical_warning", 0) or 0
        if cw != 0:
            critical_reasons.append(
                f"NVMe critical warning flag set (0x{cw:02x})"
            )

        # CRITICAL — unrecoverable media errors
        media_errors = nvme_log.get("media_errors", 0) or 0
        breached, was_accepted, limit = _evaluate_label(
            LABEL_NVME_MEDIA_ERRORS, media_errors, thresholds
        )
        if breached:
            critical_reasons.append(
                f"NVMe media errors: {media_errors} (accepted {limit})"
                if limit != default_threshold(LABEL_NVME_MEDIA_ERRORS)
                else f"NVMe media errors: {media_errors} (expected 0)"
            )
        elif was_accepted:
            accepted.append(
                f"NVMe media errors: {media_errors} (accepted {limit})"
            )

        # CRITICAL: spare below the drive's OWN threshold. Never configurable:
        # this is the device declaring it has reached its manufacturer limit,
        # the same category as SMART FAILED, and no user setting may silence it.
        spare     = nvme_log.get("available_spare")
        threshold = nvme_log.get("available_spare_threshold")
        if spare is not None and threshold is not None:
            if spare <= threshold:
                critical_reasons.append(
                    f"NVMe available spare ({spare}%) at or below "
                    f"drive threshold ({threshold}%)"
                )
            else:
                # WARNING: early heads-up before the drive's own limit. This
                # tier IS configurable. Note the direction: spare counts DOWN,
                # so it alerts BELOW the threshold, unlike every other label.
                breached, was_accepted, limit = _evaluate_label(
                    LABEL_NVME_SPARE_WARN, spare, thresholds
                )
                if breached:
                    warning_reasons.append(
                        f"NVMe available spare low: {spare}% remaining"
                        if limit == default_threshold(LABEL_NVME_SPARE_WARN)
                        else f"NVMe available spare low: {spare}% remaining "
                             f"(accepted down to {limit}%)"
                    )
                elif was_accepted:
                    accepted.append(
                        f"NVMe available spare: {spare}% "
                        f"(accepted down to {limit}%)"
                    )

        # WARNING: approaching end of rated write endurance. Shares the
        # SSD Wear Percent Used label with the ATA wear path, which is the
        # inconsistency called out in the plan: this used to hardcode 90 while
        # the ATA side used the constant.
        pct_used = nvme_log.get("percentage_used", 0) or 0
        breached, was_accepted, limit = _evaluate_label(
            LABEL_SSD_WEAR, pct_used, thresholds
        )
        if breached:
            warning_reasons.append(
                f"NVMe drive wear at {pct_used}% of rated life — "
                "consider scheduling replacement"
                if limit == default_threshold(LABEL_SSD_WEAR)
                else f"NVMe drive wear at {pct_used}% of rated life "
                     f"(accepted {limit}%)"
            )
        elif was_accepted:
            accepted.append(
                f"NVMe drive wear at {pct_used}% of rated life "
                f"(accepted {limit}%)"
            )

        return _assemble(critical_reasons, warning_reasons, accepted)

    # ------------------------------------------------------------------
    # ATA evaluation
    # ------------------------------------------------------------------
    ata_attrs = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
    seen_labels: set[str] = set()

    for attr in ata_attrs:
        name = attr.get("name", "")
        raw  = attr.get("raw", {})
        raw_value = raw.get("value", 0) if isinstance(raw, dict) else 0

        if not isinstance(raw_value, (int, float)) or raw_value <= 0:
            continue

        # Decode compound-encoded attributes (e.g., Seagate Command_Timeout).
        decoded = _decode_ata_raw(name, int(raw_value))

        label = _CRITICAL_ATA.get(name)
        if label and label not in seen_labels:
            breached, was_accepted, limit = _evaluate_label(
                label, decoded, thresholds
            )
            if breached:
                critical_reasons.append(_breach_text(label, decoded, limit))
                seen_labels.add(label)
            elif was_accepted:
                accepted.append(f"{label}: {decoded} (accepted {limit})")
                # Marking the label here too is what stops a second vendor name
                # for the same attribute emitting a duplicate accepted entry.
                # Before #36 the dedup only had to cover reasons.
                seen_labels.add(label)
            continue

        label = _WARNING_ATA.get(name)
        if label and label not in seen_labels:
            breached, was_accepted, limit = _evaluate_label(
                label, decoded, thresholds
            )
            if breached:
                warning_reasons.append(_breach_text(label, decoded, limit))
                seen_labels.add(label)
            elif was_accepted:
                accepted.append(f"{label}: {decoded} (accepted {limit})")
                seen_labels.add(label)

    # WARNING -- ATA SSD wear leveling.
    # Normalized VALUE is "life remaining"; invert to "percentage used".
    if "SSD wear" not in seen_labels:
        for attr in ata_attrs:
            if attr.get("name", "") in _ATA_WEAR_NAMES:
                normalized = attr.get("value")
                if normalized is not None:
                    pct_used = max(0, 100 - normalized)
                    breached, was_accepted, limit = _evaluate_label(
                        LABEL_SSD_WEAR, pct_used, thresholds
                    )
                    if breached:
                        warning_reasons.append(
                            f"SSD wear at {pct_used}% of rated life -- "
                            "consider scheduling replacement"
                            if limit == default_threshold(LABEL_SSD_WEAR)
                            else f"SSD wear at {pct_used}% of rated life "
                                 f"(accepted {limit}%)"
                        )
                        seen_labels.add("SSD wear")
                    elif was_accepted:
                        accepted.append(
                            f"SSD wear at {pct_used}% of rated life "
                            f"(accepted {limit}%)"
                        )
                        # The wear path keys the dedup on a literal sentinel
                        # rather than the label, so mark it here too.
                        seen_labels.add("SSD wear")
                break  # one wear attribute per drive

    return _assemble(critical_reasons, warning_reasons, accepted)


def _assemble(
    critical: list[str],
    warning: list[str],
    accepted: list[str],
) -> tuple[str, str, list[str], list[str]]:
    """Combine the reason lists into a final result tuple.

    `accepted` rides along on all three paths, including STATE_NO. That path is
    the one that matters most: a drive whose only findings are accepted reads NO
    and must still be able to show what was accepted.
    """
    if critical:
        return STATE_YES, SEVERITY_CRITICAL, critical + warning, accepted
    if warning:
        return STATE_MAYBE, SEVERITY_WARNING, warning, accepted
    return STATE_NO, SEVERITY_NONE, [], accepted


# ---------------------------------------------------------------------------
# Attention Reasons entity state
# ---------------------------------------------------------------------------
# Home Assistant truncates any entity state longer than 255 characters and logs
# an error when it does. Reasons text could already approach that on a bad drive
# before #36; appending accepted entries makes overflow likelier, so the cap is
# applied here rather than left to chance.
#
# This lives in attention.py, not sensor.py, so the tier-1 suite can test it.
# sensor.py imports Home Assistant, which means anything composed there is
# unreachable from plain pytest and would ship on inspection alone.

MAX_STATE_LENGTH = 255
_ELLIPSIS = "..."


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_ELLIPSIS))] + _ELLIPSIS


def compose_reasons_text(
    state: str,
    reasons: list[str],
    accepted: list[str],
    limit: int = MAX_STATE_LENGTH,
) -> str:
    """Build the Attention Reasons entity state from a result tuple.

    Accepted entries are appended in a parenthetical so a drive whose only
    findings are accepted still shows them, which is the whole point of the
    feature: before #36 a STATE_NO drive discarded its reasons entirely.

    When the composed string overflows, the accepted section is trimmed first.
    Actionable reasons are what the user has to act on; the full untruncated
    detail stays available in the entity attributes either way.
    """
    if state == STATE_UNSUPPORTED:
        return "No usable SMART data"

    head = "No issues detected" if state == STATE_NO else "; ".join(reasons)

    if not accepted:
        return _truncate(head, limit)

    opener, closer = " (accepted: ", ")"
    body = "; ".join(accepted)
    full = f"{head}{opener}{body}{closer}"
    if len(full) <= limit:
        return full

    room = limit - len(head) - len(opener) - len(_ELLIPSIS) - len(closer)
    if room > 0:
        return f"{head}{opener}{body[:room]}{_ELLIPSIS}{closer}"

    # Even the opener does not fit, so the reasons alone are over budget.
    return _truncate(head, limit)
