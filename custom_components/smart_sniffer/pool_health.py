"""ZFS pool health: what the agent's /api/pools payload means (GH #50).

The agent reports, per pool, what ``zpool status`` printed: the state, the
read/write/checksum error totals, the data error count behind the "errors:"
line, the status and action text, and the last scrub. This module decides what
those mean for Home Assistant, so the sensors, the notification and the device
removal rules all read them the same way.

A pool is unhealthy when any of these hold:

  - its state is not ONLINE;
  - any of its read, write or checksum totals is above zero;
  - it has data errors ("errors:" is not "No known data errors").

The ``status`` and ``action`` text never count. Every pool on a host that has
not run ``zpool upgrade`` says "Some supported and requested features are not
enabled on the pool", which is advice, not a fault.

Nothing here imports Home Assistant, so the rules can be tested without it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from .const import POOLS_KEY

# Pool states as zpool prints them. From zpool_get_state_str() and
# zpool_state_to_name() in OpenZFS lib/libzfs/libzfs_pool.c (zfs-2.3.4):
# FAULTED for an unavailable pool, SUSPENDED for I/O failure, otherwise the
# root vdev's state, which can also read SPLIT or UNKNOWN. Anything else an
# agent sends is shown as UNKNOWN, because an enum sensor rejects a value
# outside its options.
STATE_ONLINE = "ONLINE"
STATE_UNKNOWN = "UNKNOWN"
POOL_STATES: list[str] = [
    STATE_ONLINE,
    "DEGRADED",
    "FAULTED",
    "OFFLINE",
    "UNAVAIL",
    "REMOVED",
    "SUSPENDED",
    "SPLIT",
    STATE_UNKNOWN,
]

# The agent's endpoint, as /api/health lists it.
POOLS_ENDPOINT = "/api/pools"

# The three error totals, in the order they are shown.
ERROR_KEYS: tuple[str, ...] = ("read_errors", "write_errors", "checksum_errors")

_ERROR_LABELS: dict[str, str] = {
    "read_errors": "Read errors",
    "write_errors": "Write errors",
    "checksum_errors": "Checksum errors",
}

_NOTIF_PREFIX = "smart_sniffer_pool_"


# ---------------------------------------------------------------------------
# Reading the payload
# ---------------------------------------------------------------------------


def advertises_pools(health: dict[str, Any] | None) -> bool:
    """Whether the agent's /api/health lists the pools endpoint.

    Agents before this feature, and agents with pool status off or no zpool,
    do not list it, and are never asked.
    """
    if not isinstance(health, dict):
        return False
    endpoints = health.get("endpoints")
    return isinstance(endpoints, list) and POOLS_ENDPOINT in endpoints


async def fetch_pools(
    health: dict[str, Any] | None,
    get_json: Callable[[str], Awaitable[Any]],
) -> list[dict[str, Any]] | None:
    """The pools to store for this poll.

    ``[]`` when the agent does not advertise pools. ``None`` when it does but
    the fetch failed (``get_json`` returns None) or the payload is not a list:
    "could not tell" must not read as "no pools", or a passing network error
    would look like every pool vanishing.
    """
    if not advertises_pools(health):
        return []
    payload = await get_json(POOLS_ENDPOINT)
    if not isinstance(payload, list):
        return None
    return [pool for pool in payload if isinstance(pool, dict) and pool.get("name")]


def reported_pools(coordinator_data: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The pools in a coordinator payload, or None when they are not known."""
    if coordinator_data is None:
        return None
    if POOLS_KEY not in coordinator_data:
        return []
    return coordinator_data[POOLS_KEY]


