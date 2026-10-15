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

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from typing import Any

UNIQUE_ID_PREFIX = "smartha-"

# What a config entry keeps about the agent it monitors, written by the
# coordinator from the entry's own polls (identity_changes below). Changing
# them never reloads the entry. The rediscovery move (config_flow) needs the
# host name and the drive digest; agent_id is stored for later releases and
# read by nothing yet.
ENTRY_AGENT_HOSTNAME = "agent_hostname"
ENTRY_AGENT_DRIVES = "agent_drives"
ENTRY_AGENT_ID = "agent_id"
ENTRY_IDENTITY_KEYS = frozenset({ENTRY_AGENT_HOSTNAME, ENTRY_AGENT_DRIVES, ENTRY_AGENT_ID})

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


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


def norm_agent_id(value: Any) -> str:
    """An agent_id from /api/health, or "" when missing or malformed."""
    text = (value if isinstance(value, str) else "").strip().lower()
    return text if _AGENT_ID_RE.match(text) else ""


def drive_digest(drives: Any) -> str:
    """A digest of the drive serials an /api/drives body lists, or "".

    Serials only: an id ("/dev/sda") is the same on every machine. Drives
    without a serial are left out, and a body with no serial at all (or one
    that is not a list of drives) gives "", which never matches anything.
    Stored as a digest so the entry holds no serial numbers.
    """
    if not isinstance(drives, list):
        return ""
    serials = sorted(
        {
            d["serial"].strip()
            for d in drives
            if isinstance(d, dict) and isinstance(d.get("serial"), str) and d["serial"].strip()
        }
    )
    if not serials:
        return ""
    return hashlib.sha256("\n".join(serials).encode()).hexdigest()[:32]


def identity_changes(
    stored: Mapping[str, Any], health: Any, drives: Any
) -> tuple[dict[str, str], bool]:
    """What an entry should store about its agent after one successful poll.

    ``stored`` is the entry's data; ``health`` and ``drives`` the /api/health
    and /api/drives bodies from the entry's own host, read in that poll.
    Returns (changes, other_agent). Nothing is stored without a host name
    (agents before v0.8.3). When the entry already holds a host name and its
    address now answers with another one, the address belongs to another
    agent (DHCP gave it away) or the host was renamed: nothing is replaced,
    so what the entry knew about its own agent is kept, and other_agent is
    True. An empty drive digest or agent_id never replaces a stored one.
    """
    if not isinstance(health, dict):
        return {}, False
    live_name = norm_host(health.get("hostname"))
    if not live_name:
        return {}, False
    known_name = norm_host(stored.get(ENTRY_AGENT_HOSTNAME))
    if known_name and known_name != live_name:
        return {}, True
    wanted = {
        ENTRY_AGENT_HOSTNAME: live_name,
        ENTRY_AGENT_DRIVES: drive_digest(drives),
        ENTRY_AGENT_ID: norm_agent_id(health.get("agent_id")),
    }
    return {k: v for k, v in wanted.items() if v and stored.get(k) != v}, False


# Discovery address preference (lower is better). Python's ipaddress counts
# 169.254/16 as private and 100.64/10 (CGNAT: Tailscale, carrier NAT) as
# neither private nor global, so the ranges are spelled out.
_LAN_NETS = (ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("10.0.0.0/8"))
_CONTAINER_NET = ipaddress.ip_network("172.16.0.0/12")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")


def ip_score(ip_str: str) -> int:
    """How much discovery prefers an advertised address; lower is better.

      10  192.168/16, 10/8      almost always the physical LAN
      50  172.16/12             RFC 1918, often Docker/container bridges
      70  100.64/10             CGNAT: Tailscale, WireGuard, carrier NAT
      80  other private IPv4
      85  IPv6                  unreliable across VLANs at home
      90  public IPv4           a WAN address (smart-sniffer-app#12)
      95  169.254/16            link-local, no DHCP: rarely reachable
      99  not an address
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return 99
    if addr.version == 6:
        return 85
    if any(addr in net for net in _LAN_NETS):
        return 10
    if addr in _CONTAINER_NET:
        return 50
    if addr in _CGNAT_NET:
        return 70
    if addr in _LINK_LOCAL_NET:
        return 95
    if addr.is_private:
        return 80
    return 90
