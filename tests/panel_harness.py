"""A Home Assistant core with SMART Sniffer machines, for the panel tests.

Shared by the under-HA panel tests (tests/test_panel_under_ha.py) and the
page tests' backend (tests/panel_page/backend.py). Real Home Assistant
pieces: the core, config entries, the device registry, Stores in a
temporary config folder, dispatcher signals and the websocket command
decorators (require_admin, async_response, schemas). Stand-ins: the
coordinators (each holds what an agent answered, built by the repo's own
mock agent from its replica files) and the websocket connection.

Host names, addresses and serials are invented.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from homeassistant.helpers import device_registry as dr

from tests.test_device_names_under_registry import _make_entry, _new_hass

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "smart_sniffer"

panel_data = importlib.import_module("custom_components.smart_sniffer.panel_data")
panel_ws = importlib.import_module("custom_components.smart_sniffer.panel_ws")
panel_net = importlib.import_module("custom_components.smart_sniffer.panel_net")
devstat = importlib.import_module("custom_components.smart_sniffer.devstat")

_MOCK = None


def mock_agent():
    """tools/mock-agent.py, loaded once, its replica library read."""
    global _MOCK
    if _MOCK is None:
        spec = importlib.util.spec_from_file_location("ss_mock_agent", ROOT / "tools" / "mock-agent.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["ss_mock_agent"] = module
        spec.loader.exec_module(module)
        module.library = module.ReplicaLibrary(module.DEFAULT_REPLICA_DIRS, None)
        module.library.refresh()
        _MOCK = module
    return _MOCK


def agent_data(files: list[str], extra_fs: list[dict] | None = None) -> dict[str, Any]:
    """What a coordinator holds after a poll of an agent serving these
    replica files: drives by id (Device Statistics merged), filesystems,
    pools."""
    mock = mock_agent()
    store = mock.DriveStore()
    for file_id in files:
        store.add_replica(file_id)
    data: dict[str, Any] = {}
    for summary in store.get_summaries():
        drive = store.get_drive(summary["id"])
        drive[devstat.DEVSTAT_KEY], _ = devstat.merge_devstat(drive, None)
        data[drive["id"]] = drive
    data["_filesystems"] = store.served_filesystems() + list(extra_fs or [])
    data["_pools"] = store.served_pools()
    data["_pools_missing"] = []
    return data


class FakeCoordinator:
    def __init__(self, data, ok=True):
        self.data = data
        self.last_update_success = ok
        self.refreshes = 0
        self._listeners = []

    async def async_request_refresh(self):
        self.refreshes += 1

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


def health(hostname, version="0.8.5"):
    return {"connected": True, "version": version, "os": "linux", "uptime_seconds": 86400,
            "hostname": hostname, "os_name": "Debian", "os_version": "12"}


# The board the page tests draw: the App's agent (HAOS) with three drives,
# nas-01 with a drive at Attention, a pool and disk space, studio-mac not
# answering since 3 h, backup-pi that wants its password.
BOARD = [
    {"id": "01HAOS0000000000000000000A", "host": "a0d7b954-smart-sniffer-agent", "port": 9099,
     "title": "SMART Sniffer (a0d7b954-smart-sniffer-agent:9099)",
     "data": {"agent_hostname": "a0d7b954-smart-sniffer-agent", "agent_mock": False},
     "files": ["shipped.nvme-wear", "shipped.sata-hdd-healthy", "shipped.sata-ssd"]},
    {"id": "01NAS00000000000000000000B", "host": "10.0.10.20", "port": 9099,
     "title": "SMART Sniffer (nas-01)", "source": "zeroconf",
     "data": {"agent_hostname": "nas-01"},
     "files": ["shipped.sata-hdd-reallocated", "shipped.sata-hdd-devstat-uncorrectables",
               "shipped.zfs-pool-degraded", "shipped.virtual-disk"],
     "extra_fs": [{"id": "fs-md1", "mountpoint": "/srv/backup", "device": "/dev/md1",
                   "fstype": "ext4", "total_bytes": 4000000000000, "used_bytes": 1000000000000,
                   "available_bytes": 3000000000000, "use_percent": 25.0, "status": "ok"}]},
    {"id": "01MAC00000000000000000000C", "host": "10.0.10.31", "port": 9099,
     "title": "SMART Sniffer (10.0.10.31:9099)", "data": {"agent_hostname": "studio-mac"},
     "files": ["shipped.sas-enterprise"], "down": True, "read_ago": 3 * 3600},
    {"id": "01PI000000000000000000000D", "host": "10.0.10.52", "port": 9099,
     "title": "SMART Sniffer (10.0.10.52:9099)", "data": {"agent_hostname": "backup-pi"},
     "files": [], "reauth": True},
]


class Board:
    """The machines of one test core: entries, coordinators, devices."""

    def __init__(self, hass):
        self.hass = hass
        self.entries = {}
        self.reauth: set[str] = set()

    def add(self, spec: dict[str, Any]):
        hass = self.hass
        data = {"host": spec["host"], "port": spec["port"], "token": spec.get("token", ""),
                "scan_interval": 60, **spec.get("data", {})}
        entry = _make_entry(spec["id"], spec["title"], data, spec.get("source", "user"))
        hass.config_entries._entries[entry.entry_id] = entry
        self.entries[entry.entry_id] = entry
        if spec.get("reauth"):
            self.reauth.add(entry.entry_id)
            return entry
        agent = agent_data(spec.get("files", []), spec.get("extra_fs"))
        coordinator = FakeCoordinator(agent, ok=not spec.get("down"))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator,
            "health_coordinator": SimpleNamespace(data=health(data.get("agent_hostname"))),
        }
        pd = panel_data.panel_data(hass)
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        pd.read_at[entry.entry_id] = panel_data.utc_text(
            datetime.now(UTC) - timedelta(seconds=spec.get("read_ago", 20)))
        registry = dr.async_get(hass)
        for key, drive in agent.items():
            if key.startswith("_"):
                continue
            registry.async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={(DOMAIN, key)},
                name=f"{data.get('agent_hostname')} {drive['model']} ({drive['serial']})",
                model=drive["model"], serial_number=drive["serial"],
            )
        for pool in agent.get("_pools") or []:
            registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{entry.entry_id}_zpool_{pool['name']}")},
                name=f"ZFS pool {pool['name']}")
        registry.async_get_or_create(config_entry_id=entry.entry_id,
                                     identifiers={(DOMAIN, f"{entry.entry_id}_agent")},
                                     name=spec["title"])
        return entry

    def coordinator(self, entry_id):
        return self.hass.data[DOMAIN][entry_id]["coordinator"]


async def board(hass, specs=None, monkeypatch=None) -> Board:
    b = Board(hass)
    for spec in copy.deepcopy(BOARD if specs is None else specs):
        b.add(spec)
    original = getattr(panel_data, "_reauth_active_unpatched", panel_data._reauth_active)
    panel_data._reauth_active_unpatched = original

    def reauth_active(hass_, entry):
        return entry.entry_id in b.reauth or original(hass_, entry)

    if monkeypatch is not None:
        monkeypatch.setattr(panel_data, "_reauth_active", reauth_active)
    else:
        panel_data._reauth_active = reauth_active
    return b


# ── The Replica machine (v0.9) ─────────────────────────────────────────────
#
# The Replica entry with the integration's real ReplicaSource and real
# coordinators (no HTTP: the replica engine in process). Only the entry's
# creation (a config flow through Home Assistant's loader) and the
# platforms are stood in for: the entry is made as the import step makes
# it, and the devices are registered as the platforms register them.

replica = importlib.import_module("custom_components.smart_sniffer.replica")
coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")
source_mod = importlib.import_module("custom_components.smart_sniffer.source")
REPLICA_ID = "01REPLICA0000000000000000R"


def register_devices(hass, entry, data) -> None:
    """The devices the platforms would make for this read."""
    registry = dr.async_get(hass)
    for key, drive in (data or {}).items():
        if key.startswith("_") or not isinstance(drive, dict):
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, key)},
            name=f"Replica {drive.get('model')} ({drive.get('serial')})",
            model=drive.get("model"), serial_number=drive.get("serial"))
    for pool in (data or {}).get("_pools") or []:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_zpool_{pool['name']}")},
            name=f"Replica ZFS pool {pool['name']}")


async def setup_replica_entry(hass, entry) -> None:
    """Set up a Replica entry as async_setup_entry does (coordinators,
    source, first refresh), then its devices."""
    from homeassistant import config_entries  # noqa: PLC0415

    config_entries.current_entry.set(entry)
    source = await replica.async_source(hass, entry)
    coordinator = coordinator_mod.SmartSnifferCoordinator(hass, entry, None, None, replica=source)
    health = coordinator_mod.AgentHealthCoordinator(hass, entry, replica=source)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "health_coordinator": health, "replica": source}
    await coordinator.async_refresh()
    await health.async_refresh()
    panel_data.panel_data(hass).read_at[entry.entry_id] = panel_data.utc_text()
    register_devices(hass, entry, coordinator.data)


def patch_replica(hass, monkeypatch=None) -> None:
    """Make + > Replica's entry creation and refresh work in a test core."""

    async def ensure_entry(hass_):
        found = replica.replica_entry(hass_)
        if found is not None:
            return found
        data = source_mod.replica_entry_data(replica.new_agent_id(), 60)
        entry = _make_entry(REPLICA_ID, source_mod.REPLICA_TITLE, data, "import")
        hass_.config_entries._entries[entry.entry_id] = entry
        await setup_replica_entry(hass_, entry)
        from homeassistant.config_entries import (  # noqa: PLC0415
            SIGNAL_CONFIG_ENTRY_CHANGED,
            ConfigEntryChange,
        )
        from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415

        async_dispatcher_send(hass_, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.ADDED, entry)
        return entry

    original = getattr(replica, "_async_refresh_unpatched", replica.async_refresh)
    replica._async_refresh_unpatched = original

    async def refresh(hass_, entry_id):
        await original(hass_, entry_id)
        entry = hass_.config_entries.async_get_entry(entry_id)
        loaded = (hass_.data.get(DOMAIN) or {}).get(entry_id) or {}
        if entry is not None and loaded.get("coordinator") is not None:
            register_devices(hass_, entry, loaded["coordinator"].data)
            from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415

            async_dispatcher_send(hass_, panel_data.SIGNAL_PANEL_UPDATE, entry_id)

    if monkeypatch is not None:
        monkeypatch.setattr(replica, "async_ensure_entry", ensure_entry)
        monkeypatch.setattr(replica, "async_refresh", refresh)
    else:
        replica.async_ensure_entry = ensure_entry
        replica.async_refresh = refresh


