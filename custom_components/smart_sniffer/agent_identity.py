"""Is a discovered or typed-in agent one that already has a config entry?

An agent can be added twice: once by hand (or by the Control Center, which
uses the same user step) under the address it was given, and once through
zeroconf under the address it advertises. The two differ when the agent
advertises the wrong interface (smart-sniffer-app#12: an OpenWrt router
advertising its WAN address) or runs in a container (the App: entered as the
container's host name, advertised on the container network). A second entry
for the same agent gives every drive entity a twin with an entity_id ending
in _2 (GH #64).

A hand-added entry's unique_id is "{host}:{port}" and a discovered one's
"smartha-{hostname}", so unique_id alone cannot see the duplicate. Neither
can the host name alone: host names collide (OpenWrt's default is
"OpenWrt", and two such routers must stay two entries). So the rule here
needs the agents themselves to agree:

  1. the same port;
  2. the existing entry's host answers /api/health with the same host name
     (agents v0.8.3 and later report it);
  3. the same drives: the serials (or ids) from /api/drives at both
     addresses, non-empty and equal. When the new address cannot be read
     (unreachable, or it wants a token), the drive count the agent
     advertises in its TXT record must equal the existing drive count.

Anything unreadable on the existing side means "not shown to be the same",
and the discovery is offered as before.

Nothing here imports Home Assistant, so the rules can be tested without it.
"""

from __future__ import annotations

import ipaddress
from typing import Any

UNIQUE_ID_PREFIX = "smartha-"


def norm_host(name: Any) -> str:
    """A host name for comparison: lower case, no trailing dot or .local."""
    text = (name if isinstance(name, str) else "").strip().lower().rstrip(".")
    if text.endswith(".local"):
        text = text[: -len(".local")]
    return text


def is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def drive_set(drives: Any) -> frozenset[str] | None:
    """The drives an /api/drives body lists, by serial (else id), or None
    when the body is not a list of drives."""
    if not isinstance(drives, list):
        return None
    found: set[str] = set()
    for drive in drives:
        if not isinstance(drive, dict):
            return None
        serial = drive.get("serial")
        ident = serial if isinstance(serial, str) and serial else drive.get("id")
        if not isinstance(ident, str) or not ident:
            return None
        found.add(ident)
    return frozenset(found)


def confirms_same_agent(
    hostname: str,
    existing_health: Any,
    existing_drives: Any,
    new_drives: Any,
    advertised_drive_count: Any = None,
) -> bool:
    """True only when the existing entry's agent is shown to be this one.

    ``existing_health`` and ``existing_drives`` are the /api/health and
    /api/drives bodies from the existing entry's host (None when it could not
    be read); ``new_drives`` the /api/drives body from the new address (None
    when it could not be read); ``advertised_drive_count`` the TXT "drives"
    value, used only when ``new_drives`` is None.
    """
    name = norm_host(hostname)
    if not name or not isinstance(existing_health, dict):
        return False
    if norm_host(existing_health.get("hostname")) != name:
        return False
    existing = drive_set(existing_drives)
    if not existing:
        return False
    new = drive_set(new_drives)
    if new is not None:
        return new == existing
    try:
        count = int(advertised_drive_count)
    except (TypeError, ValueError):
        return False
    return count == len(existing)
