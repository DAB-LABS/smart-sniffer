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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
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
    coerce_smart_data,
    compose_reasons_text,
    evaluate_attention,
    get_thresholds,
)
from .const import CONF_FORCE_UPDATE, DEFAULT_FORCE_UPDATE, DOMAIN, FILESYSTEMS_KEY
from .extract import (  # noqa: F401 - re-exported under the names they always had
    _DIAG_COUNTER_ATTRS,
    _DIAG_GAUGE_ATTRS,
    ATA_NAME_MAP,
    ATA_ONLY_KEYS,
    NVME_ONLY_KEYS,
    SENSOR_KEYS,
    SKIP_IF_NOT_PRESENT,
    _decode_raw_value,
    _extract_attribute,
)
from .device_link import agent_link
from .devstat import data_volume, page_attributes
from .entity_plan import (
    KIND_AGENT_IP,
    KIND_AGENT_LAST_SEEN,
    KIND_AGENT_OS,
    KIND_AGENT_POLL_INTERVAL,
    KIND_AGENT_PORT,
    KIND_AGENT_VERSION,
    KIND_ATTENTION,
    KIND_ATTENTION_REASONS,
    KIND_ATTRIBUTE,
    KIND_DATA_READ,
    KIND_DATA_WRITTEN,
    KIND_DIAGNOSTIC_ATTR,
    KIND_FILESYSTEM,
    KIND_POOL_ERROR,
    KIND_POOL_LAST_SCRUB,
    KIND_POOL_STATE,
    SENSOR,
    VOLUME_KINDS,
    EntitySpec,
    guarded_listener,
    sensor_specs,
    take_new,
    wants_force_update,
)
from .coordinator import AgentHealthCoordinator, SmartSnifferCoordinator
from .pool_entity import ZfsPoolEntity, ZfsPoolMissingAwareEntity
from .pool_health import (
    POOL_STATES,
    STATE_MISSING,
    error_total,
    last_scrub_end,
    scrub_attributes,
    state_option,
)

_LOGGER = logging.getLogger(__name__)


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
        name="Wear Level (% Used)",
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


DESCRIPTIONS_BY_KEY: dict[str, SensorEntityDescription] = {
    description.key: description for description in SENSOR_DESCRIPTIONS
}

# Data Written and Data Read (D3): bytes the host wrote to and read from the
# drive over its life, shown in TB. ``total`` with no last_reset: a change of
# source or unit is one visible step in the long-term sum, never compounded.
# Names come from entity.sensor.<key>.name in the translations.
VOLUME_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    KIND_DATA_WRITTEN: SensorEntityDescription(
        key=KIND_DATA_WRITTEN,
        translation_key="data_written",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.TERABYTES,
        suggested_display_precision=2,
        icon="mdi:database-arrow-down-outline",
    ),
    KIND_DATA_READ: SensorEntityDescription(
        key=KIND_DATA_READ,
        translation_key="data_read",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.TERABYTES,
        suggested_display_precision=2,
        icon="mdi:database-arrow-up-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
}


def _diagnostic_state_class(attr_name: str) -> SensorStateClass | None:
    """Return a state class for a dynamic diagnostic attribute, or None.

    Without a state class Home Assistant treats the value as a string, so
    these entities get no statistics and cannot be graphed (issue #47).  We
    only classify attributes whose semantics are well understood.
    """
    if attr_name in _DIAG_COUNTER_ATTRS:
        return SensorStateClass.TOTAL_INCREASING
    if attr_name in _DIAG_GAUGE_ATTRS:
        return SensorStateClass.MEASUREMENT
    return None