def find_pool(coordinator_data: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    """One pool by name, or None when it is not reported."""
    for pool in reported_pools(coordinator_data) or []:
        if pool.get("name") == name:
            return pool
    return None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def pool_identifier(entry_id: str, name: str) -> str:
    """The device registry identifier for one pool on one agent.

    Keyed on the config entry so two hosts with an "rpool" each get their own
    device, and on the pool name, so a renamed pool is a new device.
    """
    return f"{entry_id}_zpool_{name}"


def pool_name_from_identifier(identifier: str, entry_id: str) -> str | None:
    """The pool name in one of our identifiers, or None if it is not a pool."""
    prefix = f"{entry_id}_zpool_"
    if identifier.startswith(prefix) and len(identifier) > len(prefix):
        return identifier[len(prefix):]
    return None


def notification_id(entry_id: str, name: str) -> str:
    return f"{_NOTIF_PREFIX}{entry_id}_{name}"


# ---------------------------------------------------------------------------
# Values for the entities
# ---------------------------------------------------------------------------


def state_option(pool: dict[str, Any]) -> str:
    """The pool state as one of POOL_STATES."""
    state = pool.get("state")
    return state if state in POOL_STATES else STATE_UNKNOWN


def error_total(pool: dict[str, Any], key: str) -> int | None:
    value = pool.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def data_errors(pool: dict[str, Any]) -> int | None:
    """The count behind the "errors:" line, None when zpool printed none."""
    return error_total(pool, "data_errors")


def last_scrub_end(pool: dict[str, Any]) -> datetime | None:
    """When the last completed scrub ended, timezone-aware, or None."""
    raw = pool.get("last_scrub_end")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # fromisoformat reads a trailing Z only from Python 3.11.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def scrub_attributes(pool: dict[str, Any]) -> dict[str, Any]:
    """Attributes of the Last Scrub sensor."""
    return {
        "scrub_errors": pool.get("last_scrub_errors"),
        "repaired_bytes": pool.get("last_scrub_repaired"),
        "scrub_in_progress": bool(pool.get("scrub_in_progress")),
        "scan_function": pool.get("scan_function"),
        "scan_state": pool.get("scan_state"),
    }


# ---------------------------------------------------------------------------
# The health rule
# ---------------------------------------------------------------------------


def pool_problems(pool: dict[str, Any]) -> list[str]:
    """Why this pool is unhealthy, one plain line per reason; [] when healthy.

    Worded like the drive attention reasons ("Label: value"). The status and
    action text are deliberately not consulted.
    """
    reasons: list[str] = []
    state = pool.get("state")
    if state != STATE_ONLINE:
        reasons.append(f"State: {state or STATE_UNKNOWN}")
    for key in ERROR_KEYS:
        value = error_total(pool, key)
        if value:
            reasons.append(f"{_ERROR_LABELS[key]}: {value}")
    errors = data_errors(pool)
    if errors:
        reasons.append(f"Data errors: {errors}")
    return reasons


def is_unhealthy(pool: dict[str, Any]) -> bool:
    return bool(pool_problems(pool))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def build_notification(name: str, host: str, reasons: Iterable[str]) -> tuple[str, str]:
    """(title, message) for a pool that needs attention. Plain, no emoji."""
    title = f"ZFS pool {name} on {host} needs attention"
    bullets = "\n".join(f"- {reason}" for reason in reasons)
    message = f"{bullets}\n\nRun `zpool status -v {name}` on {host} for details."
    return title, message


def notification_actions(
    previous: dict[str, list[str]],
    pools: list[dict[str, Any]] | None,
) -> tuple[list[tuple[str, str, list[str]]], dict[str, list[str]]]:
    """What to do with notifications after a poll, and the state to keep.

    Mirrors the drive attention rules in coordinator.py: the first sighting of
    a pool is a baseline and raises nothing; healthy to unhealthy creates a
    notification; a change in the reasons updates it under the same id;
    unhealthy to healthy dismisses it; a pool that is no longer reported has
    its state forgotten, and its notification dismissed if it had one. Nothing changes from
    one poll to the next, nothing is done.

    ``pools`` None means this poll could not tell (the fetch failed), so the
    state is kept as it was and nothing is done.

    Returns (actions, new_previous), each action ("create" | "dismiss", pool
    name, reasons).
    """
    if pools is None:
        return [], dict(previous)

    actions: list[tuple[str, str, list[str]]] = []
    current: dict[str, list[str]] = {}
    for pool in pools:
        name = pool.get("name")
        if not isinstance(name, str) or not name:
            continue
        reasons = pool_problems(pool)
        current[name] = reasons
        if name not in previous:
            continue  # baseline
        before = previous[name]
        if sorted(reasons) == sorted(before):
            continue
        if reasons:
            actions.append(("create", name, reasons))
        else:
            actions.append(("dismiss", name, before))

    for name, before in previous.items():
        if name not in current and before:
            actions.append(("dismiss", name, before))

    return actions, current

