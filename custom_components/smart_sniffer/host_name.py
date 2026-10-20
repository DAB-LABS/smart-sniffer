"""The host part of every SMART Sniffer device name (GH #50).

Two machines often have drives of the same model, a pool of the same name
(rpool on every Proxmox box) and a Disk Usage device each, so every device
name starts with the host it belongs to: "nas1 Samsung 870 EVO (S5Y...)",
"nas1 ZFS pool rpool", "nas1 Disk Usage", and "SMART Sniffer (nas1)" for the
agent.

The host is read from the config entry only, never from a poll, so every
writer (the entities' device info, the name sync, another entry sharing a
drive) gets the same answer. In order:

  1. the HA App's agents: "HAOS" for the real agent and "Replica" for the
     replica agent, which both report the container's host name;
  2. the host name the agent reports in /api/health (v0.8.3 and later),
     which the coordinator keeps on the entry (agent_identity);
  3. for an entry discovery made, the name discovery found: the host in its
     "SMART Sniffer (<name>)" title, so an older agent keeps the name it
     had (ZimaOS, brookdale) until it reports one itself;
  4. the address the entry was set up with, without a port.

Names keep the case the agent or the user gave them (LAPTOP-2ONH2TAS); only
comparisons ignore case.

The host is a label only. Device identifiers and entity unique_ids do not
contain it, so a new host name renames devices and nothing else.

Nothing here imports Home Assistant, so the rules can be tested without it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .agent_identity import ENTRY_AGENT_HOSTNAME, ENTRY_AGENT_MOCK, display_hostname, is_ip
from .source import REPLICA_LABEL, agent_address, is_replica_data

# The container host name of the HA App: "<8 hex repo hash>-smart-sniffer-agent",
# or "local-smart-sniffer-agent" for a local build, with an optional
# "-<suffix>" for an App built under another slug (a local dev build:
# "local-smart-sniffer-agent-dev"). The real agent and the replica agent in
# the App both report it.
APP_HOST = re.compile(r"^(?:[0-9a-f]{8}|local)-smart-sniffer-agent(?:-[a-z0-9][a-z0-9-]{0,30})?$")

# An entry made from the Supervisor's discovery (config_entries.SOURCE_HASSIO)
# is the HA App's agent: the App posts it, and its replica agent never does.
SOURCE_HASSIO = "hassio"
# What the Supervisor's discovery said about the App, kept on the entry the
# discovery made: the App's slug.
ENTRY_APP_SLUG = "app_slug"
APP_AGENT_LABEL = "HAOS"
APP_REPLICA_LABEL = "Replica"

# The title the config flow gives an entry: "SMART Sniffer (host:port)" by
# hand, "SMART Sniffer (hostname)" by discovery. Any other title is the
# user's own.
_DEFAULT_TITLE = re.compile(r"^SMART Sniffer \((.*)\)$")

# A container's default host name is its 12 hex digit id, which changes with
# every new container.
_DOCKER_ID = re.compile(r"^[0-9a-f]{12}$")

_PORT_SUFFIX = re.compile(r"^(.*):(\d+)$")

# The config entry source of an entry discovery made (config_entries.
# SOURCE_ZEROCONF), spelled out so this module needs no Home Assistant.
SOURCE_ZEROCONF = "zeroconf"


def _is_app_host(name: str | None) -> bool:
    return bool(name) and APP_HOST.match(name.lower()) is not None


def clean_host(raw: Any) -> str | None:
    """A host name an agent reported, as a label: its first DNS label, in
    the case it was given.

    None for anything that names no particular machine: blank, "localhost",
    or a container's 12 hex digit id.
    """
    text = display_hostname(raw)
    if not text:
        return None
    if not is_ip(text):
        text = text.split(".")[0]
    if not text or text.lower() == "localhost" or _DOCKER_ID.match(text.lower()):
        return None
    return text


def address_label(raw: Any) -> str | None:
    """The address an entry was set up with, as a label: no port, no IPv6
    brackets, no trailing dot. A DNS name is kept whole, as it was typed."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        match = _PORT_SUFFIX.match(text)
        if match:
            text = match.group(1)
    text = text.rstrip(".")
    return text or None


def legacy_host(title: Any, conf_host: Any) -> str:
    """The host as v0.8.3 showed it: the title's text in "SMART Sniffer
    (...)", else the address. Still used where the user must find the
    entry (the agent update repair, the device removal refusal)."""
    match = _DEFAULT_TITLE.match(title if isinstance(title, str) else "")
    if match:
        return match.group(1)
    return str(conf_host or "")


def _title_text(title: Any) -> str | None:
    """The host in a default title, without a port."""
    match = _DEFAULT_TITLE.match(title if isinstance(title, str) else "")
    if not match:
        return None
    return address_label(match.group(1))