class WsError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class FakeConnection:
    """The parts of HA's ActiveConnection a command handler uses."""

    def __init__(self, hass, admin=True):
        self.hass = hass
        self.user = SimpleNamespace(is_admin=admin, name="Admin" if admin else "Viewer", id="u1")
        self.subscriptions: dict[int, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.results: dict[int, asyncio.Future] = {}
        self.sent: list[Any] = []

    def _future(self, msg_id):
        fut = self.results.get(msg_id)
        if fut is None:
            fut = self.results[msg_id] = asyncio.get_running_loop().create_future()
        return fut

    def send_result(self, msg_id, result=None):
        self.sent.append(result)
        fut = self._future(msg_id)
        if not fut.done():
            fut.set_result(result)

    def send_error(self, msg_id, code, message, *args, **kwargs):
        fut = self._future(msg_id)
        if not fut.done():
            fut.set_exception(WsError(code, message))

    def send_message(self, message):
        self.sent.append(message)
        if isinstance(message, dict) and message.get("type") == "event":
            self.events.append(message["event"])

    def async_handle_exception(self, msg, err):
        fut = self._future(msg["id"])
        if not fut.done():
            fut.set_exception(WsError(type(err).__name__.lower(), str(err)))


def command_handler(name: str):
    """The registered handler of smart_sniffer/panel/<name>."""
    for handler in panel_ws.COMMANDS:
        if handler._ws_command == f"smart_sniffer/panel/{name}":
            return handler
    raise KeyError(name)


_ids = iter(range(1, 10**9))


async def call(hass, connection: FakeConnection, command: str, /, **fields) -> Any:
    """One command through its real decorators and schema; its result, or
    WsError with the code a browser would get."""
    from homeassistant.components.websocket_api.const import ERR_UNAUTHORIZED  # noqa: PLC0415
    from homeassistant.exceptions import Unauthorized  # noqa: PLC0415

    handler = command_handler(command)
    msg = {"id": next(_ids), "type": f"smart_sniffer/panel/{command}", **fields}
    try:
        schema = handler._ws_schema
        if schema is False:
            # Home Assistant 2025+ marks a command with no fields so; the
            # connection then refuses any field but id and type.
            if set(msg) - {"id", "type"}:
                raise ValueError("extra keys not allowed")
        else:
            msg = schema(msg)
    except Exception as err:  # noqa: BLE001 - voluptuous Invalid
        raise WsError("invalid_format", str(err)) from err
    fut = connection._future(msg["id"])
    try:
        handler(hass, connection, msg)
    except Unauthorized as err:
        raise WsError(ERR_UNAUTHORIZED, "Unauthorized") from err
    return await asyncio.wait_for(fut, 10)


__all__ = ["Board", "FakeConnection", "WsError", "board", "call", "_new_hass", "agent_data"]
