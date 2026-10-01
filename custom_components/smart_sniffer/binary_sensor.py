"""Binary sensors for SMART Sniffer — drive health and standby.

Per-drive binary sensors:

  health  — SMART's official pass/fail verdict + NVMe critical_warning.
            This is the lagging indicator: drives can report PASSED right up
            until catastrophic failure.

            device_class PROBLEM: on = SMART FAILED, off = SMART PASSED.
            Returns None (HA renders "Unknown") when the drive provides no
            usable SMART data (e.g., USB enclosures blocking passthrough).

  standby — Whether the drive is currently spun down. When on, the SMART
            readings for this drive are being served from cache; the
            sensor exposes a data_as_of attribute so consumers can see
            how stale those readings are. Introduced in v0.5.4.

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
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .attention import _has_usable_smart_data, coerce_smart_data
from .const import CONF_TOKEN, DOMAIN
from .device_link import agent_link
from .coordinator import AgentHealthCoordinator, SmartSnifferCoordinator
from .pool_entity import ZfsPoolEntity, ZfsPoolMissingAwareEntity
from .pool_health import (
    MISSING_REASON,
    data_errors,
    pool_names_for_setup,
    pool_problems,
    problem_devices,
)

_LOGGER = logging.getLogger(__name__)


def _evaluate_health(drive_data: dict[str, Any]) -> bool | None:
    """Return True if healthy, False if problem, None if data insufficient.

    Checks the drive's own SMART pass/fail verdict plus the NVMe
    critical_warning bitmask. Returns None when no usable SMART data is
    present — HA renders this as "Unknown" rather than a misleading "OK".
    """
    # Unusable payload yields {}, which the gate below turns into None
    # (unknown), exactly as the inline coercion did.
    smart_data = coerce_smart_data(drive_data)

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
    ata_attrs = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
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
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SmartSnifferCoordinator = data["coordinator"]
    health_coordinator: AgentHealthCoordinator = data["health_coordinator"]

    entities: list[BinarySensorEntity] = []

    # Per-drive health + standby sensors.
    for drive_id, drive_data in coordinator.data.items():
        if drive_id.startswith("_"):
            continue  # skip internal keys like _filesystems
        if drive_data.get("readable") is False:
            # Matches sensor.py: never build a device for a drive the agent
            # could not read. See smart-sniffer-app#7.
            _LOGGER.debug("Skipping unreadable drive %s at setup", drive_id)
            continue
        entities.append(SmartSnifferHealthSensor(coordinator, drive_id, drive_data))
        entities.append(DriveStandbySensor(coordinator, drive_id, drive_data))

    # ZFS pool problem sensors (one device per pool, GH #50), for every pool
    # reported now and every pool device already registered.
    for name in pool_names_for_setup(coordinator.data, coordinator.registered_pool_names()):
        entities.append(ZfsPoolDataErrorsSensor(coordinator, name))
        entities.append(ZfsPoolProblemSensor(coordinator, name))

    # Agent-level connectivity and auth sensors.
    entities.append(AgentStatusBinarySensor(health_coordinator, entry))
    entities.append(AuthActiveBinarySensor(health_coordinator, entry))

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
            # Nest this drive under its agent so HA can cascade an area
            # assignment from the agent to every drive it reports (#24).
            **agent_link(coordinator),
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
        attrs: dict[str, Any] = {}
        smart_data = drive_data.get("smart_data", {})
        if isinstance(smart_data, dict):
            status = smart_data.get("smart_status", {})
            if isinstance(status, dict):
                attrs["smart_passed"] = status.get("passed")
        # DEPRECATED (v0.5.4): the in_standby and data_as_of attributes on
        # this sensor are superseded by the dedicated DriveStandbySensor
        # (binary_sensor.*_standby). Kept here for backward compatibility
        # with v0.5.3 automations/templates. Planned removal in a future
        # release. See docs/internal/process/deprecations.md.
        if drive_data.get("in_standby"):
            attrs["in_standby"] = True
            attrs["data_as_of"] = drive_data.get("last_updated", "unknown")
        return attrs


class DriveStandbySensor(
    CoordinatorEntity[SmartSnifferCoordinator], BinarySensorEntity
):
    """Binary sensor showing whether the drive is currently in standby.

    on  = drive is spun down / sleeping; SMART data served from cache
    off = drive is active; SMART data is fresh

    When on, exposes `data_as_of` as an attribute so consumers can see how
    stale the cached readings are. Introduced in v0.5.4 as the canonical
    replacement for the in_standby/data_as_of attributes previously attached
    to the Health sensor.
    """

    _attr_has_entity_name = True
    _attr_name = "Standby"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

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
            f"{coordinator.config_entry.entry_id}_{drive_id}_standby"
        )
        self._attr_device_info = {
            "identifiers":   {(DOMAIN, drive_id)},
            "name":          f"{model} ({serial})",
            "manufacturer":  model.split()[0] if model else "Unknown",
            "model":         model,
            "serial_number": serial,
            # Nest this drive under its agent so HA can cascade an area
            # assignment from the agent to every drive it reports (#24).
            **agent_link(coordinator),
        }

    @property
    def is_on(self) -> bool:
        """Return True when the drive is in standby."""
        drive_data = self.coordinator.data.get(self._drive_id) or {}
        return bool(drive_data.get("in_standby", False))

    @property
    def icon(self) -> str:
        return "mdi:sleep" if self.is_on else "mdi:power"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.is_on:
            return {}
        drive_data = self.coordinator.data.get(self._drive_id) or {}
        return {"data_as_of": drive_data.get("last_updated", "unknown")}


# ---------------------------------------------------------------------------
# ZFS pool binary sensors (GH #50)
# ---------------------------------------------------------------------------

class ZfsPoolDataErrorsSensor(ZfsPoolEntity, BinarySensorEntity):
    """On when zpool reports permanent data errors in the pool.

    Off for "errors: No known data errors". Unknown when zpool printed no
    errors line, which happens when the pool's configuration cannot be read.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str) -> None:
        super().__init__(coordinator, pool_name, "pool_data_errors")

    @property
    def is_on(self) -> bool | None:
        pool = self._pool
        count = data_errors(pool) if pool is not None else None
        return None if count is None else count > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pool = self._pool
        return {"data_errors": data_errors(pool)} if pool is not None else {}