# ---------------------------------------------------------------------------
# Entity setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SMART Sniffer sensor entities from a config entry.

    Which entities exist is decided in entity_plan.py, from the payload, at
    setup and again after every poll (D7): a pool, filesystem or drive that
    appears later, or a drive's Data Written once it has a value, is added
    without a reload. Each entity is added once; ``coordinator.created``
    records it before the add.
    """
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: SmartSnifferCoordinator = data["coordinator"]
    health_coordinator: AgentHealthCoordinator = data["health_coordinator"]
    created = coordinator.created.setdefault(SENSOR, {})

    # --- Optional force_update (issue #40) ---
    # When enabled in options, write a state on every poll even when the value
    # is unchanged, so external time-series stores (InfluxDB, Prometheus) get a
    # datapoint each cycle and Grafana charts have no gaps. Off by default to
    # keep the default recorder lean. Applies to the SMART data, attention, and
    # filesystem sensors; the agent metadata sensors are excluded since forced
    # writes add nothing there. Read from entry.data because this integration's
    # options flow persists into entry.data, not entry.options.
    force = bool(entry.data.get(CONF_FORCE_UPDATE, DEFAULT_FORCE_UPDATE))

    def build(specs: list[EntitySpec]) -> list[SensorEntity]:
        entities = []
        for spec in specs:
            entity = _build_sensor(spec, coordinator, health_coordinator, entry)
            if entity is None:
                continue
            if wants_force_update(spec, force):
                entity._attr_force_update = True
            entities.append(entity)
        return entities

    plan = sensor_specs(
        coordinator.data, entry.entry_id,
        coordinator.registered_pool_names(), coordinator.drive_classes,
    )
    async_add_entities(build(take_new(plan, created)), update_before_add=False)

    def _add_new() -> None:
        # Pools registered but not reported were planned at setup; after it,
        # only what the agent reports can be new.
        later = sensor_specs(coordinator.data, entry.entry_id, (), coordinator.drive_classes)
        new = take_new(later, created)
        if new:
            async_add_entities(build(new), update_before_add=False)

    entry.async_on_unload(
        coordinator.async_add_listener(guarded_listener(_add_new, _LOGGER))
    )


def _build_sensor(
    spec: EntitySpec,
    coordinator: SmartSnifferCoordinator,
    health: AgentHealthCoordinator,
    entry: ConfigEntry,
) -> SensorEntity | None:
    """The entity for one spec. The one place sensors are constructed."""
    kind = spec.kind
    if kind in AGENT_SENSORS:
        return AGENT_SENSORS[kind](health, entry)
    if kind == KIND_FILESYSTEM:
        for fs_info in coordinator.data.get(FILESYSTEMS_KEY, []):
            if fs_info.get("id") == spec.subject:
                return SmartSnifferFilesystemSensor(coordinator, fs_info)
        return None
    if kind == KIND_POOL_STATE:
        return ZfsPoolStateSensor(coordinator, spec.subject)
    if kind == KIND_POOL_ERROR:
        return ZfsPoolErrorSensor(coordinator, spec.subject, spec.key)
    if kind == KIND_POOL_LAST_SCRUB:
        return ZfsPoolLastScrubSensor(coordinator, spec.subject)

    drive_data = coordinator.data.get(spec.subject)
    if drive_data is None:
        return None
    drive_id = spec.subject
    if kind == KIND_ATTRIBUTE:
        return SmartSnifferSensor(coordinator, drive_id, drive_data, DESCRIPTIONS_BY_KEY[spec.key])
    if kind in VOLUME_KINDS:
        return SmartSnifferDataVolumeSensor(
            coordinator, drive_id, drive_data, VOLUME_DESCRIPTIONS[kind], VOLUME_KINDS[kind]
        )
    if kind == KIND_ATTENTION:
        return SmartSnifferAttentionSensor(coordinator, drive_id, drive_data)
    if kind == KIND_ATTENTION_REASONS:
        return SmartSnifferAttentionReasonsSensor(coordinator, drive_id, drive_data)
    if kind == KIND_DIAGNOSTIC_ATTR:
        # Convert smartctl name to friendly name:
        # "Total_SLC_Erase_Ct" -> "Total SLC Erase Ct"
        diag_description = SensorEntityDescription(
            key=spec.key,
            name=spec.attr_name.replace("_", " "),
            icon="mdi:database-search-outline",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            # Known counters/gauges get a state class so HA treats them
            # as numeric and records statistics.  Unknown attributes
            # stay unclassified on purpose.  See issue #47.
            state_class=_diagnostic_state_class(spec.attr_name),
        )
        return SmartSnifferDiagnosticAttrSensor(
            coordinator, drive_id, drive_data, diag_description, spec.attr_id, spec.attr_name,
        )
    return None


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
            # Nest this drive under its agent so HA can cascade an area
            # assignment from the agent to every drive it reports (#24).
            **agent_link(coordinator),
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Standby attributes when the drive is sleeping, and the Device
        Statistics readings this sensor shows beside its value (this poll's
        pages only, never held)."""
        drive_data = self.coordinator.data.get(self._drive_id, {})
        attrs: dict[str, Any] = {}
        if drive_data.get("in_standby"):
            attrs["in_standby"] = True
            attrs["data_as_of"] = drive_data.get("last_updated", "unknown")
        attrs.update(page_attributes(drive_data, self.entity_description.key))
        return attrs


