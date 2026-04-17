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
    ata_table = smart_data.get("ata_smart_attributes", {}).get("table", [])
    if ata_table:
        return True

    # Check NVMe health log
    nvme_log = smart_data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        return True

    return False


def evaluate_attention(
    drive_data: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """Evaluate early-warning SMART indicators for a single drive.

    Args:
        drive_data: Full drive payload from the coordinator (as returned by
                    the agent's /api/drives/{id} endpoint).

    Returns:
        (state, severity, reasons)

        - state:    one of STATE_YES, STATE_MAYBE, STATE_NO, STATE_UNSUPPORTED
        - severity: one of SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_NONE
        - reasons:  human-readable list of what triggered the alert.
                    Empty list when state is STATE_NO or STATE_UNSUPPORTED.
    """
    smart_data = drive_data.get("smart_data", {})

    if isinstance(smart_data, str):
        import json
        try:
            smart_data = json.loads(smart_data)
        except (json.JSONDecodeError, TypeError):
            return STATE_UNSUPPORTED, SEVERITY_NONE, []

    # --- Data-quality gate ---
    if not _has_usable_smart_data(smart_data):
        return STATE_UNSUPPORTED, SEVERITY_NONE, []

    critical_reasons: list[str] = []
    warning_reasons:  list[str] = []

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
        if media_errors > 0:
            critical_reasons.append(
                f"NVMe media errors: {media_errors} (expected 0)"
            )

        # CRITICAL — spare below drive's own threshold
        spare     = nvme_log.get("available_spare")
        threshold = nvme_log.get("available_spare_threshold")
        if spare is not None and threshold is not None:
            if spare <= threshold:
                critical_reasons.append(
                    f"NVMe available spare ({spare}%) at or below "
                    f"drive threshold ({threshold}%)"
                )
            elif spare < 20:
                # WARNING — early heads-up before hitting official threshold
                warning_reasons.append(
                    f"NVMe available spare low: {spare}% remaining"
                )

        # WARNING — approaching end of rated write endurance
        pct_used = nvme_log.get("percentage_used", 0) or 0
        if pct_used >= 90:
            warning_reasons.append(
                f"NVMe drive wear at {pct_used}% of rated life — "
                "consider scheduling replacement"
            )

        return _assemble(critical_reasons, warning_reasons)

    # ------------------------------------------------------------------
    # ATA evaluation
    # ------------------------------------------------------------------
    ata_attrs = smart_data.get("ata_smart_attributes", {}).get("table", [])
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
            if decoded > 0:
                critical_reasons.append(f"{label}: {decoded} (expected 0)")
                seen_labels.add(label)
            continue

        label = _WARNING_ATA.get(name)
        if label and label not in seen_labels:
            if name == "Command_Timeout":
                if decoded > _COMMAND_TIMEOUT_WARN_THRESHOLD:
                    warning_reasons.append(
                        f"{label}: {decoded} (threshold {_COMMAND_TIMEOUT_WARN_THRESHOLD})"
                    )
                    seen_labels.add(label)
            elif decoded > 0:
                warning_reasons.append(f"{label}: {decoded} (expected 0)")
                seen_labels.add(label)

    return _assemble(critical_reasons, warning_reasons)


def _assemble(
    critical: list[str],
    warning: list[str],
) -> tuple[str, str, list[str]]:
    """Combine critical and warning reason lists into a final result tuple."""
    if critical:
        return STATE_YES, SEVERITY_CRITICAL, critical + warning
    if warning:
        return STATE_MAYBE, SEVERITY_WARNING, warning
    return STATE_NO, SEVERITY_NONE, []
