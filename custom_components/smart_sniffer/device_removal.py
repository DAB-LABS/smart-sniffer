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

A refusal is raised to the user as a translated HomeAssistantError, so the
dialog says why. How much of that reaches the screen depends on which page the
user deleted from; see async_remove_config_entry_device in __init__.py.

Nothing here imports Home Assistant, so the decision can be tested without it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import NamedTuple

from .const import DOMAIN, FILESYSTEMS_KEY

# Translation keys under "exceptions" in strings.json. A refusal is raised as a
# HomeAssistantError carrying one of these, so the user reads the reason in the
# dialog rather than core's generic "rejected by integration".
REMOVE_LIVE_DRIVE = "remove_live_drive"
REMOVE_AGENT_DEVICE = "remove_agent_device"
REMOVE_AGENT_OFFLINE = "remove_agent_offline"
REMOVE_DISK_USAGE = "remove_disk_usage"

REFUSAL_KEYS: frozenset[str] = frozenset(
    {REMOVE_LIVE_DRIVE, REMOVE_AGENT_DEVICE, REMOVE_AGENT_OFFLINE, REMOVE_DISK_USAGE}
)


class Decision(NamedTuple):
    """The outcome for one device.

    ``translation_key`` names the message the user sees when removal is refused,
    and is None when it is allowed or when the device is not this integration's
    at all, in which case core's own message is the honest one.
    """

    allowed: bool
    reason: str
    translation_key: str | None


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
) -> Decision:
    """Whether this device may be deleted, and why.

    ``agent_reporting`` is false when the last poll failed or none has completed
    yet, in which case ``drive_ids`` says nothing about what exists.

    The reason is returned rather than logged here so that the caller decides
    how loud to be, and so the tests can assert on it.
    """
    identifier = our_identifier(identifiers)
    if identifier is None:
        return Decision(False, "the device does not belong to this integration", None)

    if identifier == f"{entry_id}_agent":
        return Decision(
            False, "the agent device goes away with its config entry", REMOVE_AGENT_DEVICE
        )

    if not agent_reporting or drive_ids is None or filesystem_count is None:
        return Decision(
            False,
            "the agent is not reporting, so nothing can be called stale",
            REMOVE_AGENT_OFFLINE,
        )

    if identifier == f"{entry_id}_filesystems":
        if filesystem_count:
            return Decision(
                False,
                f"the agent still reports {filesystem_count} filesystem(s)",
                REMOVE_DISK_USAGE,
            )
        return Decision(True, "the agent reports no filesystems", None)

    if identifier in drive_ids:
        return Decision(False, "the agent still reports this drive", REMOVE_LIVE_DRIVE)

    return Decision(True, "the agent no longer reports this drive", None)


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
