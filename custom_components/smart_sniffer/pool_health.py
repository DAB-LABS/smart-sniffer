"""ZFS pool health: what the agent's /api/pools payload means (GH #50).

The agent reports, per pool, what ``zpool status`` printed: the state, the
read/write/checksum error totals, the data error count behind the "errors:"
line, the status and action text, and the last scrub. This module decides what
those mean for Home Assistant, so the sensors, the notification and the device
removal rules all read them the same way.

A pool is unhealthy when any of these hold:

  - its state is not ONLINE;
  - any of its read, write or checksum totals is above zero;
  - it has data errors ("errors:" is not "No known data errors");
  - the agent lists a problem vdev (a disk or group that is not ONLINE or
    has errors, or a spare that is neither AVAIL nor INUSE).

The reasons name the problem vdevs, one line each, after the pool-level lines.

A pool is missing when its device is in the device registry, the agent
advertises pools, and the last pool fetch succeeded without listing it: the
pool was exported, or failed to import. A failed fetch or an offline agent is
"unknown", never missing.

The ``status`` and ``action`` text never count. Every pool on a host that has
not run ``zpool upgrade`` says "Some supported and requested features are not
enabled on the pool", which is advice, not a fault.

Nothing here imports Home Assistant, so the rules can be tested without it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any, NamedTuple

from .const import DOMAIN, POOLS_KEY, POOLS_MISSING_KEY

# Pool states as zpool prints them. From zpool_get_state_str() and
# zpool_state_to_name() in OpenZFS lib/libzfs/libzfs_pool.c (zfs-2.3.4):
# FAULTED for an unavailable pool, SUSPENDED for I/O failure, otherwise the
# root vdev's state, which can also read SPLIT or UNKNOWN. Anything else an
# agent sends is shown as UNKNOWN, because an enum sensor rejects a value
# outside its options.
STATE_ONLINE = "ONLINE"
STATE_UNKNOWN = "UNKNOWN"
# Not a zpool state: the integration's own, for a pool the agent no longer
# lists (see the module docstring). The agent never sends it.
STATE_MISSING = "MISSING"
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
    STATE_MISSING,
]

# The one reason a missing pool has.
MISSING_REASON = "Not reported by zpool"

# Device lines a notification shows before "and N more". The Problem sensor's
# attributes always carry the full list.
NOTIFICATION_DEVICE_LINES = 10

# The agent's endpoint, as /api/health lists it.
POOLS_ENDPOINT = "/api/pools"

# The three error totals, in the order they are shown.
ERROR_KEYS: tuple[str, ...] = ("read_errors", "write_errors", "checksum_errors")

_ERROR_LABELS: dict[str, str] = {
    "read_errors": "Read errors",
    "write_errors": "Write errors",
    "checksum_errors": "Checksum errors",
}

# The same counters as a device line words them ("18 read errors").
_DEVICE_ERROR_WORDS: dict[str, str] = {
    "read_errors": "read error",
    "write_errors": "write error",
    "checksum_errors": "checksum error",
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


def missing_pools(
    advertised: bool,
    pools: list[dict[str, Any]] | None,
    registered: Iterable[str],
) -> list[str] | None:
    """Registered pools the agent's pool list left out, sorted.

    None when the list could not be read (pools unknown, so none is missing);
    [] when the agent does not advertise pools at all, because an agent with
    pool status turned off, or an older one, says nothing about whether a pool
    is imported.
    """
    if pools is None:
        return None
    if not advertised:
        return []
    listed = {pool.get("name") for pool in pools}
    return sorted({name for name in registered if name not in listed})


def reported_missing(coordinator_data: dict[str, Any] | None) -> list[str]:
    """The missing pools in a coordinator payload; [] when none or unknown."""
    if not coordinator_data:
        return []
    return list(coordinator_data.get(POOLS_MISSING_KEY) or [])


def is_missing(coordinator_data: dict[str, Any] | None, name: str) -> bool:
    return name in reported_missing(coordinator_data)


def pool_names_for_setup(
    coordinator_data: dict[str, Any] | None, registered: Iterable[str]
) -> list[str]:
    """Pools to create entities for at setup: every pool in the payload and
    every pool device already registered, so a pool that failed to import
    before Home Assistant started still has its entities."""
    names = {pool["name"] for pool in reported_pools(coordinator_data) or []}
    names.update(registered)
    return sorted(names)


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


def registered_pool_names(
    device_identifiers: Iterable[Iterable[tuple[str, str]]], entry_id: str
) -> list[str]:
    """Pool names among the identifiers of this entry's registered devices."""
    names: set[str] = set()
    for identifiers in device_identifiers:
        for domain, identifier in identifiers:
            if domain != DOMAIN:
                continue
            name = pool_name_from_identifier(identifier, entry_id)
            if name is not None:
                names.add(name)
    return sorted(names)


