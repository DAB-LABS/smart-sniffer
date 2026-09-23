"""How a drive or filesystem device points at the agent it belongs to.

Home Assistant nests one device under another through the device info a platform
returns. Until 2026.8 the only way to express that was ``via_device``, a
``(domain, identifier)`` tuple that the registry resolves by lookup. That lookup
is ambiguous, because an identifier is not unique across config entries, so
2026.8 added ``via_device_id``, which is the registry id of an already
registered device. From 2026.9 passing ``via_device`` logs a deprecation, and
core removes it in 2027.8.0.

This integration declares a floor of 2024.4.0, where ``DeviceInfo`` has no
``via_device_id`` key at all, and passing an unknown key makes the whole device
info invalid. So the link has to follow whatever the installed core supports.

That is decided by looking for the key on ``DeviceInfo`` itself rather than by
comparing version numbers: the key is the capability, and a version comparison
would be a second copy of the same fact, wrong the moment a backport or a fork
disagrees with it.

Nothing here imports Home Assistant at module level, so the pure decision can be
tested without it; ``agent_link`` imports ``DeviceInfo`` when called.
"""

from __future__ import annotations

from typing import Any

from .const import AGENT_DEVICE_ID, DOMAIN


def supports_via_device_id(device_info_cls: Any) -> bool:
    """Whether this Home Assistant's DeviceInfo accepts via_device_id.

    DeviceInfo is a TypedDict. Its keys are read from __required_keys__ and
    __optional_keys__, falling back to __annotations__, because which of those a
    TypedDict exposes has moved around between Python versions.
    """
    keys: set[str] = set()
    for attribute in ("__required_keys__", "__optional_keys__", "__annotations__"):
        try:
            keys |= set(getattr(device_info_cls, attribute, ()) or ())
        except Exception:  # noqa: BLE001 - a lazily evaluated annotation may raise
            continue
    return "via_device_id" in keys


def link_for(
    supported: bool,
    entry_id: str,
    agent_device_id: str | None,
) -> dict[str, Any]:
    """The via-link to merge into a device info dict.

    Returns exactly one key. Core rejects device info carrying both, and an
    agent device id that is not in the registry is an error there too, so the
    old form is also the fallback when the id is missing.
    """
    if supported and agent_device_id:
        return {"via_device_id": agent_device_id}
    return {"via_device": (DOMAIN, f"{entry_id}_agent")}


def agent_link(coordinator: Any) -> dict[str, Any]:
    """The via-link for a device that belongs to this entry's agent.

    The agent device is registered in async_setup_entry before any platform
    loads, and its registry id is kept in hass.data next to the coordinators.
    """
    from homeassistant.helpers.device_registry import (  # noqa: PLC0415
        DeviceInfo,  # imported here so this module stays importable without HA
    )

    entry_id = coordinator.config_entry.entry_id
    entry_data = (coordinator.hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    return link_for(
        supports_via_device_id(DeviceInfo),
        entry_id,
        entry_data.get(AGENT_DEVICE_ID),
    )
