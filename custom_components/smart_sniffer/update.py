"""Agent update notices (v1.0, #68 phase 1): an update entity per agent.

Under the agent's device: the installed version is what the agent reports
in /api/health, the latest is LATEST_AGENT_VERSION (const.py). Home
Assistant never installs anything: the summary says what is new and gives
the install command for that machine's OS (agent_update.py).

No entity for the SMART Sniffer App's own agent (the Supervisor updates the
App and its agent with it) or for the Replica and the App's old replica.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .agent_identity import ENTRY_AGENT_MOCK
from .agent_update import agent_behind, release_summary
from .agent_version import display_version
from .const import AGENT_RELEASES_URL, DOMAIN, LATEST_AGENT_VERSION
from .coordinator import AgentHealthCoordinator
from .device_naming import agent_device_info
from .host_name import is_app_entry
from .source import is_replica_data


def has_update_entity(entry: ConfigEntry, live_hostname: Any = None) -> bool:
    """Whether an entry's agent gets an update entity: not the Replica, not
    the App's agent or its old replica."""
    if is_replica_data(entry.data) or entry.data.get(ENTRY_AGENT_MOCK) is True:
        return False
    return not is_app_entry(entry.data, entry.title, live_hostname,
                            source=getattr(entry, "source", None))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    health: AgentHealthCoordinator = data["health_coordinator"]
    hostname = (health.data or {}).get("hostname") if health.data else None
    if not has_update_entity(entry, hostname):
        return
    async_add_entities([AgentUpdateEntity(health, entry)])


class AgentUpdateEntity(CoordinatorEntity[AgentHealthCoordinator], UpdateEntity):
    """The agent's version against the newest release. No install."""

    _attr_has_entity_name = True
    _attr_translation_key = "agent"
    _attr_supported_features = UpdateEntityFeature(0)
    _attr_title = "SMART Sniffer agent"
    _attr_release_url = AGENT_RELEASES_URL

    def __init__(self, coordinator: AgentHealthCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_agent_update"
        self._attr_device_info = agent_device_info(entry)

    @property
    def _health(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def installed_version(self) -> str | None:
        return display_version(self._health.get("version")) or None

    @property
    def latest_version(self) -> str | None:
        """LATEST_AGENT_VERSION when the agent is behind it; else its own
        version, so a dev build or a newer test build never reads as
        needing an update (on any Home Assistant version)."""
        installed = self.installed_version
        if installed is None:
            return None
        return LATEST_AGENT_VERSION if agent_behind(installed) else installed

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        return agent_behind(installed_version, latest_version)

    @property
    def release_summary(self) -> str | None:
        if self.latest_version == self.installed_version:
            return None
        health = self._health
        return release_summary(health.get("os"), health.get("container"))