def is_app_entry(data: Mapping[str, Any], title: Any, live_hostname: Any = None,
                 source: Any = None) -> bool:
    """Whether an entry is one of the HA App's agents: made from the
    Supervisor's discovery (its source, or the App facts it keeps), or its
    stored or live host name, title or address is the App's container name."""
    if source == SOURCE_HASSIO or data.get(ENTRY_APP_SLUG):
        return True
    names = (
        clean_host(data.get(ENTRY_AGENT_HOSTNAME)),
        clean_host(live_hostname),
        _title_text(title),
        address_label(agent_address(data)),
    )
    return any(_is_app_host(name) for name in names)


def app_names(data: Mapping[str, Any], title: Any) -> set[str]:
    """The HA App container names an entry carries (stored host name, title,
    address), lower case: which App's agent an App entry is."""
    names = (
        clean_host(data.get(ENTRY_AGENT_HOSTNAME)),
        _title_text(title),
        address_label(agent_address(data)),
    )
    return {name.lower() for name in names if _is_app_host(name)}


def discovered_name(title: Any, source: Any) -> str | None:
    """The name discovery found, for an entry discovery made: the host in
    its "SMART Sniffer (<name>)" title. None for any other entry, and for a
    title holding an address, an address and port, the App's container
    name, "localhost" or a container id."""
    if source != SOURCE_ZEROCONF:
        return None
    match = _DEFAULT_TITLE.match(title if isinstance(title, str) else "")
    if not match:
        return None
    text = match.group(1).strip()
    if not text or ":" in text or "[" in text or is_ip(text.rstrip(".")):
        return None
    name = clean_host(text)
    if not name or _is_app_host(name):
        return None
    return name


def entry_host(data: Mapping[str, Any], title: Any, source: Any = None) -> str | None:
    """The host label for an entry's devices, from the entry alone.

    ``source`` is the entry's source; only a discovered entry ("zeroconf")
    may take its name from its title. None only for an HA App entry whose
    kind (agent or replica) is not known yet: no health read has succeeded
    since it was added. The Replica machine (v0.9) is "Replica".
    """
    if is_replica_data(data):
        return REPLICA_LABEL
    if is_app_entry(data, title, source=source):
        mock = data.get(ENTRY_AGENT_MOCK)
        if mock is True:
            return APP_REPLICA_LABEL
        if mock is False or source == SOURCE_HASSIO or data.get(ENTRY_APP_SLUG):
            return APP_AGENT_LABEL  # the discovery never offers the replica agent
        return None
    return (
        clean_host(data.get(ENTRY_AGENT_HOSTNAME))
        or discovered_name(title, source)
        or address_label(agent_address(data))
    )


def app_kind_change(data: Mapping[str, Any], title: Any, health: Any) -> dict[str, bool]:
    """What an HA App entry should store about its kind after a health read.

    The replica agent adds "mock": true to /api/health and the real agent
    never sends it. Only for App entries, and only when it changed. A live
    host name that is not the App's means another agent answers: nothing.
    """
    if not isinstance(health, dict):
        return {}
    live = clean_host(health.get("hostname"))
    if live and not _is_app_host(live):
        return {}
    if not is_app_entry(data, title, live):
        return {}
    mock = health.get("mock") is True
    if data.get(ENTRY_AGENT_MOCK) is mock:
        return {}
    return {ENTRY_AGENT_MOCK: mock}


def agent_name(title: Any, conf_host: Any, host: str | None) -> str:
    """The agent device: "SMART Sniffer (<host>)", or the entry's own title
    when the user gave it one."""
    text = title if isinstance(title, str) else ""
    if text and not _DEFAULT_TITLE.match(text):
        return text
    if host:
        return f"SMART Sniffer ({host})"
    return text or f"SMART Sniffer ({conf_host or 'unknown'})"


def drive_name(host: str | None, model: Any, serial: Any) -> str:
    """A drive device: "<host> <model> (<serial>)"."""
    base = f"{model} ({serial})"
    return f"{host} {base}" if host else base


def pool_name(host: str | None, pool: str, fallback: str | None = None) -> str:
    """A ZFS pool device: "<host> ZFS pool <name>". Without a host, the
    v0.8.3 form with ``fallback``."""
    if host:
        return f"{host} ZFS pool {pool}"
    return f"ZFS pool {pool} ({fallback})" if fallback else f"ZFS pool {pool}"


def disk_usage_name(host: str | None, fallback: str | None = None) -> str:
    """The Disk Usage device: "<host> Disk Usage". Without a host, the
    v0.8.3 form with ``fallback``."""
    if host:
        return f"{host} Disk Usage"
    return f"Disk Usage ({fallback})" if fallback else "Disk Usage"


def pick_shared_host(owners: Iterable[tuple[str | None, str]]) -> str | None:
    """The host for a drive device several entries share: the least
    (host, entry_id) among owners that have a host, host compared without
    case, so every entry picks the same one in any order. None when no owner
    has a host."""
    candidates = [(host.casefold(), host, entry_id) for host, entry_id in owners if host]
    return min(candidates)[1] if candidates else None
