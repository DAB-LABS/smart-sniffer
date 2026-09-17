"""SMART Sniffer — Home Assistant integration for monitoring disk SMART health.

This integration polls one or more smartha-agent REST endpoints and exposes
each physical drive as a HA Device with sensors for SMART attributes, a
binary_sensor for official SMART health, and an enum sensor for the
proactive Attention Needed assessment.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry

from .attention import evaluate_attention
from .const import DOMAIN, FILESYSTEMS_KEY, SERVICE_GET_DRIVE_DATA
from .coordinator import AgentHealthCoordinator, SmartSnifferCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SMART Sniffer from a config entry."""
    coordinator = SmartSnifferCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    health_coordinator = AgentHealthCoordinator(hass, entry)
    await health_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "health_coordinator": health_coordinator,
    }

    # Register the agent device up front, before any platform loads (#24).
    # Drive and filesystem devices point at it with via_device, and HA only
    # resolves that link if the parent already exists in the device registry.
    # Platforms are set up concurrently, so creating it here is the only way
    # to guarantee the parent is present whichever platform registers first.
    device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_agent")},
        name=entry.title or f"SMART Sniffer ({entry.data.get(CONF_HOST, 'unknown')})",
        manufacturer="SMART Sniffer",
        model="Agent",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
                state, severity, reasons = evaluate_attention(drive_data)
                drives[drive_id] = {
                    "model": drive_data.get("model"),
                    "serial": drive_data.get("serial"),
                    "protocol": drive_data.get("protocol"),
                    "device_path": drive_data.get("device_path"),
                    "attention": {
                        "state": state,
                        "severity": severity,
                        "reasons": reasons,
                    },
                    "smart_data": drive_data.get("smart_data"),
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

    # Reload the integration when options are changed via the UI.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SMART Sniffer config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_GET_DRIVE_DATA)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change (token, port, interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
