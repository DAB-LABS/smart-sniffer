"""SMART Sniffer — Home Assistant integration for monitoring disk SMART health.

This integration polls one or more smartha-agent REST endpoints and exposes
each physical drive as a HA Device with sensors for SMART attributes, a
binary_sensor for official SMART health, and an enum sensor for the
proactive Attention Needed assessment.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .agent_identity import ENTRY_IDENTITY_KEYS
from .attention import evaluate_attention, get_thresholds
from .const import (
    AGENT_DEVICE_ID,
    DOMAIN,
    FILESYSTEMS_KEY,
    SERVICE_GET_DRIVE_DATA,
    STORE_KEY_PREFIX,
    STORE_VERSION,
)
from .device_naming import NAMED_HOST, agent_device_info, async_sync_names, display_host
from .device_removal import (
    our_identifier,
    refusal_placeholders,
    removable,
    reported_drive_ids,
    reported_filesystem_count,
    reported_pool_names,
)
from .coordinator import AgentHealthCoordinator, SmartSnifferCoordinator
from .panel_data import SIGNAL_PANEL_UPDATE, panel_data, utc_text
from .source import agent_address, is_replica_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Set up only from config entries; no YAML.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Once per Home Assistant run: the SMART Sniffer panel (v0.9). Guarded:
    whatever happens there, the entries are set up and monitored."""
    try:
        from .panel_setup import async_setup_panel  # noqa: PLC0415

        await async_setup_panel(hass)
    except Exception:  # noqa: BLE001 - the panel never stops monitoring
        _LOGGER.exception("SMART Sniffer panel could not be set up; monitoring continues")
    # The SMART Sniffer App's old Replica machines go once Home Assistant
    # runs (replica.py); Replicas now live in the integration.
    try:
        from .replica import async_schedule_legacy_removal  # noqa: PLC0415

        async_schedule_legacy_removal(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SMART Sniffer: old Replica machines not checked")
    return True


def _panel_hooks(hass: HomeAssistant, entry: ConfigEntry,
                 coordinator: SmartSnifferCoordinator,
                 health_coordinator: AgentHealthCoordinator) -> None:
    """Tell the panel about every poll of this entry (v0.9): the time of the
    last one that answered, and a signal its open pages listen to. Never
    raises; the panel only reads what entries publish."""
    try:
        pd = panel_data(hass)
        if coordinator.last_update_success:
            pd.read_at[entry.entry_id] = utc_text()

        def on_update() -> None:
            if coordinator.last_update_success:
                pd.read_at[entry.entry_id] = utc_text()
            async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, entry.entry_id)

        def on_health() -> None:
            async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, entry.entry_id)

        entry.async_on_unload(coordinator.async_add_listener(on_update))
        entry.async_on_unload(health_coordinator.async_add_listener(on_health))
        async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, entry.entry_id)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SMART Sniffer panel: entry %s not linked", entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SMART Sniffer from a config entry."""
    # The Device Statistics hold and announcement records (devstat.py), loaded
    # before the first refresh so that refresh already merges against them and
    # a drive announced before a restart is not announced again.
    from . import replica as replica_mod  # noqa: PLC0415

    if replica_mod.is_legacy_app_replica(entry):
        # The SMART Sniffer App's old Replica machine: never polled again,
        # removed (with a one-time note) instead.
        replica_mod.async_schedule_legacy_removal(hass)
        raise ConfigEntryNotReady("Replicas moved into the integration")
    # The Replica machine (v0.9) reads the replica engine in process.
    source = await replica_mod.async_source(hass, entry) if is_replica_data(entry.data) else None

    replica_kw = {"replica": source} if source is not None else {}

    store = Store(hass, STORE_VERSION, _store_key(entry.entry_id))
    coordinator = SmartSnifferCoordinator(hass, entry, store, await store.async_load(),
                                          **replica_kw)
    await coordinator.async_config_entry_first_refresh()

    health_coordinator = AgentHealthCoordinator(hass, entry, **replica_kw)
    await health_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "health_coordinator": health_coordinator,
        "replica": source,
    }

    _panel_hooks(hass, entry, coordinator, health_coordinator)

    # Register the agent device up front, before any platform loads (#24).
    # Drive and filesystem devices point at it with via_device, and HA only
    # resolves that link if the parent already exists in the device registry.
    # Platforms are set up concurrently, so creating it here is the only way
    # to guarantee the parent is present whichever platform registers first.
    agent_device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **agent_device_info(entry)
    )
    # Its registry id, which is what via_device_id takes. Stored rather than
    # looked up again per entity, and refreshed on every setup so a reload
    # cannot leave a stale id behind.
    hass.data[DOMAIN][entry.entry_id][AGENT_DEVICE_ID] = agent_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Device names start with the host (GH #50). The entities' device info
    # names the devices they are added to; this also renames the ones whose
    # entities are all disabled. Never raises.
    async_sync_names(hass, entry)

    # Register the get_drive_data service once per domain.
    if not hass.services.has_service(DOMAIN, SERVICE_GET_DRIVE_DATA):
        async def handle_get_drive_data(call: ServiceCall) -> dict[str, Any]:
            entry_id: str = call.data["config_entry_id"]
            domain_data = hass.data.get(DOMAIN, {})
            if entry_id not in domain_data:
                raise ServiceValidationError(
                    f"No SMART Sniffer entry found with ID: {entry_id}"
                )
            coord = domain_data[entry_id]["coordinator"]
            health = domain_data[entry_id]["health_coordinator"]

            drives: dict[str, Any] = {}
            for drive_id, drive_data in coord.data.items():
                if drive_id.startswith("_"):
                    continue
                state, severity, reasons, accepted = evaluate_attention(
                    drive_data, get_thresholds(coord.config_entry, drive_id)
                )
                drives[drive_id] = {
                    "model": drive_data.get("model"),
                    "serial": drive_data.get("serial"),
                    "protocol": drive_data.get("protocol"),
                    "device_path": drive_data.get("device_path"),
                    "attention": {
                        "state": state,
                        "severity": severity,
                        "reasons": reasons,
                        "accepted": accepted,
                    },
                    "smart_data": drive_data.get("smart_data"),
                    # v0.8.0 agents; absent from older ones. No identifiers.
                    "device_statistics": drive_data.get("device_statistics"),
                    "derived": drive_data.get("derived"),
                }

            raw_filesystems: list[dict[str, Any]] = coord.data.get(FILESYSTEMS_KEY, [])
            filesystems = [
                {
                    "mountpoint": fs.get("mountpoint"),
                    "total_bytes": fs.get("total_bytes"),
                    "used_bytes": fs.get("used_bytes"),
                    "use_percent": fs.get("use_percent"),
                }
                for fs in raw_filesystems
            ]

            return {
                "agent": {
                    "name": health.config_entry.title,
                    "host": health.host,
                    "os": health.data.get("os"),
                    "uptime": health.data.get("uptime_seconds"),
                    "version": health.data.get("version"),
                },
                "drives": drives,
                "filesystems": filesystems,
            }

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_DRIVE_DATA,
            handle_get_drive_data,
            schema=vol.Schema({vol.Required("config_entry_id"): str}),
            supports_response=SupportsResponse.ONLY,
        )

    # Reload the integration when options are changed via the UI, but not
    # when the coordinator only stored what it learned about the agent, and
    # only refresh when the thresholds changed.
    hass.data[DOMAIN][entry.entry_id][_SETTINGS] = _settings(entry)
    hass.data[DOMAIN][entry.entry_id][_THRESHOLDS] = _thresholds(entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: device_registry.DeviceEntry,
) -> bool:
    """Whether the user may delete this device from the integration.

    Defining this function is what puts a Delete button on the integration's
    devices at all, which is why the v0.6.2 note about deleting ghost devices
    could not be followed: without it Home Assistant offers no such button.

    Core shows the button on every device of the entry and asks here only once
    the user confirms. Returning False gets core's fixed "rejected by
    integration" message, so a refusal is raised instead, as a translated
    HomeAssistantError that core passes to the frontend unchanged. The reason is
    also logged, because not every page shows it: in frontend 20260826.7 the
    integration page's device row renders it, while the device page renders a
    plain error object as "[object Object]" whatever is raised.

    A drive deleted while it is still attached re-registers on the next poll.
    """
    coordinator = (hass.data.get(DOMAIN, {}).get(config_entry.entry_id) or {}).get(
        "coordinator"
    )
    data = getattr(coordinator, "data", None)
    agent_reporting = bool(coordinator is not None and coordinator.last_update_success)

    decision = removable(
        device_entry.identifiers,
        config_entry.entry_id,
        reported_drive_ids(data),
        reported_filesystem_count(data),
        agent_reporting,
        reported_pool_names(data),
    )
    # The log names a drive by its model: its device name carries the serial.
    name = (device_entry.model if device_entry.serial_number else None) or (
        device_entry.name or device_entry.id
    )

    if decision.allowed:
        _LOGGER.info("Removing device %s: %s", name, decision.reason)
        # A removed pool device is a pool gone on purpose: dismiss its
        # notification and never report it missing again.
        pool = refusal_placeholders(device_entry.identifiers, config_entry.entry_id).get("pool")
        if pool is not None and coordinator is not None:
            coordinator.forget_pool(pool)
        # Whatever it was, forget what was created for it, so it is built
        # again if it comes back; a drive also loses its held Device
        # Statistics readings and its announcement records.
        identifier = our_identifier(device_entry.identifiers)
        if identifier is not None and coordinator is not None:
            coordinator.forget_device(identifier, device_entry.serial_number)
        return True

    _LOGGER.warning("Refusing to remove device %s: %s", name, decision.reason)
    if decision.translation_key is None:
        return False
    # A pool refusal names the pool as well as the host.
    extra = refusal_placeholders(device_entry.identifiers, config_entry.entry_id)
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=decision.translation_key,
        translation_placeholders={"host": _display_host(config_entry, coordinator), **extra},
    )


