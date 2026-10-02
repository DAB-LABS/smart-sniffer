"""Reading values out of a drive payload, for the sensors and the entity plan.

Moved out of sensor.py unchanged in v0.8.0 so the entity plan (entity_plan.py)
and the tests can use the same lookups without Home Assistant. sensor.py
re-imports every name here under the name it always had.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

import re
from typing import Any

from .attention import coerce_smart_data, is_dead_wear_attr

# ---------------------------------------------------------------------------
# Drive-type gate sets
# ---------------------------------------------------------------------------
ATA_ONLY_KEYS: frozenset[str] = frozenset({
    "reallocated_sector_count",
    "current_pending_sector_count",
    "reallocated_event_count",
    "spin_retry_count",
    "command_timeout",
})

NVME_ONLY_KEYS: frozenset[str] = frozenset({
    "critical_warning",
    "media_errors",
    "available_spare",
    "available_spare_threshold",
})

SKIP_IF_NOT_PRESENT: frozenset[str] = frozenset({
    "current_pending_sector_count",
    "spin_retry_count",
    "command_timeout",
    "wear_leveling_count",
    "available_spare",
    "available_spare_threshold",
    "power_cycle_count",
    "reallocated_event_count",
})


# The curated sensors, in the order setup has always created them. The
# descriptions themselves (units, classes, icons) stay in sensor.py.
SENSOR_KEYS: tuple[str, ...] = (
    "temperature",
    "power_on_hours",
    "power_cycle_count",
    "reported_uncorrectable_errors",
    "wear_leveling_count",
    "smart_status",
    "reallocated_sector_count",
    "current_pending_sector_count",
    "reallocated_event_count",
    "spin_retry_count",
    "command_timeout",
    "critical_warning",
    "media_errors",
    "available_spare",
    "available_spare_threshold",
)


# ---------------------------------------------------------------------------
# ATA attribute name map (module-level for reuse in entity registration)
# ---------------------------------------------------------------------------
# Maps our internal sensor keys to the smartctl attribute names that
# correspond to each key.  Used by _extract_attribute() for value lookup
# and by async_setup_entry() to identify which attributes already have
# dedicated sensors (so diagnostic entities skip them).

ATA_NAME_MAP: dict[str, list[str]] = {
    "temperature": [
            "Temperature_Celsius",
            "Temperature_Internal",
            "Airflow_Temperature_Cel",
            "HDA_Temperature",
            "Drive_Temperature",
        ],
        "power_on_hours": [
            "Power_On_Hours",
            "Power_On_Hours_and_Msec",
            "Power_On_Time",
        ],
        "power_cycle_count": [
            "Power_Cycle_Count",
            "Power_Cycles",
        ],
        "reallocated_sector_count": [
            "Reallocated_Sector_Ct",
            # SK Hynix SATA SSD name for attribute 5 (#27).
            "Retired_Block_Count",
        ],
        "current_pending_sector_count": [
            "Current_Pending_Sector",
            "Current_Pending_Sector_Ct",
            "Total_Pending_Sectors",
        ],
        "reallocated_event_count": [
            "Reallocated_Event_Count",
        ],
        "spin_retry_count": [
            "Spin_Retry_Count",
        ],
        "command_timeout": [
            "Command_Timeout",
        ],
        "reported_uncorrectable_errors": [
            "Offline_Uncorrectable",
            "Reported_Uncorrect",
            "Uncorrectable_Error_Cnt",
            "Total_Offl_Uncorrectabl",
        ],
        "wear_leveling_count": [
            "Wear_Leveling_Count",
            # Not Wear_Range_Delta (177 on SandForce and some Seagate SSDs):
            # it is the spread between the most and least worn blocks, not
            # life remaining, and a normal reading of 0 turned into "100%
            # used" (GH #55).  Keep in step with _ATA_WEAR_NAMES in
            # attention.py.
            "Media_Wearout_Indicator",
            "SSD_Life_Left",
            "Remaining_Lifetime_Perc",
            "Percent_Lifetime_Remain",
            "Perc_Rated_Life_Remain",
            "Percent_Life_Remaining",
        ],
    }


# ---------------------------------------------------------------------------
# SMART attribute extraction
# ---------------------------------------------------------------------------

# Diagnostic attributes we understand well enough to assign a state class.
# Monotonic counters get TOTAL_INCREASING; gauges that move both ways get
# MEASUREMENT.  Anything not listed gets no state class at all, which is the
# deliberate default: no statistics is better than wrong statistics for a
# vendor-specific attribute whose semantics we do not know.  See issue #47.
_DIAG_COUNTER_ATTRS = frozenset(
    {
        "Start_Stop_Count",
        "Load_Cycle_Count",
        "Offline_Uncorrectable",
        "UDMA_CRC_Error_Count",
        # Samsung (and several other vendors) name attribute 199
        # CRC_Error_Count rather than UDMA_CRC_Error_Count.  Without this
        # variant the fix misses the exact attribute class reported in #47
        # on Samsung SSDs.  Confirmed against a Samsung 870 EVO.
        "CRC_Error_Count",
        # Samsung vendor counters, all monotonic.
        "POR_Recovery_Count",
        "Runtime_Bad_Block",
        "Used_Rsvd_Blk_Cnt_Tot",
        # SK Hynix spells the same counter with an extra r (#27).
        "Used_Rsrvd_Blk_Cnt_Tot",
        "Power_Cycle_Count",
        "Power-Off_Retract_Count",
        "Reallocated_Sector_Ct",
        "Reallocated_Event_Count",
        "Current_Pending_Sector",
        "Reported_Uncorrect",
        "Command_Timeout",
        "Spin_Retry_Count",
        "G-Sense_Error_Rate",
        "Erase_Fail_Count",
        "Erase_Fail_Count_Total",
        "Program_Fail_Count",
        "Program_Fail_Cnt_Total",
        "Total_LBAs_Written",
        "Total_LBAs_Read",
        "Host_Writes_32MiB",
        "Host_Reads_32MiB",
        "Head_Flying_Hours",
        "Power_On_Hours",
    }
)

_DIAG_GAUGE_ATTRS = frozenset(
    {
        "Temperature_Celsius",
        "Airflow_Temperature_Cel",
        "Available_Reservd_Space",
        "Media_Wearout_Indicator",
        "Percent_Lifetime_Remain",
        "Remaining_Lifetime_Perc",
        "SSD_Life_Left",
        "Wear_Leveling_Count",
        # Remaining spare blocks: counts DOWN as blocks are consumed, so it
        # is a gauge, unlike its Used_ counterpart above.
        "Unused_Rsvd_Blk_Cnt_Tot",
    }
)


def diagnostic_state_class_name(attr_name: str) -> str | None:
    """The state class a dynamic diagnostic attribute gets, by its value
    (``total_increasing`` or ``measurement``), or None. sensor.py maps it to
    SensorStateClass; see _diagnostic_state_class there."""
    if attr_name in _DIAG_COUNTER_ATTRS:
        return "total_increasing"
    if attr_name in _DIAG_GAUGE_ATTRS:
        return "measurement"
    return None


def _decode_raw_value(raw: Any) -> Any | None:
    """Decode a SMART attribute raw value, unpacking vendor-compound values.

    Several drive families pack multiple sub-counters into the single 48-bit
    raw value, so `raw.value` comes back as a huge integer while `raw.string`
    holds the decoded figure:

        Temperature_Celsius     value 244813987870   string "30 (Min/Max 13/57)"
        Media_Wearout_Indicator value 1284200464683  string "299 80 299"

    smartctl's `raw.string` is the vendor-decoded human form and its leading
    integer is the real value.  We prefer it only when the numeric value looks
    packed (above 0xFFFF) and the string actually disagrees, so legitimately
    large counters such as Total_LBAs_Written are left untouched.

    See issue #44, and the per-attribute fixes this generalises: #10
    (Power_On_Hours), Command_Timeout in v0.4.26, Wear_Leveling in v0.4.30.
    """
    if not isinstance(raw, dict):
        return raw

    raw_value = raw.get("value")
    if not isinstance(raw_value, int):
        return raw_value

    if raw_value > 0xFFFF:
        raw_string = raw.get("string")
        if raw_string:
            m = re.match(r"\s*(\d+)", str(raw_string))
            if m:
                decoded = int(m.group(1))
                if decoded != raw_value:
                    return decoded

    return raw_value


def _extract_attribute(drive_data: dict[str, Any], key: str) -> Any | None:
    """Extract a SMART attribute value from the drive's full JSON payload.

    Handles ATA-style attribute tables, NVMe health info logs, and
    provides a universal top-level fallback for SCSI/SAS drives.
    Returns None if the attribute is not present.
    """
    smart_data = coerce_smart_data(drive_data)

    # --- SMART overall status ---
    if key == "smart_status":
        # No default here. An unusable payload coerces to {}, and reporting
        # "FAILED" for a drive that reported nothing at all would be a false
        # alarm; the inline coercion this replaced returned None in that case.
        status = smart_data.get("smart_status")
        if isinstance(status, dict):
            return "PASSED" if status.get("passed", False) else "FAILED"
        return None

    # --- NVMe path ---
    nvme_log = smart_data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        nvme_map = {
            "temperature":                  lambda: nvme_log.get("temperature"),
            "power_on_hours":               lambda: nvme_log.get("power_on_hours"),
            "power_cycle_count":            lambda: nvme_log.get("power_cycles"),
            "wear_leveling_count":          lambda: nvme_log.get("percentage_used"),
            "reported_uncorrectable_errors":lambda: nvme_log.get("media_errors"),
            "critical_warning":             lambda: nvme_log.get("critical_warning"),
            "media_errors":                 lambda: nvme_log.get("media_errors"),
            "available_spare":              lambda: nvme_log.get("available_spare"),
            "available_spare_threshold":    lambda: nvme_log.get("available_spare_threshold"),
        }
        extractor = nvme_map.get(key)
        if extractor:
            return extractor()

    # --- ATA path ---
    ata_attrs = (smart_data.get("ata_smart_attributes") or {}).get("table", [])

    names = ATA_NAME_MAP.get(key, [])
    for attr in ata_attrs:
        if attr.get("name") in names:
            # A wear-named row reading 0/0/0 with flags 0 is not a gauge
            # (GH #55); skip it and keep looking.  With nothing usable left
            # the drive has no wear reading.
            if key == "wear_leveling_count" and is_dead_wear_attr(attr):
                continue
            raw = attr.get("raw", {})
            if isinstance(raw, dict):
                raw_value = raw.get("value")
                # WD/HGST drives pack min/max/current into a single 48-bit
                # raw value for Temperature_Celsius (e.g., 214749675563
                # instead of 43).  The actual temp is in the low 16 bits.
                # Parse raw.string first (e.g., "43 (Min/Max 20/50)"),
                # fall back to masking if needed.
                if key == "temperature" and isinstance(raw_value, int) and raw_value > 300:
                    raw_string = raw.get("string", "")
                    if raw_string:
                        import re
                        m = re.match(r"(\d+)", str(raw_string))
                        if m:
                            return int(m.group(1))
                    # Fallback: low 16 bits hold current temp.
                    return raw_value & 0xFFFF

                # Command_Timeout (attribute 188): some vendors — notably
                # Seagate and OEM drives — pack compound data into the
                # 48-bit raw value.  The actual timeout count is in the
                # lower 16 bits.  Values above 0xFFFF are always compound.
                if (
                    key == "command_timeout"
                    and isinstance(raw_value, int)
                    and raw_value > 0xFFFF
                ):
                    return raw_value & 0xFFFF

                # Power_On_Hours (attribute 9): some vendors pack
                # additional counters (days, minutes, milliseconds)
                # into the upper bytes of the 48-bit raw value.
                # The actual hours are in the lower 32 bits.
                # Parse raw.string first (e.g., "73593 (159 43 0)"),
                # fall back to masking if needed.
                # See: https://github.com/DAB-LABS/smart-sniffer/issues/10
                if (
                    key == "power_on_hours"
                    and isinstance(raw_value, int)
                    and raw_value > 1_000_000
                ):
                    raw_string = raw.get("string", "")
                    if raw_string:
                        import re
                        m = re.match(r"(\d+)", str(raw_string))
                        if m:
                            return int(m.group(1))
                    return raw_value & 0xFFFFFFFF

                # Wear-leveling attributes: the normalized VALUE column
                # (0-100) represents percentage of life REMAINING for
                # ATA drives (100 = new, 0 = worn).  We invert to
                # "percentage used" (0 = new, 100 = worn) for
                # consistency with NVMe percentage_used semantics.
                # RAW_VALUE is a vendor-specific counter (total writes,
                # erase cycles, etc.) and should not be used directly.
                # See: https://github.com/DAB-LABS/smart-sniffer/issues/7
                # See: docs/internal/research/smart-wear-leveling-semantics.md
                if key == "wear_leveling_count":
                    normalized = attr.get("value")
                    if normalized is None:
                        return None
                    return max(0, 100 - normalized)

                # Everything else: unpack vendor-compound raw values rather
                # than handing Home Assistant a packed 48-bit integer.  See
                # issue #44.
                return _decode_raw_value(raw)
            return raw

    # --- Universal fallback (top-level fields, all protocols) -----------
    # smartctl places temperature, power_on_time, and power_cycle_count at
    # the JSON top level for ATA, NVMe, and SCSI alike.  This catches
    # SAS/SCSI drives that have no protocol-specific path above, and acts
    # as a safety net for malformed ATA/NVMe payloads.
    _top_level_map = {
        "temperature":      lambda: smart_data.get("temperature", {}).get("current"),
        "power_on_hours":   lambda: smart_data.get("power_on_time", {}).get("hours"),
        "power_cycle_count": lambda: smart_data.get("power_cycle_count"),
    }
    _fallback = _top_level_map.get(key)
    if _fallback:
        _val = _fallback()
        if _val is not None:
            return _val

    return None
