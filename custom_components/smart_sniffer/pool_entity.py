"""Shared base for the ZFS pool entities (GH #50).

One Home Assistant device per pool, named "ZFS pool <name> (<host>)", nested
under the agent device the same way the drive and Disk Usage devices are. The
host is in the name because two machines often have a pool of the same name
(rpool on every Proxmox box): without it the devices were indistinguishable in
the device list and the second host's entity ids got a "_2" suffix (GH #50). The pool's
entities read their pool out of the coordinator's ``_pools`` list on every
update, and go unavailable when the agent stops reporting the pool or the last
pool fetch failed. A missing pool (registered, but left out of a pool list that
was read) keeps its State and Problem sensors, which say so; the rest go
unavailable, because nothing is known about them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartSnifferCoordinator
from .device_link import agent_link
from .pool_health import find_pool, is_missing, pool_identifier


class ZfsPoolEntity(CoordinatorEntity[SmartSnifferCoordinator]):
    """An entity belonging to one ZFS pool on one agent."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartSnifferCoordinator, pool_name: str, key: str) -> None:
        super().__init__(coordinator)
        self._pool_name = pool_name
        entry_id = coordinator.config_entry.entry_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{pool_identifier(entry_id, pool_name)}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, pool_identifier(entry_id, pool_name))},
            "name": f"ZFS pool {pool_name} ({coordinator._hostname})",
            "manufacturer": "SMART Sniffer",
            "model": "ZFS Pool",
            # Nest under the agent alongside the drive devices (#24).
            **agent_link(coordinator),
        }

    @property
    def _pool(self) -> dict[str, Any] | None:
        return find_pool(self.coordinator.data, self._pool_name)

    @property
    def _missing(self) -> bool:
        return is_missing(self.coordinator.data, self._pool_name)

    @property
    def available(self) -> bool:
        return super().available and self._pool is not None


class ZfsPoolMissingAwareEntity(ZfsPoolEntity):
    """A pool entity that stays available, and says so, when the pool is missing."""

    @property
    def available(self) -> bool:
        # The coordinator's availability (agent reachable), skipping the
        # pool-present rule of ZfsPoolEntity.
        coordinator_ok = super(ZfsPoolEntity, self).available
        return coordinator_ok and (self._pool is not None or self._missing)