# ---------------------------------------------------------------------------
# Data Written / Data Read (D3)
# ---------------------------------------------------------------------------

class SmartSnifferDataVolumeSensor(SmartSnifferSensor):
    """Bytes written to or read from the drive by the host, over its life.

    The value is the agent's ``derived`` figure (the standard Device
    Statistics counter, the NVMe counter, or a vendor attribute that states
    its unit), else the last Device Statistics value Home Assistant held, with
    ``held: true``, else unknown. See devstat.py.
    """

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
        description: SensorEntityDescription,
        volume_key: str,
    ) -> None:
        super().__init__(coordinator, drive_id, drive_data, description)
        self._volume_key = volume_key

    @property
    def icon(self) -> str | None:
        return self._default_icon

    @property
    def native_value(self) -> int | None:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return None
        return data_volume(drive_data, self._volume_key)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        drive_data = self.coordinator.data.get(self._drive_id) or {}
        return data_volume(drive_data, self._volume_key)[1]


# ---------------------------------------------------------------------------
# Dynamic diagnostic attribute sensor
# ---------------------------------------------------------------------------

class SmartSnifferDiagnosticAttrSensor(SmartSnifferSensor):
    """A SMART attribute sensor created dynamically from the attribute table.

    These are disabled by default.  When enabled, they show the raw value
    of a vendor-specific SMART attribute identified by its numeric ID.
    """

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        drive_id: str,
        drive_data: dict[str, Any],
        description: SensorEntityDescription,
        attr_id: int,
        attr_name: str,
    ) -> None:
        super().__init__(coordinator, drive_id, drive_data, description)
        self._attr_id = attr_id
        self._attr_name_raw = attr_name

    @property
    def native_value(self) -> Any | None:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return None
        smart_data = coerce_smart_data(drive_data)

        ata_attrs = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
        for attr in ata_attrs:
            if attr.get("id") == self._attr_id:
                # Unpack vendor-compound raw values here too -- diagnostic
                # entities previously returned raw.value untouched, which
                # showed packed integers on drives that pack.  See issue #44.
                return _decode_raw_value(attr.get("raw", {}))
        return None


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
            # Nest this drive under its agent so HA can cascade an area
            # assignment from the agent to every drive it reports (#24).
            **agent_link(coordinator),
        }

    def _thresholds(self) -> dict[str, int]:
        """Threshold overrides for this drive, from entry.data."""
        return get_thresholds(self.coordinator.config_entry, self._drive_id)

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
        state, _, _, _ = evaluate_attention(drive_data, self._thresholds())
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        drive_data = self.coordinator.data.get(self._drive_id)
        if not drive_data:
            return {
                "severity": SEVERITY_NONE,
                "reasons": ["Drive data unavailable"],
                "issue_count": 0,
                "accepted": [],
            }
        _, severity, reasons, accepted = evaluate_attention(
            drive_data, self._thresholds()
        )
        return {
            "severity": severity,
            "reasons": reasons if reasons else ["No issues detected"],
            # issue_count counts actionable reasons only. Accepted readings are
            # explicitly not issues, which is the whole point of accepting them.
            "issue_count": len(reasons),
            "accepted": accepted,
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
            # Nest this drive under its agent so HA can cascade an area
            # assignment from the agent to every drive it reports (#24).
            **agent_link(coordinator),
        }

    def _thresholds(self) -> dict[str, int]:
        """Threshold overrides for this drive, from entry.data."""
        return get_thresholds(self.coordinator.config_entry, self._drive_id)

    @property
    def native_value(self) -> str:
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return "Drive data unavailable"
        state, _, reasons, accepted = evaluate_attention(
            drive_data, self._thresholds()
        )
        # Composed in attention.py so the 255-character cap is covered by the
        # tier-1 suite; this module cannot be imported without Home Assistant.
        return compose_reasons_text(state, reasons, accepted)

    @property
    def icon(self) -> str:
        """Dynamic icon matching the attention state."""
        drive_data = self.coordinator.data.get(self._drive_id)
        if drive_data is None:
            return "mdi:text-box-search-outline"
        state, _, _, _ = evaluate_attention(drive_data, self._thresholds())
        if state == STATE_YES:
            return "mdi:alert-octagon"
        if state == STATE_MAYBE:
            return "mdi:alert-circle-outline"
        return "mdi:text-box-search-outline"