def _display_host(config_entry: ConfigEntry, coordinator: Any) -> str:
    """The host as the integration already shows it to users.

    The coordinator derives it from the entry title, falling back to the
    address, and uses it in the agent-update repair notice: the user must
    find the entry by it, so it is not the device names' host label. Without
    a coordinator the address is the next best thing.
    """
    hostname = getattr(coordinator, "_hostname", None)
    if hostname:
        return str(hostname)
    return str(agent_address(config_entry.data) or config_entry.title)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SMART Sniffer config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_GET_DRIVE_DATA)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is deleted: delete its Device Statistics Store with it, and
    what the panel kept of it (its last view, label, names and order)."""
    await Store(hass, STORE_VERSION, _store_key(entry.entry_id)).async_remove()
    if is_replica_data(entry.data):
        try:
            from .replica import async_forget_entry  # noqa: PLC0415

            await async_forget_entry(hass, entry.entry_id)
        except Exception:  # noqa: BLE001 - never stops the removal
            _LOGGER.exception("SMART Sniffer: the Replica's picks were not removed")
    try:
        pd = panel_data(hass)
        pd.forget(entry.entry_id)
        await pd.views.async_remove(entry.entry_id)
        await pd.state.async_forget_entry(entry.entry_id)
    except Exception:  # noqa: BLE001 - never stops the removal
        _LOGGER.exception("SMART Sniffer panel: %s not forgotten", entry.entry_id)
    try:
        # A machine the SMART Sniffer App handed over is never made again.
        from .panel_import import async_forget_entry as forget_import  # noqa: PLC0415

        await forget_import(hass, entry.entry_id)
    except Exception:  # noqa: BLE001 - never stops the removal
        _LOGGER.exception("SMART Sniffer: the App handover record of %s not updated",
                          entry.entry_id)


def _store_key(entry_id: str) -> str:
    return f"{STORE_KEY_PREFIX}.{entry_id}"


# Key under hass.data[DOMAIN][entry_id]: the entry as set up, less the agent
# identity the coordinator keeps (agent_identity.ENTRY_IDENTITY_KEYS) and the
# thresholds.
_SETTINGS = "settings_at_setup"

# Key under hass.data[DOMAIN][entry_id]: the per-drive thresholds in force.
# Everything that uses them reads them from the entry when it runs
# (attention.get_thresholds), so a change needs a refresh, not a reload.
_THRESHOLDS = "thresholds_applied"

# No name sync has completed for the entry yet.
_UNNAMED = object()


def _settings(entry: ConfigEntry) -> tuple[Any, ...]:
    data = {
        k: v
        for k, v in entry.data.items()
        if k not in ENTRY_IDENTITY_KEYS and k != "thresholds"
    }
    return (entry.title, entry.unique_id, data, dict(entry.options))


def _thresholds(entry: ConfigEntry) -> dict[str, Any]:
    return copy.deepcopy(entry.data.get("thresholds") or {})


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change (token, port, interval).

    An update that only stored what the coordinator learned about the agent
    (coordinator._store_identity) changes nothing the entry was set up with,
    so it does not reload. When it changed the host label (the agent reported
    a new host name, or its first), the devices are renamed instead.

    A threshold save (the options flow's threshold form) does not reload
    either: the drives are polled again and judged by the new thresholds,
    and nothing is unloaded. Thresholds saved together with any other
    change reload, which applies both.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    at_setup = entry_data.get(_SETTINGS)
    if at_setup is not None and at_setup == _settings(entry):
        if entry_data.get(NAMED_HOST, _UNNAMED) != display_host(entry):
            async_sync_names(hass, entry)
        thresholds = _thresholds(entry)
        if entry_data.get(_THRESHOLDS, {}) != thresholds:
            entry_data[_THRESHOLDS] = thresholds
            await entry_data["coordinator"].async_request_refresh()
        return
    await hass.config_entries.async_reload(entry.entry_id)
