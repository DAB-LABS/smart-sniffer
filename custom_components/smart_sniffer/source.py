"""Where an entry's agent data comes from (v0.9): an agent over HTTP, or the
Replica machine inside the integration.

A field agent's or the HA App's entry holds the agent's address (host,
port, token); its coordinators read it over HTTP. The Replica entry holds
``{"kind": "replica", "host": "replica.invalid", "port": 9099, ...}``: the
placeholder host is a ``.invalid`` name that never resolves (RFC 6761), so
an integration rolled back to v0.8.5, which reads ``entry.data["host"]``,
finds a host, fails its first poll on the name, and retries quietly while
every other entry works. v0.9 never uses it: every read of an entry's
address goes through agent_address() and agent_port() here, which give
None for the Replica (tests/test_replica_under_ha.py greps for any other).

HA-free: host_name.py and entity_plan.py use it too.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT

ENTRY_KIND = "kind"
KIND_REPLICA = "replica"
REPLICA_HOST = "replica.invalid"
REPLICA_UNIQUE_ID = "replica"
REPLICA_TITLE = "SMART Sniffer (Replica)"
REPLICA_LABEL = "Replica"


def is_replica_data(data: Mapping[str, Any] | None) -> bool:
    """Whether an entry's data is the Replica machine's."""
    return isinstance(data, Mapping) and data.get(ENTRY_KIND) == KIND_REPLICA


def agent_address(data: Mapping[str, Any] | None) -> str | None:
    """The agent's host as the entry stores it; None for the Replica, which
    has no address."""
    if not isinstance(data, Mapping) or is_replica_data(data):
        return None
    host = data.get(CONF_HOST)
    return str(host) if host not in (None, "") else None


def agent_port(data: Mapping[str, Any] | None) -> int | None:
    """The agent's port; None for the Replica."""
    if not isinstance(data, Mapping) or is_replica_data(data):
        return None
    try:
        return int(data.get(CONF_PORT, DEFAULT_PORT))
    except (TypeError, ValueError):
        return None


def replica_entry_data(agent_id: str, scan_interval: int) -> dict[str, Any]:
    """A new Replica entry's data."""
    return {ENTRY_KIND: KIND_REPLICA, CONF_HOST: REPLICA_HOST, CONF_PORT: DEFAULT_PORT,
            "token": "", "scan_interval": scan_interval, "agent_id": agent_id}
