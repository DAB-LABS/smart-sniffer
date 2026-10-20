"""Device names in the registry: the entities' device info and the name sync.

The names themselves come from host_name.py. They are written two ways:

  - by each entity's device info, when the entity is added (every setup);
  - by async_sync_names, once after the platforms are set up and again when
    the entry's host label changes (the agent reported a new host name, or
    its first one). That covers a rename while Home Assistant runs, without
    a reload, and devices whose entities are all disabled.

Both read the entry and the device registry, never a poll's data, so they
agree, and the sync is never run from a coordinator update. A device the user
renamed keeps its name: only ``name`` is written, never ``name_by_user``, and
entity ids are never touched.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .source import agent_address
from .device_link import agent_link
from .device_removal import our_identifier
from .host_name import (
    agent_name,
    disk_usage_name,
    drive_name,
    entry_host,
    legacy_host,
    pick_shared_host,
    pool_name,
)
from .pool_health import pool_name_from_identifier

_LOGGER = logging.getLogger(__name__)

# Key under hass.data[DOMAIN][entry_id]: the host label the last name sync
# used, so the update listener syncs again only when it changes.
NAMED_HOST = "named_host"

# From 2026.8 a device registered by two config entries with the same
# identifier is two devices, one per entry. Before, it is one device with
# both entries in its config_entries, and both name it.
PER_ENTRY_DEVICES = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8)


def display_host(entry: ConfigEntry) -> str | None:
    """The host label for this entry's devices (host_name.entry_host)."""
    return entry_host(entry.data, entry.title, entry.source)


def agent_device_info(entry: ConfigEntry) -> dict[str, Any]:
    """Device info for the per-agent device (sensor and binary_sensor)."""
    return {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_agent")},
        "name": agent_name(entry.title, agent_address(entry.data), display_host(entry)),
        "manufacturer": "SMART Sniffer",
        "model": "Agent",
    }


def drive_device_info(
    coordinator: Any,
    drive_id: str,
    drive_data: dict[str, Any],
    manufacturer: str,
) -> dict[str, Any]:
    """Device info for a drive. The identifier carries no entry id, so the
    same drive reported by two entries can be one device (drive_host)."""
    model = drive_data.get("model", "Unknown Drive")
    serial = drive_data.get("serial", drive_id)
    host = drive_host(coordinator.hass, coordinator.config_entry, drive_id)
    return {
        "identifiers": {(DOMAIN, drive_id)},
        # A drive without a serial is named by its id, "(dev-sda)", as the
        # sync names it from the registry; never "()".
        "name": drive_name(host, model, drive_data.get("serial") or drive_id),
        "manufacturer": manufacturer,
        "model": model,
        "serial_number": serial,
        # Nest this drive under its agent so HA can cascade an area
        # assignment from the agent to every drive it reports (#24).
        **agent_link(coordinator),
    }


def drive_host(hass: HomeAssistant, entry: ConfigEntry, drive_id: str) -> str | None:
    """The host label for a drive device.

    Its own entry's host, except on Home Assistant before 2026.8 when the
    device is shared with other entries: then the least (host, entry_id) of
    the entries it belongs to, read from their stored data, so every entry
    names it the same whichever is set up first. Falls back to its own host
    when the registry cannot be read.
    """
    own = display_host(entry)
    if PER_ENTRY_DEVICES:
        return own
    try:
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, drive_id)})
        owners = set(device.config_entries) if device is not None else set()
    except Exception:  # noqa: BLE001 - no registry (tests) or an older API
        return own
    return _shared_host(hass, entry, owners)


def _shared_host(hass: HomeAssistant, entry: ConfigEntry, owners: set[str]) -> str | None:
    hosts: dict[str, str | None] = {entry.entry_id: display_host(entry)}
    for entry_id in owners - {entry.entry_id}:
        other = hass.config_entries.async_get_entry(entry_id)
        if other is not None and other.domain == DOMAIN:
            hosts[entry_id] = display_host(other)
    return pick_shared_host((host, entry_id) for entry_id, host in hosts.items())


@callback
def async_sync_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring this entry's device names in the registry up to date.

    Never raises: a failure is logged and the names are left as they are,
    so it cannot fail setup or the update listener.
    """
    try:
        _sync_names(hass, entry)
    except Exception:  # noqa: BLE001 - names are cosmetic; never fail setup
        _LOGGER.warning(
            "SMART Sniffer: could not update the device names for %s",
            entry.title, exc_info=True,
        )


def _sync_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = dr.async_get(hass)
    entry_id = entry.entry_id
    host = display_host(entry)
    fallback = legacy_host(entry.title, agent_address(entry.data))
    renamed = 0
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        identifier = our_identifier(device.identifiers)
        if identifier is None:
            continue
        if identifier == f"{entry_id}_agent":
            name = agent_name(entry.title, agent_address(entry.data), host)
        elif identifier == f"{entry_id}_filesystems":
            name = disk_usage_name(host, fallback)
        elif (pool := pool_name_from_identifier(identifier, entry_id)) is not None:
            name = pool_name(host, pool, fallback)
        else:
            if not device.model:
                continue  # not one our device info wrote; leave it alone
            name = drive_name(
                _drive_host_of(hass, entry, device, host),
                device.model,
                device.serial_number or identifier,
            )
        if device.name != name:
            registry.async_update_device(device.id, name=name)
            renamed += 1
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})[NAMED_HOST] = host
    if renamed:
        _LOGGER.debug("SMART Sniffer: %s: %d device name(s) updated for host %s",
                      entry.title, renamed, host)


def _drive_host_of(
    hass: HomeAssistant, entry: ConfigEntry, device: Any, own: str | None
) -> str | None:
    if PER_ENTRY_DEVICES:
        return own
    return _shared_host(hass, entry, set(device.config_entries))
