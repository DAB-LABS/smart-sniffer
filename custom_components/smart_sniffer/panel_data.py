"""The SMART Sniffer panel's data: one machine per config entry (v0.9).

Everything the panel's page draws comes from here, built from what the
integration already has: the config entries (in any state), the drive and
health coordinators of loaded entries, the device registry, the panel Store
and each entry's last good view. Nothing here reads an agent: a machine that
is not loaded, not answering or waiting for its password shows its last good
read, marked stale.

A machine, as the page receives it:

  host  the machine: entry id, address, port, has_password (never the
        password), kind (haos, replica, field), label, whether its label and
        cards are fixed, and its entry state;
  data  its read in the HA App 0.3.1 shape (status, read_at, last_answer,
        agent, summary, items, filesystem_cards), built by panel_view;
  ha    its devices in Home Assistant (card key -> device id and names) and
        what the trash can do in Home Assistant for each card.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .agent_identity import ENTRY_AGENT_MOCK
from .attention import get_thresholds
from .const import CONF_TOKEN, DOMAIN, FILESYSTEMS_KEY, POOLS_KEY
from .host_name import APP_HOST, entry_host, is_app_entry
from .panel_state import PanelState, ViewStores
from .panel_view import (
    build_items,
    card_id,
    card_list,
    ha_removals,
    mark_stale,
    split_card_id,
    summarize,
    without_removed,
)
from .source import agent_address, agent_port, is_replica_data

_LOGGER = logging.getLogger(__name__)

PANEL_DATA = "smart_sniffer_panel"
SIGNAL_PANEL_UPDATE = "smart_sniffer_panel_update"  # (entry_id)
SIGNAL_PANEL_STATE = "smart_sniffer_panel_state"  # ()

INTEGRATION_URL = "/config/integrations/integration/smart_sniffer"
VERSION: str = json.loads((Path(__file__).parent / "manifest.json").read_text())["version"]

# The + card's Replica area and the Replica machine (replica.py).
REPLICA_ENABLED = True

# The HA App's slug in the Supervisor's app list.
APP_SLUG_SUFFIX = "_smart_sniffer_agent"


def utc_text(when: datetime | None = None) -> str:
    """A time as the page reads it: UTC, ISO 8601, seconds."""
    return (when or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PanelData:
    """The panel's per-run state: its Stores and what each entry last read."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.state = PanelState(hass)
        self.views = ViewStores(hass)
        # entry_id -> time of the last poll that answered (this run)
        self.read_at: dict[str, str] = {}
        # entry_id -> (cache key, fresh response) of the last build
        self._built: dict[str, tuple[Any, dict[str, Any]]] = {}
        # A Store change reaches every open page.
        self.state.add_listener(lambda: async_dispatcher_send(hass, SIGNAL_PANEL_STATE))

    def forget(self, entry_id: str) -> None:
        self.read_at.pop(entry_id, None)
        self._built.pop(entry_id, None)


@callback
def panel_data(hass: HomeAssistant) -> PanelData:
    """The panel's data for this run, made on first use."""
    data = hass.data.get(PANEL_DATA)
    if data is None:
        data = hass.data[PANEL_DATA] = PanelData(hass)
    return data


def entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every SMART Sniffer entry the user has, in any state; ignored ones
    are not machines."""
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.source != "ignore"]


def entry_kind(entry: ConfigEntry) -> str:
    """haos (the HA App's agent), replica (the Replica machine, or the HA
    App's old replica agent until it is removed), or field."""
    if is_replica_data(entry.data):
        return "replica"
    if is_app_entry(entry.data, entry.title, source=entry.source):
        return "replica" if entry.data.get(ENTRY_AGENT_MOCK) is True else "haos"
    return "field"


def _loaded(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    return (hass.data.get(DOMAIN) or {}).get(entry_id) or {}


def _reauth_active(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        return any(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    except Exception:  # noqa: BLE001 - no flow manager (tests)
        return False


def status(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """ok, reading, unreachable, password_needed, password_refused or disabled."""
    if entry.disabled_by is not None:
        return "disabled"
    if _reauth_active(hass, entry):
        return "password_refused" if entry.data.get(CONF_TOKEN) else "password_needed"
    coordinator = _loaded(hass, entry.entry_id).get("coordinator")
    if coordinator is not None:
        if coordinator.last_update_success and coordinator.data is not None:
            return "ok"
        return "unreachable"
    if entry.state in (ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.NOT_LOADED) \
            and entry.entry_id not in panel_data(hass).read_at:
        return "reading"
    return "unreachable"


def host_object(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The machine, as the page's host: no password, only whether it has one."""
    kind = entry_kind(entry)
    # The Replica machine has no address (source.py): "" and None.
    port = agent_port(entry.data)
    address = agent_address(entry.data) or ""
    return {
        "id": entry.entry_id,
        "address": address,
        "port": port,
        "has_password": bool(entry.data.get(CONF_TOKEN)),
        "kind": kind,
        "label": entry_host(entry.data, entry.title, entry.source) or address,
        "fixed": kind in ("haos", "replica"),
        "removable": kind != "haos",
        "entry_state": str(getattr(entry.state, "value", entry.state)),
        "disabled": entry.disabled_by is not None,
        "title": entry.title,
        "entry_url": f"{INTEGRATION_URL}#config_entry={entry.entry_id}",
        "added_at": utc_text(entry.created_at) if isinstance(
            getattr(entry, "created_at", None), datetime) else None,
    }


MISSING_FILE = "replica_missing"


def replica_cards(entry_id: str, items: list[dict[str, Any]], info: dict[str, Any]) -> None:
    """The Replica's cards: each drive and pool card says which library file
    it shows (lab.name, lab.file); a pick whose file is gone (a seed file a
    later release dropped, a user file deleted) gets a card of its own that
    says so, so it can be removed."""
    cards = info.get("cards") or {}
    for item in items:
        found = cards.get(split_card_id(item.get("card_id") or "")[1] or "")
        if found:
            item["lab"] = {"name": found.get("name"), "file": found.get("file_id"),
                           "source": found.get("source"), "kind": found.get("kind"),
                           "missing": False}
    for pick in info.get("missing") or []:
        is_pool = pick.get("kind") == "pool"
        key = f"pool:{pick.get('pool_name')}" if is_pool else str(pick.get("serial"))
        items.append({
            "type": "drive", "id": str(pick.get("drive_id") or f"pool.{pick.get('pool_name')}"),
            "card_id": card_id(entry_id, key),
            "drive": {"id": pick.get("drive_id"), "model": pick.get("name"),
                      "serial": None if is_pool else pick.get("serial")},
            "verdict": None, "view": None, "error": MISSING_FILE, "filesystems": [],
            "lab": {"name": pick.get("name"), "file": pick.get("file_id"), "source": None,
                    "kind": "zfs_pool" if is_pool else None, "missing": True},
        })


def _agent_block(label: str, health: dict[str, Any] | None, items: list[dict[str, Any]],
                 last: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(health, dict) or not health.get("version"):
        return (last or {}).get("agent")
    return {
        "hostname": label,
        "version": health.get("version"), "os": health.get("os"),
        "os_name": health.get("os_name"), "uptime_seconds": health.get("uptime_seconds"),
        "drives": sum(1 for i in items if i.get("type") == "drive"),
        "pools": sum(1 for i in items if i.get("type") == "pool"),
    }


def fresh_response(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any] | None:
    """The machine's read from its coordinator's last good poll, built once
    per poll and threshold change; None when it has none this run."""
    loaded = _loaded(hass, entry.entry_id)
    coordinator = loaded.get("coordinator")
    data = getattr(coordinator, "data", None)
    if not isinstance(data, dict):
        return None
    health_coordinator = loaded.get("health_coordinator")
    health = getattr(health_coordinator, "data", None)
    pd = panel_data(hass)
    thresholds = json.dumps(entry.data.get("thresholds") or {}, sort_keys=True, default=str)
    label = entry_host(entry.data, entry.title, entry.source) or str(agent_address(entry.data))
    key = (id(data), thresholds, label, id(health))
    built = pd._built.get(entry.entry_id)
    if built is not None and built[0] == key:
        return built[1]
    read_at = pd.read_at.get(entry.entry_id) or utc_text()
    drives = {k: v for k, v in data.items() if not str(k).startswith("_")}
    pools = data.get(POOLS_KEY)
    items, fs_cards = build_items(
        entry.entry_id, drives, lambda drive_id: get_thresholds(entry, drive_id), VERSION,
        pools if isinstance(pools, list) else None, data.get(FILESYSTEMS_KEY),
    )
    replica = loaded.get("replica")
    if replica is not None:
        replica_cards(entry.entry_id, items, replica.card_info())
    response = {
        "id": entry.entry_id, "kind": entry_kind(entry), "status": "ok",
        "read_at": read_at, "last_answer": read_at,
        "agent": _agent_block(label, health, items),
        "summary": summarize(items, True),
        "items": items,
        "filesystem_cards": fs_cards,
        "pools_status": "ok" if isinstance(pools, list) else "failed",
        "filesystems_status": "ok",
        "rules_version": VERSION,
        "stale": False,
        "health": {k: v for k, v in (health or {}).items() if k != "connected"} or None,
    }
    pd._built[entry.entry_id] = (key, response)
    return response


async def async_response(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The machine's read in any state: fresh, or the last good one marked
    stale, or none."""
    pd = panel_data(hass)
    state = status(hass, entry)
    fresh = fresh_response(hass, entry)
    if fresh is not None:
        pd.views.put(entry.entry_id, fresh)
        if state == "ok":
            return fresh
        return mark_stale(fresh, state)
    last = await pd.views.async_get(entry.entry_id)
    if isinstance(last, dict) and last.get("items") is not None:
        return mark_stale(last, "unreachable" if state in ("ok", "reading") else state)
    return {
        "id": entry.entry_id, "kind": entry_kind(entry), "status": state,
        "read_at": utc_text(), "last_answer": None, "agent": None,
        "summary": summarize([], False), "items": [], "filesystem_cards": [],
        "pools_status": None, "filesystems_status": None, "rules_version": VERSION,
        "stale": True, "health": None,
    }


def ha_target(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The machine's devices in Home Assistant, by card key: as the App's
    lookup had them, from the device registry."""
    entry_id = entry.entry_id
    target: dict[str, Any] = {
        "entry_id": entry_id,
        "entry_url": f"{INTEGRATION_URL}#config_entry={entry_id}",
        "state": str(getattr(entry.state, "value", entry.state)),
        "devices": {}, "names": {}, "identifiers": {},
    }
    registry = dr.async_get(hass)
    prefix = f"{entry_id}_zpool_"
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        names = {"name": device.name, "name_by_user": device.name_by_user}

        def put(key: str, device_id: str = device.id, names: dict = names) -> None:
            target["devices"][key] = device_id
            target["names"][key] = names

        ours = [str(i[1]) for i in device.identifiers if i[0] == DOMAIN]
        drive_ident = next((i for i in ours if not i.startswith(f"{entry_id}_")), None)
        serial = device.serial_number
        if isinstance(serial, str) and serial and drive_ident is not None:
            put(serial)
            target["identifiers"][serial] = drive_ident
        for ident in ours:
            if ident.startswith(prefix):
                put(f"pool:{ident[len(prefix):]}")
            elif not ident.startswith(f"{entry_id}_"):
                # A drive whose serial the agent does not give is known by
                # its drive id (its card key is "id:<drive id>").
                put(f"id:{ident}")
                target["identifiers"][f"id:{ident}"] = ident
    return target


async def async_machine(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any] | None:
    """One machine for the page; None when every card of it was removed from
    the page with the trash."""
    pd = panel_data(hass)
    state = await pd.state.async_load()
    response = await async_response(hass, entry)
    removed = {cid for cid in state["removed"] if split_card_id(cid)[0] == entry.entry_id}
    shown = without_removed(response, removed)
    if removed and card_list(response) and not card_list(shown):
        return None
    target = ha_target(hass, entry)
    return {
        "host": host_object(hass, entry),
        "data": shown,
        "ha": {"target": target, "removal": ha_removals(entry.entry_id, target, response, removed)},
    }


def page_state(pd: PanelData) -> dict[str, Any]:
    """The panel Store as the page reads it."""
    state = pd.state.data
    return {
        "order": list(state["order"]),
        "hidden": [cid for cid, hidden in state["hidden"].items() if hidden],
        "names": dict(state["card_names"]),
        "host_names": dict(state["machine_names"]),
        "notes": dict(state["notes"]),
        "read_only": pd.state.read_only,
    }


# ── This Home Assistant and the HA App ──────────────────────────────────────


def is_hassio(hass: HomeAssistant) -> bool:
    return "hassio" in hass.config.components


def app_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """The SMART Sniffer App in the Supervisor's app list: {slug, state}; {}
    when the list has no such app; None when there is no list (Container,
    Core) or it is not known yet."""
    if not is_hassio(hass):
        return None
    try:
        from homeassistant.components.hassio import get_addons_info  # noqa: PLC0415
    except ImportError:
        return None
    try:
        addons = get_addons_info(hass)
    except Exception:  # noqa: BLE001 - HassioNotReadyError and the like
        return None
    if not isinstance(addons, dict):
        return None
    for slug, info in addons.items():
        if isinstance(slug, str) and slug.endswith(APP_SLUG_SUFFIX) and (
                slug.split("_", 1)[0] == "local" or len(slug.split("_", 1)[0]) == 8):
            state = (info or {}).get("state") if isinstance(info, dict) else None
            return {"slug": slug, "state": state}
    return {}


def _app_entry(hass: HomeAssistant) -> ConfigEntry | None:
    return next((e for e in entries(hass) if entry_kind(e) == "haos"), None)


def _app_page(hass: HomeAssistant, slug: str) -> str:
    """The App's page in Home Assistant: /config/app/<slug>/info from 2026,
    /hassio/addon/<slug>/info before."""
    legacy = HA_VERSION.split(".", 1)[0].isdigit() and int(HA_VERSION.split(".", 1)[0]) < 2026
    return f"/hassio/addon/{slug}/info" if legacy else f"/config/app/{slug}/info"


def page_info(hass: HomeAssistant) -> dict[str, Any]:
    """The panel's own facts: versions, the agent line, the App note."""
    pd = panel_data(hass)
    app = app_info(hass)
    app_entry = _app_entry(hass)
    # The App's agent line: on HAOS or Supervised only, while its agent is a
    # machine here and the Supervisor's app list does not say the App is gone.
    agent_line = None
    if is_hassio(hass) and app_entry is not None and app != {}:
        agent_line = {
            "addr": f"{agent_address(app_entry.data)}:{agent_port(app_entry.data)}",
            "mdns": "",
        }
    notes = pd.state.data["notes"]
    app_note = (is_hassio(hass) and app == {} and app_entry is None
                and not notes.get("app_hint"))
    return {
        "version": VERSION,
        "rules_version": VERSION,
        "ha_version": HA_VERSION,
        "is_hassio": is_hassio(hass),
        "integration_url": INTEGRATION_URL,
        "agent_line": agent_line,
        "app_note": bool(app_note),
        "app_url": _app_page(hass, app["slug"]) if app else None,
        "replica": REPLICA_ENABLED,
        "replica_caps": {"drives": 12, "pools": 4},
        "read_only": pd.state.read_only,
        "app_import": _import_report(hass),
    }


def _import_report(hass: HomeAssistant) -> dict[str, Any] | None:
    """The SMART Sniffer App handover's one-time report line (panel_import)."""
    try:
        from .panel_import import page_report  # noqa: PLC0415

        return page_report(hass)
    except Exception:  # noqa: BLE001 - the page draws without it
        _LOGGER.exception("SMART Sniffer panel: the App handover report could not be read")
        return None


async def async_load_import(hass: HomeAssistant) -> None:
    """Read the handover's Store before the page's first answer."""
    try:
        from .panel_import import app_import  # noqa: PLC0415

        await app_import(hass).async_load()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SMART Sniffer panel: the App handover record could not be read")


async def async_cards(hass: HomeAssistant) -> dict[str, Any]:
    """Everything the page draws, at once."""
    pd = panel_data(hass)
    await pd.state.async_load()
    await async_load_import(hass)
    machines = []
    for entry in entries(hass):
        try:
            machine = await async_machine(hass, entry)
        except Exception:  # noqa: BLE001 - one machine never fails the page
            _LOGGER.exception("SMART Sniffer panel: machine %s could not be built", entry.entry_id)
            continue
        if machine is not None:
            machines.append(machine)
    return {"info": page_info(hass), "state": page_state(pd), "machines": machines}


# ── Agentic access ──────────────────────────────────────────────────────────

AGENTIC_SCHEMA = "smart-sniffer-agentic-access"
AGENTIC_SCHEMA_VERSION = 1
AGENTIC_ABOUT = (
    "SMART Sniffer drive health export from the SMART Sniffer integration in Home Assistant. "
    "A read-only snapshot of every agent the integration reads, with each drive's raw SMART "
    "data, ZFS pools, filesystems and the health verdicts the Control Center shows. Read the "
    "guide at the end before drawing conclusions."
)
AGENTIC_ERRORS = {
    "unreachable": "The agent did not answer. Readings are from last_answer.",
    "password_needed": "The agent needs a token and none is stored. Readings are from last_answer.",
    "password_refused": "The agent refused the stored token. Readings are from last_answer.",
    "disabled": "The agent is disabled in Home Assistant. Readings are from last_answer.",
    "reading": "The agent is being read for the first time. No readings yet.",
}
AGENTIC_NEVER_READ = "The agent has not answered since it was added. No readings."
AGENTIC_GUIDE: dict[str, Any] = {
    "purpose": (
        "Read-only drive health data from SMART Sniffer agents, judged by the SMART Sniffer "
        "integration's rules in Home Assistant. Nothing in this document can change a setting."
    ),
    "structure": [
        "agents: one entry per agent the integration reads (one config entry each). kind haos "
        "is the agent of the SMART Sniffer App in this Home Assistant, reading this machine's "
        "drives. kind field is a SMART Sniffer agent on another machine. kind replica holds "
        "simulated drives for testing (simulated is true); they are not real hardware, leave "
        "them out of real health answers.",
        "counts covers the real agents only (agents, drives, pools, attention, maybe, "
        "unsupported, unreachable_agents). The Replica's simulated drives and pools are "
        "counted apart in replica_drives, replica_pools, replica_attention and replica_maybe.",
        "agents[].status is ok, unreachable, password_needed, password_refused, disabled or "
        "reading. When it is not ok, stale is true and the drives are the last good readings, "
        "taken at last_answer. Say that the data may be out of date.",
        "agents[].health is the agent's own /api/health answer from its last good read.",
        "agents[].drives[].data is the agent's payload for the drive. data.smart_data is smartctl "
        "JSON output, unchanged.",
        "agents[].drives[].attention is the verdict. It is null when the drive could not be read "
        "(error is set); health is then not known.",
        "agents[].pools[].verdict.state is YES (problems lists why) or NO. Pools have no MAYBE.",
        "Filesystems sit on the drive or pool they belong to. agents[].filesystems holds those "
        "that belong to neither, each as a card with the filesystem in data.",
        "hidden true marks a card the user hid in the Control Center. It is still monitored.",
    ],
    "attention_states": {
        "NO": "All monitored indicators are clear.",
        "MAYBE": "Warning. An early sign of wear or degradation. Plan a replacement and watch "
                 "the reasons.",
        "YES": "Critical. Data is at risk. Back up now.",
        "UNSUPPORTED": "No usable SMART data, so health cannot be judged. Often a USB bridge that "
                       "does not pass SMART through. If data.smart_lost is true, the drive gave "
                       "SMART data earlier in this agent run and has stopped; that is not the "
                       "same as never supported.",
    },
    "severity": "critical goes with YES, warning with MAYBE, none with NO and UNSUPPORTED.",
    "reasons": (
        "attention.reasons lists what caused the state, one line each. attention.accepted lists "
        "readings the user accepted with a custom threshold; they are known and not new "
        "problems. attention.limits gives each threshold, its default and whether the user "
        "changed it. attention.readings holds the values the rules used; prefer them over your "
        "own reading of raw tables."
    ),
    "fields": [
        "Wear, NVMe: nvme_smart_health_information_log.percentage_used counts up, percent of "
        "rated life used. It can pass 100; that means the rated endurance is used up, not that "
        "the drive has failed.",
        "Wear, ATA: wear attributes such as Wear_Leveling_Count, SSD_Life_Left and "
        "Media_Wearout_Indicator usually count down (normalized value is life remaining); the "
        "rules turn them into percent used.",
        "Wear, WD SSDs: on WD Blue, Red and Green SATA SSDs (model_family \"WD Blue / Red / Green "
        "SSDs\") attribute 230 counts up and is already percent used. The rules use Device "
        "Statistics Percentage Used Endurance Indicator first, and report wear as unknown when "
        "attribute 230 and Average_PE_Cycles_TLC disagree badly.",
        "Raw values: some vendors pack several numbers into one ATA raw value (for example "
        "Seagate attribute 188). Treat large raw values with care; the rules decode the "
        "counters they use.",
        "Standby: data.in_standby true means the drive was spun down and the agent served its "
        "cached readings. data.last_updated is when SMART data was last read; Home Assistant "
        "shows it as the data_as_of attribute.",
        "data.readable false means smartctl could not read the drive in the last cycle; the "
        "readings are cached.",
        "data.smart_available false means the drive answered without SMART data; "
        "data.smart_unavailable_reason says why. data.smart_lost true means it had SMART data "
        "earlier in this agent run. Both need agent 0.8.3 or later and are absent when not set.",
        "Units: temperature.current is degrees Celsius. power_on_time.hours is hours. NVMe "
        "data_units_read and data_units_written count units of 512000 bytes. "
        "data.derived.host_writes.bytes and host_reads.bytes are bytes. Filesystem sizes are "
        "bytes and use_percent is percent.",
        "Times are UTC in ISO 8601.",
    ],
    "home_assistant_action": {
        "action": "smart_sniffer.get_drive_data",
        "what": "Returns the cached SMART data and verdicts for all drives of one SMART Sniffer "
                "agent in Home Assistant. It only returns data, so it must be called with a "
                "response variable.",
        "required": "config_entry_id: the integration entry of the agent. It is "
                    "agents[].home_assistant.config_entry_id here.",
        "example": "action: smart_sniffer.get_drive_data\ndata:\n  config_entry_id: "
                   "<config_entry_id>\nresponse_variable: drive_data",
        "response": "agent: name, host, os, uptime, version. drives: one object per drive id with "
                    "model, serial, protocol, device_path, attention (state, severity, reasons, "
                    "accepted), smart_data, device_statistics, derived. filesystems: mountpoint, "
                    "total_bytes, used_bytes, use_percent. Pools are not included.",
    },
}


def _iso_epoch(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def agentic_cards(response: dict[str, Any], hidden: set[str], names: dict[str, str],
                  devices: dict[str, Any], device_names: dict[str, Any]
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(drives, pools, filesystems) of one machine's read, as the export gives
    them: the page's view left out, every card's name, hidden flag and Home
    Assistant device beside the agent's own payload."""
    def common(cid: str) -> dict[str, Any]:
        key = split_card_id(cid)[1]
        named = (device_names.get(key) or {}).get("name_by_user") if key else None
        return {"name": named or names.get(cid), "hidden": cid in hidden,
                "ha_device_id": devices.get(key) if key else None}

    drives: list[dict[str, Any]] = []
    pools: list[dict[str, Any]] = []
    for item in response.get("items") or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("card_id") or "")
        if item.get("type") == "pool":
            pools.append({
                "card_id": cid, **common(cid), "stale": bool(item.get("stale")),
                "verdict": item.get("verdict"),
                "filesystems": item.get("filesystems") or [],
                "data": item.get("pool"),
            })
            continue
        verdict = item.get("verdict")
        drives.append({
            "card_id": cid, "id": item.get("id"), **common(cid),
            "stale": bool(item.get("stale")),
            "error": item.get("error"),
            "attention": ({**verdict, "thresholds_source": item.get("thresholds_source")}
                          if isinstance(verdict, dict) else None),
            "filesystems": item.get("filesystems") or [],
            "data": item.get("drive"),
        })
    filesystems: list[dict[str, Any]] = []
    for card in response.get("filesystem_cards") or []:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("card_id") or "")
        filesystems.append({"card_id": cid, **common(cid), "stale": bool(card.get("stale")),
                            "data": card.get("filesystem")})
    return drives, pools, filesystems


async def async_agentic(hass: HomeAssistant) -> dict[str, Any]:
    """The Agentic access document, from what the integration has now."""
    pd = panel_data(hass)
    state = await pd.state.async_load()
    generated = utc_text()
    now = _iso_epoch(generated) or 0.0
    hidden = {cid for cid, h in state["hidden"].items() if h}
    agents: list[dict[str, Any]] = []
    for entry in entries(hass):
        try:
            machine = await async_machine(hass, entry)
            if machine is None:
                continue
            host, response, target = machine["host"], machine["data"], machine["ha"]["target"]
            status_ = str(response.get("status") or "error")
            last_answer = response.get("last_answer")
            answered = _iso_epoch(last_answer)
            error = None
            if status_ != "ok":
                error = {"code": status_,
                         "message": AGENTIC_ERRORS.get(status_) if last_answer
                         else AGENTIC_NEVER_READ}
            drives, pools, filesystems = agentic_cards(
                response, hidden, state["card_names"], target["devices"], target["names"])
            agents.append({
                "id": entry.entry_id,
                "kind": host["kind"],
                "simulated": host["kind"] == "replica",
                "name": state["machine_names"].get(entry.entry_id) or host["label"],
                "address": host["address"],
                "port": host["port"],
                "has_token": host["has_password"],
                "status": status_,
                "stale": bool(response.get("stale")),
                "read_at": response.get("read_at"),
                "last_answer": last_answer,
                "data_age_seconds": max(0, int(now - answered)) if answered is not None else None,
                "error": error,
                "agent": response.get("agent"),
                "health": response.get("health"),
                "summary": response.get("summary"),
                "home_assistant": {"config_entry_id": entry.entry_id,
                                   "entry_state": host["entry_state"]},
                "pools_status": response.get("pools_status"),
                "filesystems_status": response.get("filesystems_status"),
                "drives": drives,
                "pools": pools,
                "filesystems": filesystems,
            })
        except Exception:  # noqa: BLE001 - one agent never fails the document
            _LOGGER.exception("SMART Sniffer agentic access: %s failed", entry.entry_id)
            agents.append({"id": entry.entry_id, "kind": entry_kind(entry),
                           "simulated": entry_kind(entry) == "replica", "status": "error",
                           "stale": True, "error": {"code": "internal_error", "message": None}})
    counts = agentic_counts(agents)
    return {
        "about": AGENTIC_ABOUT,
        "schema": AGENTIC_SCHEMA,
        "schema_version": AGENTIC_SCHEMA_VERSION,
        "generated_at": generated,
        "integration_version": VERSION,
        "rules_version": VERSION,
        "home_assistant": {"version": HA_VERSION},
        "counts": counts,
        "agents": agents,
        "guide": AGENTIC_GUIDE,
    }


def agentic_counts(agents: list[dict[str, Any]]) -> dict[str, int]:
    """The Agentic access counts: real agents only (as the HA App 0.3.1
    counted them), and the Replica's simulated cards apart. The page's
    header line uses these same numbers."""
    real = [a for a in agents if a.get("kind") != "replica"]
    replicas = [a for a in agents if a.get("kind") == "replica"]

    def total(group: list[dict[str, Any]], key: str) -> int:
        return sum(len(a.get(key) or []) for a in group)

    def summed(group: list[dict[str, Any]], key: str) -> int:
        return sum(int((a.get("summary") or {}).get(key) or 0) for a in group)

    return {
        "agents": len(real),
        "drives": total(real, "drives"),
        "pools": total(real, "pools"),
        "attention": summed(real, "attention"),
        "maybe": summed(real, "maybe"),
        "unsupported": summed(real, "unsupported"),
        "unreachable_agents": sum(1 for a in real if a.get("status") != "ok"),
        "replica_drives": total(replicas, "drives"),
        "replica_pools": total(replicas, "pools"),
        "replica_attention": summed(replicas, "attention"),
        "replica_maybe": summed(replicas, "maybe"),
    }


def app_host_name(name: Any) -> bool:
    """Whether a host name is the HA App's container (its agent)."""
    return isinstance(name, str) and APP_HOST.match(name.lower()) is not None
