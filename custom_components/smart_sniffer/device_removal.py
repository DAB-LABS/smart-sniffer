"""Which of this integration's devices a user may delete.

Home Assistant offers a Delete button on a device only when the integration
defines ``async_remove_config_entry_device``, and it offers it on *every* device
of that config entry. There is no per-device gating in the frontend: the
integration is asked at the moment the user confirms, and returning False turns
into a generic "rejected by integration" error. So the reasons live here and get
logged, because the user never sees them.

The rules, in order:

1. The agent device is never removable while its config entry exists. Removing
   the entry is how a user removes an agent, and deleting the parent out from
   under its drives would only orphan them.
2. Nothing is removable while the agent is unreachable or has not reported yet.
   An offline agent and a drive that has genuinely gone away look identical from
   here, and deleting a real drive because its agent was rebooting is the
   failure worth avoiding.
3. A drive device is removable when the agent does not currently report its
   identifier. Devices from old identifier schemes, such as ``dev-sda``, fall
   out of this without naming them: the agent never reports those ids.
4. The Disk Usage device is removable only when the agent reports no
   filesystems at all.

A device deleted while its hardware is still present comes back on the next
poll, because the platforms register it again from the agent's data.

Nothing here imports Home Assistant, so the decision can be tested without it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from .const import DOMAIN, FILESYSTEMS_KEY


def our_identifier(
    identifiers: Iterable[tuple[str, str]],
) -> str | None:
    """This integration's identifier for a device, if it has one."""
    for domain, identifier in identifiers:
        if domain == DOMAIN:
            return identifier
    return None


def removable(
    identifiers: Iterable[tuple[str, str]],
    entry_id: str,
    drive_ids: Collection[str] | None,
    filesystem_count: int | None,
    agent_reporting: bool,
) -> tuple[bool, str]:
    """Whether this device may be deleted, and why.

    ``agent_reporting`` is false when the last poll failed or none has completed
    yet, in which case ``drive_ids`` says nothing about what exists.

    The reason is returned rather than logged here so that the caller decides
    how loud to be, and so the tests can assert on it.
    """
    identifier = our_identifier(identifiers)
    if identifier is None:
        return False, "the device does not belong to this integration"

    if identifier == f"{entry_id}_agent":
        return False, "the agent device goes away with its config entry"

    if not agent_reporting or drive_ids is None or filesystem_count is None:
        return False, "the agent is not reporting, so nothing can be called stale"

    if identifier == f"{entry_id}_filesystems":
        if filesystem_count:
            return False, f"the agent still reports {filesystem_count} filesystem(s)"
        return True, "the agent reports no filesystems"

    if identifier in drive_ids:
        return False, "the agent still reports this drive"

    return True, "the agent no longer reports this drive"


def reported_drive_ids(coordinator_data: dict | None) -> list[str] | None:
    """Drive ids in a coordinator payload, without its internal keys.

    ``None`` when there is no payload at all, which is not the same as an agent
    that reports no drives.
    """
    if coordinator_data is None:
        return None
    return [key for key in coordinator_data if not key.startswith("_")]


def reported_filesystem_count(coordinator_data: dict | None) -> int | None:
    """How many filesystems the agent reports, or None when it has not reported."""
    if coordinator_data is None:
        return None
    return len(coordinator_data.get(FILESYSTEMS_KEY) or [])
