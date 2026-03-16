"""Binary sensor for SMART Sniffer — drive health.

One binary sensor per drive:

  health — SMART's official pass/fail verdict + NVMe critical_warning.
           This is the lagging indicator: drives can report PASSED right up
           until catastrophic failure.

           device_class PROBLEM: on = SMART FAILED, off = SMART PASSED.
           Returns None (HA renders "Unknown") when the drive provides no
           usable SMART data (e.g., USB enclosures blocking passthrough).

The early-warning "Attention Needed" sensor lives in sensor.py as an enum
sensor (NO / MAYBE / YES / UNSUPPORTED). See attention.py for the logic.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .attention import _has_usable_smart_data
from .const import DOMAIN
from .coordinator import SmartSnifferCoordinator

_LOGGER = logging.getLogger(__name__)


def _evaluate_health(drive_data: dict[str, Any]) -> bool | None:
    """Return True if healthy, False if problem, None if data insufficient.

    Checks the drive's own SMART pass/fail verdict plus the NVMe
    critical_warning bitmask. Returns None when no usable SMART data is
    present — HA renders this as "Unknown" rather than a misleading "OK".
    """
    smart_data = drive_data.get("smart_data", {})
    if isinstance(smart_data, str):
        import json
        try:
            smart_data = json.loads(smart_data)
        except (json.JSONDecodeError, TypeError):
            return None  # Unparseable → unknown, not "OK".

    # No usable data at all → unknown.
    if not _has_usable_smart_data(smart_data):
        return None

    # Official SMART pass/fail.
    smart_status = smart_data.get("smart_status", {})
    if isinstance(smart_status, dict) and not smart_status.get("passed", True):
        return False

    # NVMe critical_warning bitmask.
    nvme_log = smart_data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        if nvme_log.get("critical_warning", 0) != 0:
            return False
        return True

    # Belt-and-suspenders: check ATA critical thresholds even if SMART passed.
    CRITICAL_ATA: dict[str, int] = {
        "Reallocated_Sector_Ct":     1,
        "Current_Pending_Sector":    1,
        "Current_Pending_Sector_Ct": 1,
        "Total_Pending_Sectors":     1,
        "Reported_Uncorrect":        1,
        "Offline_Uncorrectable":     1,
        "Total_Offl_Uncorrectabl":   1,
        "Uncorrectable_Error_Cnt":   1,
        "Reallocated_Event_Count":   1,
    }
    ata_attrs = smart_data.get("ata_smart_attributes", {}).get("table", [])
    for attr in ata_attrs:
        threshold = CRITICAL_ATA.get(attr.get("name", ""))
        if threshold is not None:
            raw = attr.get("raw", {})
            raw_value = raw.get("value", 0) if isinstance(raw, dict) else 0
            if isinstance(raw_value, (int, float)) and raw_value >= threshold:
                return False

    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SMART Sniffer binary sensor entities from a config entry."""
    coordinator: SmartSnifferCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SmartSnifferHealthSensor] = []
    for drive_id, drive_data in coordinator.data.items():
        entities.append(SmartSnifferHealthSensor(coordinator, drive_id, drive_data))

    async_add_entities(entities, update_before_add=False)


class SmartSnifferHealthSensor(
    CoordinatorEntity[SmartSnifferCoordinator], BinarySensorEntity
):
    """Binary sensor for the official SMART health status.

    on   = SMART FAILED or NVMe critical_warning set (problem detected)
    off  = SMART PASSED, all clear
    None = no usable SMART data (HA renders as "Unknown")
    """

    _attr_has_entity_name = True
    _attr_name = "Health"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:harddisk"

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._drive_id = drive_id
        model  = drive_data.get("model", "Unknown Drive")
        serial = drive_data.get("serial", drive_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{drive_id}_health"
        )
        self._attr_device_info = {
            "identifiers":   {(DOMAIN, drive_id)},
            "name":          f"{model} ({serial})",
            "manufacturer":  model.split()[0] if model else "Unknown",
            "model":         model,
            "serial_number": serial,
        }

    @property
    def is_on(self) -> bool | None:
        """Return True = problem, False = healthy, None = unknown."""
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return None
        health = _evaluate_health(drive_data)
        if health is None:
            return None  # No usable data → HA shows "Unknown"
        return not health  # Invert: healthy=True → is_on=False (no problem)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        drive_data = self.coordinator.data.get(self._drive_id)
        if not drive_data:
            return {}
        smart_data = drive_data.get("smart_data", {})
        if isinstance(smart_data, dict):
            status = smart_data.get("smart_status", {})
            if isinstance(status, dict):
                return {"smart_passed": status.get("passed")}
        return {}
