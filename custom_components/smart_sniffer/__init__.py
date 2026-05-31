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
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

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

            drives: dict[str, Any] = {
                drive_id: drive_data
                for drive_id, drive_data in coord.data.items()
                if not drive_id.startswith("_")
            }

            return {
                "agent": {
                    "host": health.host,
                    "port": health.port,
                    "version": health.data.get("version"),
                    "os": health.data.get("os"),
                    "connected": health.data.get("connected"),
                },
                "drives": drives,
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