def notification_id(entry_id: str, name: str) -> str:
    return f"{_NOTIF_PREFIX}{entry_id}_{name}"


# ---------------------------------------------------------------------------
# Values for the entities
# ---------------------------------------------------------------------------


def state_option(pool: dict[str, Any]) -> str:
    """The pool state as one of POOL_STATES. MISSING is never taken from the
    payload: only the integration decides a pool is missing."""
    state = pool.get("state")
    return state if state in POOL_STATES and state != STATE_MISSING else STATE_UNKNOWN


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


def problem_devices(pool: dict[str, Any]) -> list[dict[str, Any]]:
    """The agent's problem vdevs for this pool; [] from an agent without them."""
    vdevs = pool.get("problem_vdevs")
    if not isinstance(vdevs, list):
        return []
    return [v for v in vdevs if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]]


def device_line(vdev: dict[str, Any]) -> str | None:
    """One problem vdev as a line: "sdb: FAULTED, 18 read errors"."""
    parts: list[str] = []
    state = vdev.get("state")
    if isinstance(state, str) and state and state != STATE_ONLINE:
        parts.append(state)
    for key, word in _DEVICE_ERROR_WORDS.items():
        count = error_total(vdev, key)
        if count:
            parts.append(f"{count} {word}{'' if count == 1 else 's'}")
    if not parts:
        return None
    return f"{vdev['name']}: {', '.join(parts)}"


def device_problems(pool: dict[str, Any]) -> list[str]:
    """A line per problem vdev, in the agent's order."""
    lines = (device_line(v) for v in problem_devices(pool))
    return [line for line in lines if line]


def pool_problems(pool: dict[str, Any]) -> list[str]:
    """Why this pool is unhealthy, one plain line per reason; [] when healthy.

    Pool-level lines first, worded like the drive attention reasons
    ("Label: value"), then a line per problem vdev. The status and action text
    are deliberately not consulted.
    """
    return pool_level_problems(pool) + device_problems(pool)


def pool_level_problems(pool: dict[str, Any]) -> list[str]:
    """The pool-level lines of pool_problems."""
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


def notification_pool_lines(pool: dict[str, Any]) -> list[str]:
    """The pool-level lines as a notification shows them: no count twice.

    zpool totals each error counter up the tree, so a pool whose only
    erroring disk has 18 read errors also reports 18 at pool level, and the
    notification said it twice. A pool-level Read, Write or Checksum line is
    dropped when exactly one problem device has a non-zero count for that
    counter and it equals the pool total; the device line says it. Otherwise
    it stays, and the State and Data errors lines always stay. Only the
    notification text: the reasons, the Problem sensor and the change
    detection keep every line.
    """
    devices = problem_devices(pool)
    lines: list[str] = []
    state = pool.get("state")
    if state != STATE_ONLINE:
        lines.append(f"State: {state or STATE_UNKNOWN}")
    for key in ERROR_KEYS:
        value = error_total(pool, key)
        if not value:
            continue
        counts = [c for c in (error_total(v, key) for v in devices) if c]
        if len(counts) == 1 and counts[0] == value:
            continue
        lines.append(f"{_ERROR_LABELS[key]}: {value}")
    errors = data_errors(pool)
    if errors:
        lines.append(f"Data errors: {errors}")
    return lines


