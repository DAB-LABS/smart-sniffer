"""Sensor entities for SMART Sniffer.

Creates two kinds of sensors per drive:

1. SMART attribute sensors — individual readings (temperature, power-on hours,
   reallocated sectors, etc.) extracted from the agent's JSON payload.

2. Attention Needed sensor — an enum sensor with states NO / MAYBE / YES /
   UNSUPPORTED that aggregates early-warning indicators from attention.py.
   This is the primary "should I care about this drive" signal.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .attention import (
    ATTENTION_STATES,
    SEVERITY_NONE,
    STATE_MAYBE,
    STATE_NO,
    STATE_UNSUPPORTED,
    STATE_YES,
    evaluate_attention,
)
from .const import DOMAIN
from .coordinator import SmartSnifferCoordinator

_LOGGER = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# SMART attribute sensor descriptions
# ---------------------------------------------------------------------------
SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    # --- Universal (all protocols) ---
    SensorEntityDescription(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer",
    ),
    SensorEntityDescription(
        key="power_on_hours",
        name="Power-On Hours",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:clock-outline",
    ),
    SensorEntityDescription(
        key="power_cycle_count",
        name="Power Cycle Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:power-cycle",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="reported_uncorrectable_errors",
        name="Reported Uncorrectable Errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-decagram-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="wear_leveling_count",
        name="Wear Leveling / Percentage Used",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:chart-donut",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="smart_status",
        name="SMART Status",
        icon="mdi:harddisk",
    ),

    # --- ATA / SATA only ---
    SensorEntityDescription(
        key="reallocated_sector_count",
        name="Reallocated Sector Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="current_pending_sector_count",
        name="Current Pending Sector Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="reallocated_event_count",
        name="Reallocated Event Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="spin_retry_count",
        name="Spin Retry Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:rotate-right",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="command_timeout",
        name="Command Timeout",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:timer-off-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),

    # --- NVMe only ---
    SensorEntityDescription(
        key="critical_warning",
        name="Critical Warning",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-decagram",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="media_errors",
        name="Media Errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-decagram-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="available_spare",
        name="Available Spare",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="available_spare_threshold",
        name="Available Spare Threshold",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk-remove",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


# ---------------------------------------------------------------------------
# SMART attribute extraction
# ---------------------------------------------------------------------------

def _extract_attribute(drive_data: dict[str, Any], key: str) -> Any | None:
    """Extract a SMART attribute value from the drive's full JSON payload.

    Handles both ATA-style attribute tables and NVMe health info logs.
    Returns None if the attribute is not present.
    """
    smart_data = drive_data.get("smart_data", {})

    if isinstance(smart_data, str):
        import json
        try:
            smart_data = json.loads(smart_data)
        except (json.JSONDecodeError, TypeError):
            return None

    # --- SMART overall status ---
    if key == "smart_status":
        status = smart_data.get("smart_status", {})
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
    ata_attrs = smart_data.get("ata_smart_attributes", {}).get("table", [])

    ata_name_map: dict[str, list[str]] = {
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
            "Wear_Range_Delta",
            "Media_Wearout_Indicator",
            "SSD_Life_Left",
            "Remaining_Lifetime_Perc",
            "Percent_Lifetime_Remain",
            "Perc_Rated_Life_Remain",
            "Percent_Life_Remaining",
            "Drive_Life_Protection_Stat",
        ],
    }

    names = ata_name_map.get(key, [])
    for attr in ata_attrs:
        if attr.get("name") in names:
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
                # (0–100) is the correct percentage.  RAW_VALUE is a
                # vendor-specific counter (total writes, erase cycles,
                # etc.) that can be in the thousands.
                # See: https://github.com/DAB-LABS/smart-sniffer/issues/7
                if key == "wear_leveling_count":
                    return attr.get("value")

                return raw_value
            return raw

    return None


# ---------------------------------------------------------------------------
# Entity setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SMART Sniffer sensor entities from a config entry."""
    coordinator: SmartSnifferCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for drive_id, drive_data in coordinator.data.items():
        protocol = drive_data.get("protocol", "").upper()
        smart_data = drive_data.get("smart_data", {})
        has_ata_attrs = bool(smart_data.get("ata_smart_attributes"))
        is_nvme = protocol == "NVME" and not has_ata_attrs
        is_ata = protocol in ("ATA", "SATA", "") or has_ata_attrs

        # --- SMART attribute sensors ---
        for description in SENSOR_DESCRIPTIONS:
            key = description.key

            if key in ATA_ONLY_KEYS and not is_ata:
                _LOGGER.debug(
                    "Skipping ATA-only sensor '%s' for %s drive %s",
                    key, protocol, drive_id,
                )
                continue

            if key in NVME_ONLY_KEYS and not is_nvme:
                _LOGGER.debug(
                    "Skipping NVMe-only sensor '%s' for ATA/SATA drive %s",
                    key, drive_id,
                )
                continue

            if key in SKIP_IF_NOT_PRESENT:
                initial_value = _extract_attribute(drive_data, key)
                if initial_value is None:
                    _LOGGER.debug(
                        "Skipping sensor '%s' for drive %s — not in SMART data",
                        key, drive_id,
                    )
                    continue

            entities.append(
                SmartSnifferSensor(coordinator, drive_id, drive_data, description)
            )

        # --- Attention Needed sensor (one per drive, always created) ---
        entities.append(
            SmartSnifferAttentionSensor(coordinator, drive_id, drive_data)
        )

        # --- Attention Reasons sensor (one per drive, always created) ---
        entities.append(
            SmartSnifferAttentionReasonsSensor(coordinator, drive_id, drive_data)
        )

    async_add_entities(entities, update_before_add=False)