class ZfsPoolProblemSensor(ZfsPoolMissingAwareEntity, BinarySensorEntity):
    """On when the pool is unhealthy, with the reasons as an attribute.

    Unhealthy means a state other than ONLINE, any read, write or checksum
    error, data errors, or a problem vdev; the problem vdevs are named in the
    reasons and listed in full under problem_devices. The status text ("Some
    supported and requested features are not enabled") never counts. Also on
    when the pool is missing. See pool_health.py.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str) -> None:
        super().__init__(coordinator, pool_name, "pool_problem")

    @property
    def is_on(self) -> bool | None:
        pool = self._pool
        if pool is not None:
            return bool(pool_problems(pool))
        return True if self._missing else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pool = self._pool
        if pool is None:
            if self._missing:
                return {"reasons": [MISSING_REASON], "issue_count": 1, "problem_devices": []}
            return {}
        reasons = pool_problems(pool)
        return {
            "reasons": reasons if reasons else ["No issues detected"],
            "issue_count": len(reasons),
            "problem_devices": problem_devices(pool),
        }


# ---------------------------------------------------------------------------
# Agent connectivity binary sensor
# ---------------------------------------------------------------------------

class AgentStatusBinarySensor(
    CoordinatorEntity[AgentHealthCoordinator], BinarySensorEntity
):
    """Binary sensor showing whether the agent is reachable.

    device_class CONNECTIVITY: on = connected, off = disconnected.
    Because this entity uses AgentHealthCoordinator (which never raises
    UpdateFailed), it stays available even when the agent is offline --
    it just flips to "off".
    """

    _attr_has_entity_name = True
    _attr_name = "Agent Status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_registry_enabled_default = True
    _attr_icon = "mdi:lan-connect"

    def __init__(
        self,
        coordinator: AgentHealthCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent_status"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return True when the agent is reachable."""
        return self.coordinator.data.get("connected", False)

    @property
    def icon(self) -> str:
        if self.is_on:
            return "mdi:lan-connect"
        return "mdi:lan-disconnect"


class AuthActiveBinarySensor(
    CoordinatorEntity[AgentHealthCoordinator], BinarySensorEntity
):
    """Binary sensor showing whether token auth is configured for this agent.

    Reads from the config entry data, not from the agent. Diagnostic only.
    """

    _attr_has_entity_name = True
    _attr_name = "Auth Active"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: AgentHealthCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_auth_active"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def is_on(self) -> bool:
        """Return True when a token is configured."""
        return bool(self._entry.data.get(CONF_TOKEN, ""))

    @property
    def icon(self) -> str:
        return "mdi:lock" if self.is_on else "mdi:lock-open-variant"


def _agent_device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Build device_info for the per-agent device.

    Groups all agent-level entities (status, version, diagnostics) under a
    single device named after the agent's configured hostname or IP.
    """
    from .const import CONF_HOST

    host = entry.data.get(CONF_HOST, "unknown")
    title = entry.title or f"SMART Sniffer ({host})"

    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_agent")},
        "name": title,
        "manufacturer": "SMART Sniffer",
        "model": "Agent",
    }