def is_unhealthy(pool: dict[str, Any]) -> bool:
    return bool(pool_problems(pool))


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class PoolAction(NamedTuple):
    """One thing to do with a pool's notification after a poll."""

    kind: str  # "create" (raise or update), "missing", or "dismiss"
    name: str
    reasons: list[str]
    # For "create": the pool-level lines as the notification shows them (see
    # notification_pool_lines) and the device lines of reasons.
    pool_lines: list[str] = []
    device_lines: list[str] = []


def build_notification(
    name: str,
    host: str,
    pool_lines: Iterable[str],
    device_lines: Iterable[str] = (),
) -> tuple[str, str]:
    """(title, message) for a pool that needs attention. Plain, no emoji.

    Pool-level lines, then one line per problem device, at most
    NOTIFICATION_DEVICE_LINES of them and then "and N more".
    """
    devices = list(device_lines)
    shown = devices[:NOTIFICATION_DEVICE_LINES]
    lines = list(pool_lines) + shown
    if len(devices) > len(shown):
        lines.append(f"and {len(devices) - len(shown)} more")
    title = f"ZFS pool {name} on {host} needs attention"
    bullets = "\n".join(f"- {line}" for line in lines)
    message = f"{bullets}\n\nRun `zpool status -v {name}` on {host} for details."
    return title, message


def build_missing_notification(name: str, host: str) -> tuple[str, str]:
    """(title, message) for a pool the agent no longer reports."""
    title = f"ZFS pool {name} on {host} is missing"
    message = (
        f"The agent on {host} no longer reports this pool. It may have been "
        "exported, or failed to import.\n\n"
        f'Run "zpool import" on {host} to see pools that can be imported.'
    )
    return title, message


def notification_actions(
    previous: dict[str, list[str]],
    pools: list[dict[str, Any]] | None,
    missing: Iterable[str] | None = (),
) -> tuple[list[PoolAction], dict[str, list[str]]]:
    """What to do with notifications after a poll, and the state to keep.

    Mirrors the drive attention rules in coordinator.py: the first sighting of
    a listed pool is a baseline and raises nothing; healthy to unhealthy
    creates a notification; a change in the reasons (including the problem
    devices) updates it under the same id; unhealthy to healthy dismisses it;
    a pool that is neither listed nor missing has its state forgotten, and its
    notification dismissed if it had one. Nothing changes from one poll to the
    next, nothing is done.

    A missing pool raises its own notification, under the same id, even on
    the first poll: a pool that failed to import before Home Assistant started
    is the case most worth hearing about. When it comes back the usual rules
    apply: healthy dismisses, unhealthy updates.

    ``pools`` None means this poll could not tell (the fetch failed), so the
    state is kept as it was and nothing is done.

    Returns (actions, new_previous).
    """
    if pools is None:
        return [], dict(previous)

    actions: list[PoolAction] = []
    current: dict[str, list[str]] = {}
    for pool in pools:
        name = pool.get("name")
        if not isinstance(name, str) or not name:
            continue
        pool_lines, device_lines = pool_level_problems(pool), device_problems(pool)
        reasons = pool_lines + device_lines
        current[name] = reasons
        if name not in previous:
            continue  # baseline
        before = previous[name]
        if sorted(reasons) == sorted(before):
            continue
        if reasons:
            actions.append(
                PoolAction("create", name, reasons, notification_pool_lines(pool), device_lines)
            )
        else:
            actions.append(PoolAction("dismiss", name, before))

    for name in missing or ():
        if name in current:
            continue
        reasons = [MISSING_REASON]
        current[name] = reasons
        if previous.get(name) != reasons:
            actions.append(PoolAction("missing", name, reasons))

    for name, before in previous.items():
        if name not in current and before:
            actions.append(PoolAction("dismiss", name, before))

    return actions, current