# ---------------------------------------------------------------------------
# SMART attribute sensor
# ---------------------------------------------------------------------------

# Sensor keys whose non-zero values trigger critical attention (YES).
_CRITICAL_SENSOR_KEYS: frozenset[str] = frozenset({
    "reallocated_sector_count",
    "current_pending_sector_count",
    "reported_uncorrectable_errors",
    "critical_warning",
    "media_errors",
})

# Sensor keys whose non-zero values trigger warning attention (MAYBE).
_WARNING_SENSOR_KEYS: frozenset[str] = frozenset({
    "reallocated_event_count",
    "spin_retry_count",
    "command_timeout",
})

# NVMe sensors with threshold-based triggers (not simple non-zero).
# These are handled specially in the icon property.
_NVME_SPARE_KEY = "available_spare"
_NVME_WEAR_KEY = "wear_leveling_count"

# Alert icons — used when a diagnostic sensor is actively triggering attention.
_ALERT_ICON_CRITICAL = "mdi:alert-octagon"
_ALERT_ICON_WARNING  = "mdi:alert-circle"


class SmartSnifferSensor(CoordinatorEntity[SmartSnifferCoordinator], SensorEntity):
    """Representation of a single SMART attribute as a HA sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._drive_id = drive_id
        self._default_icon = description.icon

        model = drive_data.get("model", "Unknown Drive")
        serial = drive_data.get("serial", drive_id)

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{drive_id}_{description.key}"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, drive_id)},
            "name": f"{model} ({serial})",
            "manufacturer": _guess_manufacturer(model),
            "model": model,
            "serial_number": serial,
        }

    @property
    def icon(self) -> str | None:
        """Dynamic icon — switches to alert icon when this sensor triggers attention."""
        key = self.entity_description.key
        value = self.native_value

        # Non-zero triggers (ATA + NVMe critical_warning/media_errors).
        if value is not None and isinstance(value, (int, float)) and value > 0:
            if key in _CRITICAL_SENSOR_KEYS:
                return _ALERT_ICON_CRITICAL
            if key in _WARNING_SENSOR_KEYS:
                return _ALERT_ICON_WARNING

        # NVMe available_spare — threshold-based (needs drive's own threshold).
        if key == _NVME_SPARE_KEY and value is not None and isinstance(value, (int, float)):
            # Get the threshold from the same drive's data.
            drive_data = self.coordinator.data.get(self._drive_id, {})
            threshold = _extract_attribute(drive_data, "available_spare_threshold")
            if threshold is not None and value <= threshold:
                return _ALERT_ICON_CRITICAL
            if value < 20:
                return _ALERT_ICON_WARNING

        # NVMe percentage_used (mapped to wear_leveling_count) — ≥90% = warning.
        if key == _NVME_WEAR_KEY and value is not None and isinstance(value, (int, float)):
            if value >= 90:
                return _ALERT_ICON_WARNING

        # SMART Status — FAILED = critical.
        if key == "smart_status" and value == "FAILED":
            return _ALERT_ICON_CRITICAL

        return self._default_icon

    @property
    def native_value(self) -> Any | None:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return None
        return _extract_attribute(drive_data, self.entity_description.key)


# ---------------------------------------------------------------------------
# Attention Needed sensor (enum: NO / MAYBE / YES / UNSUPPORTED)
# ---------------------------------------------------------------------------

class SmartSnifferAttentionSensor(
    CoordinatorEntity[SmartSnifferCoordinator], SensorEntity
):
    """Enum sensor that aggregates early-warning SMART indicators.

    States:
      NO           All indicators clear.
      MAYBE        Warning-level issues (plan replacement).
      YES          Critical issues (back up immediately).
      UNSUPPORTED  Drive returned no usable SMART data.

    Attributes:
      severity     "critical" | "warning" | "none"
      reasons      List of human-readable trigger descriptions.
      issue_count  Number of active issues.
    """

    _attr_has_entity_name = True
    _attr_name = "Attention Needed"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ATTENTION_STATES

    # Icon per state — provides at-a-glance status on dashboards.
    _STATE_ICONS: dict[str, str] = {
        STATE_NO:          "mdi:check-circle-outline",
        STATE_MAYBE:       "mdi:alert-circle-outline",
        STATE_YES:         "mdi:alert-octagon",
        STATE_UNSUPPORTED: "mdi:help-circle-outline",
    }

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._drive_id = drive_id

        model = drive_data.get("model", "Unknown Drive")
        serial = drive_data.get("serial", drive_id)

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{drive_id}_attention"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, drive_id)},
            "name": f"{model} ({serial})",
            "manufacturer": _guess_manufacturer(model),
            "model": model,
            "serial_number": serial,
        }

    @property
    def icon(self) -> str:
        """Dynamic icon based on current attention state."""
        return self._STATE_ICONS.get(
            self.native_value, "mdi:help-circle-outline"
        )

    @property
    def native_value(self) -> str:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return STATE_UNSUPPORTED
        state, _, _ = evaluate_attention(drive_data)
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        drive_data = self.coordinator.data.get(self._drive_id)
        if not drive_data:
            return {
                "severity": SEVERITY_NONE,
                "reasons": ["Drive data unavailable"],
                "issue_count": 0,
            }
        _, severity, reasons = evaluate_attention(drive_data)
        return {
            "severity": severity,
            "reasons": reasons if reasons else ["No issues detected"],
            "issue_count": len(reasons),
        }


# ---------------------------------------------------------------------------
# Attention Reasons sensor (text: human-readable trigger summary)
# ---------------------------------------------------------------------------

class SmartSnifferAttentionReasonsSensor(
    CoordinatorEntity[SmartSnifferCoordinator], SensorEntity
):
    """Text sensor showing human-readable reasons for the current attention state.

    Provides an at-a-glance answer to "why does this drive need attention?"
    directly on the device page, without needing to inspect entity attributes.

    When attention is NO:          "No issues detected"
    When attention is UNSUPPORTED: "No usable SMART data"
    When attention is MAYBE/YES:   Semicolon-separated list of trigger reasons.
    """

    _attr_has_entity_name = True
    _attr_name = "Attention Reasons"
    _attr_icon = "mdi:text-box-search-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._drive_id = drive_id

        model = drive_data.get("model", "Unknown Drive")
        serial = drive_data.get("serial", drive_id)

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{drive_id}_attention_reasons"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, drive_id)},
            "name": f"{model} ({serial})",
            "manufacturer": _guess_manufacturer(model),
            "model": model,
            "serial_number": serial,
        }

    @property
    def native_value(self) -> str:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return "Drive data unavailable"
        state, _, reasons = evaluate_attention(drive_data)
        if state == STATE_NO:
            return "No issues detected"
        if state == STATE_UNSUPPORTED:
            return "No usable SMART data"
        return "; ".join(reasons)

    @property
    def icon(self) -> str:
        """Dynamic icon matching the attention state."""
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return "mdi:text-box-search-outline"
        state, _, _ = evaluate_attention(drive_data)
        if state == STATE_YES:
            return "mdi:alert-octagon"
        if state == STATE_MAYBE:
            return "mdi:alert-circle-outline"
        return "mdi:text-box-search-outline"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guess_manufacturer(model: str) -> str:
    """Best-effort manufacturer guess from model string."""
    model_lower = model.lower()
    manufacturers = {
        "samsung": "Samsung",
        "seagate": "Seagate",
        "western digital": "Western Digital",
        "wd": "Western Digital",
        "toshiba": "Toshiba",
        "hitachi": "Hitachi",
        "hgst": "HGST",
        "intel": "Intel",
        "crucial": "Crucial (Micron)",
        "micron": "Micron",
        "kingston": "Kingston",
        "sandisk": "SanDisk",
        "sk hynix": "SK Hynix",
        "apple": "Apple",
    }
    for keyword, name in manufacturers.items():
        if keyword in model_lower:
            return name
    return "Unknown"