# ---------------------------------------------------------------------------
# Filesystem usage sensor (one per monitored mountpoint)
# ---------------------------------------------------------------------------

def _bytes_to_gb(b: int | float) -> float:
    """Convert bytes to GB, rounded to 1 decimal."""
    return round(b / (1024 ** 3), 1)


def _friendly_mountpoint(mountpoint: str) -> str:
    """Human-readable label for a mountpoint path."""
    if mountpoint == "/":
        return "Root (/)"
    return mountpoint


class SmartSnifferFilesystemSensor(
    CoordinatorEntity[SmartSnifferCoordinator], SensorEntity
):
    """Disk usage percentage sensor for a monitored filesystem.

    One sensor per mountpoint configured on the agent host. Attributes
    expose total/used/available in GB plus filesystem metadata.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:harddisk"

    def __init__(
        self,
        coordinator: SmartSnifferCoordinator,
        fs_info: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._fs_id: str = fs_info["id"]
        mountpoint = fs_info.get("mountpoint", "unknown")
        self._attr_name = f"Disk Usage — {_friendly_mountpoint(mountpoint)}"

        # Unique ID: entry + filesystem id guarantees uniqueness across hosts.
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{self._fs_id}_usage"
        )

        # Group under a device named after the host, so filesystem sensors
        # sit alongside the drive devices on the integration page.
        hostname = coordinator._hostname
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{coordinator.config_entry.entry_id}_filesystems")},
            "name": f"Disk Usage ({hostname})",
            "manufacturer": "SMART Sniffer",
            "model": "Filesystem Monitor",
            # Nest under the agent alongside the drive devices (#24).
            **agent_link(coordinator),
        }

    def _get_fs_data(self) -> dict[str, Any] | None:
        """Find this filesystem in the coordinator data."""
        for fs in self.coordinator.data.get(FILESYSTEMS_KEY, []):
            if fs.get("id") == self._fs_id:
                return fs
        return None

    @property
    def available(self) -> bool:
        """Mark entity unavailable when the mount disappears."""
        fs = self._get_fs_data()
        if fs is None:
            return False
        return fs.get("status") == "ok"

    @property
    def native_value(self) -> float | None:
        fs = self._get_fs_data()
        if fs is None or fs.get("status") != "ok":
            return None
        return fs.get("use_percent")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fs = self._get_fs_data()
        if fs is None:
            return {}
        return {
            "mountpoint": fs.get("mountpoint"),
            "device": fs.get("device"),
            "fstype": fs.get("fstype"),
            "total_gb": _bytes_to_gb(fs.get("total_bytes", 0)),
            "used_gb": _bytes_to_gb(fs.get("used_bytes", 0)),
            "available_gb": _bytes_to_gb(fs.get("available_bytes", 0)),
        }


# ---------------------------------------------------------------------------
# ZFS pool sensors (GH #50)
# ---------------------------------------------------------------------------

class ZfsPoolStateSensor(ZfsPoolMissingAwareEntity, SensorEntity):
    """The pool's state as zpool reports it, with its status text.

    MISSING when the agent's pool list no longer includes the pool.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = POOL_STATES
    _attr_icon = "mdi:database-check-outline"

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str) -> None:
        super().__init__(coordinator, pool_name, "pool_state")

    @property
    def native_value(self) -> str | None:
        pool = self._pool
        if pool is not None:
            return state_option(pool)
        return STATE_MISSING if self._missing else None

    @property
    def icon(self) -> str:
        if self.native_value in (None, "ONLINE"):
            return "mdi:database-check-outline"
        return "mdi:database-alert-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pool = self._pool or {}
        # Informational, passed through as the agent read it. Never part of
        # the health rule: see pool_health.py.
        return {"status": pool.get("status"), "action": pool.get("action")}


