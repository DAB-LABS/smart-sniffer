"""SMART Sniffer — Home Assistant integration for monitoring disk SMART health.

This integration polls one or more smartha-agent REST endpoints and exposes
each physical drive as a HA Device with sensors for SMART attributes, a
binary_sensor for official SMART health, and an enum sensor for the
proactive Attention Needed assessment.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
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

    # Reload the integration when options are changed via the UI.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SMART Sniffer config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change (token, port, interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