class ZfsPoolErrorSensor(ZfsPoolEntity, SensorEntity):
    """One of the pool's read, write or checksum error totals.

    A measurement, not total_increasing: `zpool clear` resets the counters,
    and a reset is not a meter rollover.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str, key: str) -> None:
        super().__init__(coordinator, pool_name, f"pool_{key}")
        self._key = key

    @property
    def native_value(self) -> int | None:
        pool = self._pool
        return error_total(pool, self._key) if pool is not None else None


class ZfsPoolLastScrubSensor(ZfsPoolEntity, SensorEntity):
    """When the pool's last completed scrub ended.

    Unknown when the pool has never been scrubbed, and also when a resilver
    has run since: a pool keeps one scan record, so the scrub's is gone.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:broom"

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str) -> None:
        super().__init__(coordinator, pool_name, "pool_last_scrub")

    @property
    def native_value(self) -> Any | None:
        pool = self._pool
        return last_scrub_end(pool) if pool is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pool = self._pool
        return scrub_attributes(pool) if pool is not None else {}


# ---------------------------------------------------------------------------
# Agent diagnostic sensors
# ---------------------------------------------------------------------------

def _agent_device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Build device_info for the per-agent device (shared with binary_sensor)."""
    from .const import CONF_HOST
    host = entry.data.get(CONF_HOST, "unknown")
    title = entry.title or f"SMART Sniffer ({host})"
    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_agent")},
        "name": title,
        "manufacturer": "SMART Sniffer",
        "model": "Agent",
    }


class AgentVersionSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """Agent version reported by /api/health."""

    _attr_has_entity_name = True
    _attr_name = "Agent Version"
    _attr_icon = "mdi:tag-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_agent_version"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("version") or None


class AgentLastSeenSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """Timestamp of the last successful health check."""

    _attr_has_entity_name = True
    _attr_name = "Agent Last Seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_agent_last_seen"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self):
        """Return UTC-aware datetime. HA renders in user's timezone."""
        from datetime import datetime, timezone
        last_seen = self.coordinator.data.get("last_seen")
        if last_seen is None:
            return None
        try:
            dt = datetime.fromisoformat(last_seen)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None


class AgentIPSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """Agent IP address from config entry (read-only diagnostic)."""

    _attr_has_entity_name = True
    _attr_name = "Agent IP"
    _attr_icon = "mdi:ip-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent_ip"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self) -> str:
        from .const import CONF_HOST
        return self._entry.data.get(CONF_HOST, "unknown")


class AgentPortSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """Agent port from config entry (read-only diagnostic)."""

    _attr_has_entity_name = True
    _attr_name = "Agent Port"
    _attr_icon = "mdi:ethernet"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent_port"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self) -> int:
        from .const import CONF_PORT
        return self._entry.data.get(CONF_PORT, 9099)


class AgentOSSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """Agent host OS (linux, darwin, windows) from the health endpoint."""

    _attr_has_entity_name = True
    _attr_name = "OS"
    _attr_icon = "mdi:monitor"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent_os"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self) -> str | None:
        value = self.coordinator.data.get("os")
        return value or None


class AgentPollIntervalSensor(
    CoordinatorEntity[AgentHealthCoordinator], SensorEntity
):
    """HA-side poll interval from config entry (read-only diagnostic).

    Reports how often Home Assistant pulls fresh data from the agent.
    Distinct from the agent's own scan_interval (how often the agent
    reads SMART data from drives).

    Unique ID retains the legacy "_agent_scan_interval" suffix so existing
    entity registry entries are preserved across the v0.5.4 rename.
    """

    _attr_has_entity_name = True
    _attr_name = "HA Poll Interval"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent_scan_interval"
        self._attr_device_info = _agent_device_info(entry)

    @property
    def native_value(self) -> int:
        from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        return self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)


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


# The agent diagnostic sensors by entity plan kind.
AGENT_SENSORS: dict[str, Any] = {
    KIND_AGENT_VERSION: AgentVersionSensor,
    KIND_AGENT_LAST_SEEN: AgentLastSeenSensor,
    KIND_AGENT_IP: AgentIPSensor,
    KIND_AGENT_PORT: AgentPortSensor,
    KIND_AGENT_OS: AgentOSSensor,
    KIND_AGENT_POLL_INTERVAL: AgentPollIntervalSensor,
}
