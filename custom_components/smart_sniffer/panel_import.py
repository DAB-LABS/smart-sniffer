"""The SMART Sniffer App hands its field machines over (v0.9).

App 0.3.x kept the field machines of its Control Center in its own options:
address, port and password of each, machine labels, card names, hidden
cards, the card order and per-card thresholds. App 0.4.0 sends all of it
here once, over the Supervisor's Core websocket, and clears what this side
confirms.

Two commands, for an admin that is a system user (the App reaches Core
through the Supervisor, which signs in as its own system user, an admin):

  smart_sniffer/app/import         {items}: starts a job, answers {job_id}
                                   at once; the work runs in the background
  smart_sniffer/app/import/result  {job_id}: {state: running} or {state:
                                   done, report}

and two for the panel's admins: ``smart_sniffer/panel/app_import/dismiss``
(the report line) and ``smart_sniffer/panel/app_import/discard`` (one
skipped machine: never asked for again).

The rules:

- Integration state wins: names, hidden marks, thresholds and the order are
  filled where the integration has none (gaps only).
- A small ledger (its own Store, ``smart_sniffer.app_import``) keeps every
  item done, with its outcome and machine, never a password. An item in the
  ledger is answered with its recorded outcome and never probed, created or
  filled again: a lost reply, a retry or a second App start changes
  nothing, and a machine the user deleted after the handover stays deleted
  (its row is marked removed then).
- A machine is matched by its agent_id first, then by being the App's own
  agent (HAOS), then by its address; else it is made through the config
  flow's import step. One that does not answer is made too, and shows
  offline until it does (owner decision). Probes: 4 at a time, 5 s each.
- Settings that need the machine's drives (a drive's name is its device's
  name; thresholds are kept by drive id) wait in the Store until the
  machine is read, then are filled the same way.
- The App's old Replica settings are confirmed as dropped (replicas live in
  the integration now, made again from + > Replica).
- Passwords go into the entry's data (its token) and nowhere else: no
  answer, report, ledger row or log line holds one. A machine already here
  gets the App's password only when matched by the App's own record (the
  agent_id it sends, its own agent, or the address), never by the agent_id
  an agent answers in /api/health.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.storage import Store

from .agent_identity import ENTRY_AGENT_ID, norm_agent_id, norm_host
from .const import CONF_HOST, CONF_PORT, CONF_TOKEN, DOMAIN, IMPORT_FROM_APP
from .host_name import APP_HOST
from .panel_data import (
    SIGNAL_PANEL_STATE,
    SIGNAL_PANEL_UPDATE,
    entries,
    entry_kind,
    fresh_response,
    ha_target,
    host_object,
    panel_data,
    utc_text,
)
from .panel_state import PanelStateNewer, clean_name
from .panel_view import card_list, clean_overrides, split_card_id
from .source import agent_address, agent_port

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "smart_sniffer.app_import"
STORE_VERSION = 1
STORE_MINOR = 1
SAVE_DELAY = 1  # seconds

PROBES = 4  # machines asked at once
PROBE_TIMEOUT = 5  # seconds per machine
JOBS_KEPT = 20
DATA = "smart_sniffer_app_import"

# Outcomes. Final ones go into the ledger and are never redone.
CREATED = "created"  # a new machine, answering
CREATED_OFFLINE = "created_offline"  # a new machine that did not answer
MATCHED = "matched"  # already a machine here; settings filled in its gaps
APPLIED = "applied"  # this box's settings (HAOS), the order
PENDING = "pending"  # this box's settings, waiting for its machine
KEPT = "kept"  # the order: the user's own order here wins
REPLICAS_DROPPED = "replicas_dropped"
DISCARDED = "discarded"  # a skipped machine the user let go
SKIPPED = "skipped"  # not done; tried again next time (reason given)
FINAL = (CREATED, CREATED_OFFLINE, MATCHED, APPLIED, PENDING, KEPT, REPLICAS_DROPPED, DISCARDED)

ITEM_ID = vol.All(cv.string, vol.Length(min=1, max=80))
NAME = vol.All(cv.string, vol.Length(max=400))
CARD_KEY = vol.All(cv.string, vol.Length(min=1, max=300))
APP_CARD_ID = vol.All(cv.string, vol.Length(min=3, max=400))
THRESHOLDS = vol.Schema({vol.All(cv.string, vol.Length(min=1, max=100)):
                         vol.All(vol.Coerce(int), vol.Range(min=0))})
CARD = vol.Schema({
    vol.Optional("name"): NAME,
    vol.Optional("hidden"): bool,
    vol.Optional("thresholds"): THRESHOLDS,
})
CARDS = vol.All({CARD_KEY: CARD}, vol.Length(max=500))
FIELD = vol.Schema({
    vol.Required("item_id"): ITEM_ID,
    vol.Required("kind"): "field",
    vol.Required("address"): vol.All(cv.string, vol.Length(min=1, max=253)),
    vol.Required("port"): cv.port,
    vol.Optional("password", default=""): vol.All(cv.string, vol.Length(max=1000)),
    vol.Optional("agent_id"): vol.Any(None, vol.All(cv.string, vol.Length(max=64))),
    vol.Optional("hostname"): vol.Any(None, vol.All(cv.string, vol.Length(max=253))),
    vol.Optional("name"): NAME,
    vol.Optional("hidden", default=False): bool,
    vol.Optional("removed", default=[]): vol.All([CARD_KEY], vol.Length(max=500)),
    vol.Optional("cards", default={}): CARDS,
})
SELF = vol.Schema({
    vol.Required("item_id"): ITEM_ID,
    vol.Required("kind"): "self",
    vol.Optional("cards", default={}): CARDS,
})
ORDER = vol.Schema({
    vol.Required("item_id"): ITEM_ID,
    vol.Required("kind"): "order",
    vol.Required("order"): vol.All([APP_CARD_ID], vol.Length(max=2000)),
})
REPLICAS = vol.Schema({
    vol.Required("item_id"): ITEM_ID,
    vol.Required("kind"): "replicas",
})
ITEM = vol.Any(FIELD, SELF, ORDER, REPLICAS)


def host_key(address: str, port: int) -> str:
    """The App's id of a field machine: "<address>:<port>", lower case."""
    return f"{str(address).lower()}:{int(port)}"


# ── The Store: ledger, waiting settings, report ──────────────────────────────


def empty_store() -> dict[str, Any]:
    return {
        "ledger": {},  # item_id -> {outcome, entry_id, host_key, at, counts, removed_at}
        "waiting": {},  # entry_id -> {item_id, names, thresholds, host_hidden}
        "waiting_app": None,  # this box's settings while it is no machine here
        "skipped": {},  # item_id -> {address, port, reason, at}
        "dismissed_at": None,  # the report line closed at this time
    }


class _ImportStore(Store):
    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        if old_major_version > STORE_VERSION:
            raise ValueError("app import Store is newer")
        return old_data


class AppImport:
    """The handover's Store and jobs for this run."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = _ImportStore(hass, STORE_VERSION, STORE_KEY, minor_version=STORE_MINOR)
        self.data: dict[str, Any] | None = None
        self.read_only = False
        self.lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> dict[str, Any]:
        if self.data is not None:
            return self.data
        async with self._load_lock:
            if self.data is None:
                try:
                    stored = await self._store.async_load()
                except Exception as err:  # noqa: BLE001 - newer or unreadable
                    _LOGGER.warning("SMART Sniffer: the App handover record could not be "
                                    "read (%s); nothing is moved", err)
                    self.read_only = True
                    stored = None
                data = empty_store()
                if isinstance(stored, dict):
                    for key, value in stored.items():
                        if key in data and (data[key] is None or isinstance(value, type(data[key]))):
                            data[key] = value
                self.data = data
        return self.data

    def save(self) -> None:
        if not self.read_only and self.data is not None:
            self._store.async_delay_save(lambda: self.data, SAVE_DELAY)
            async_dispatcher_send(self.hass, SIGNAL_PANEL_STATE)


@callback
def app_import(hass: HomeAssistant) -> AppImport:
    found = hass.data.get(DATA)
    if found is None:
        found = hass.data[DATA] = AppImport(hass)
    return found


# ── Who may ask: a system user that is an admin ──────────────────────────────


def is_system_user(user: Any) -> bool:
    """The App's calls come through the Supervisor, as its own system user
    (an admin: require_admin checks that on each command). A person's login,
    admin or not, is never one."""
    return user is not None and bool(getattr(user, "system_generated", False))


# ── Machines ─────────────────────────────────────────────────────────────────


async def async_health(hass: HomeAssistant, address: str, port: int) -> dict[str, Any] | None:
    """The agent's /api/health (it needs no password), PROBE_TIMEOUT at most;
    None when nothing that is an agent answers."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(f"http://{address}:{port}/api/health",
                               timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError, OSError):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


async def async_create(hass: HomeAssistant, address: str, port: int,
                       password: str) -> tuple[str, str | None]:
    """Make the machine through the config flow's import step (its duplicate
    checks; an offline machine is made too): (created, entry id),
    (already_configured, None), (replicas_moved, None) or (error, None)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"},
        data={CONF_HOST: address, CONF_PORT: port, CONF_TOKEN: password, IMPORT_FROM_APP: True})
    kind = result.get("type")
    kind = str(getattr(kind, "value", kind))
    if kind == "create_entry":
        return "created", getattr(result.get("result"), "entry_id", None)
    if kind == "abort" and result.get("reason") in ("already_configured", "replicas_moved"):
        return str(result["reason"]), None
    return "error", None


def _by_agent_id(hass: HomeAssistant, agent_id: Any):
    wanted = norm_agent_id(agent_id)
    if not wanted:
        return None
    return next((e for e in entries(hass) if norm_agent_id(e.data.get(ENTRY_AGENT_ID)) == wanted),
                None)


def _by_address(hass: HomeAssistant, address: str, port: int):
    for entry in entries(hass):
        entry_port = agent_port(entry.data)
        if entry_port is None:
            continue  # the Replica
        if entry_port == int(port) and norm_host(agent_address(entry.data)) == norm_host(address):
            return entry
    return None


async def _by_resolved_address(hass: HomeAssistant, address: str, port: int):
    """An entry on the same port whose host name and this one resolve to a
    common address (the config flow's _entry_at rule)."""
    loop = asyncio.get_running_loop()

    async def resolve(name: str) -> set[str]:
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(name, None), timeout=3)
        except (OSError, UnicodeError, asyncio.TimeoutError, TimeoutError):
            return set()
        return {str(i[4][0]) for i in infos}

    wanted = await resolve(address)
    if not wanted:
        return None
    for entry in entries(hass):
        entry_port = agent_port(entry.data)
        if entry_port != int(port):
            continue
        if await resolve(str(agent_address(entry.data))) & wanted:
            return entry
    return None


def _app_agent(hass: HomeAssistant):
    from .panel_ws import haos_entry  # noqa: PLC0415

    return next((e for e in entries(hass) if entry_kind(e) == "haos"), None) or haos_entry(hass)


def _match(hass: HomeAssistant, item: dict[str, Any], health: dict[str, Any] | None):
    """agent_id (the App's, then the one the agent answers), then the App's
    own agent, then the address."""
    for agent_id in (item.get("agent_id"), (health or {}).get("agent_id")):
        found = _by_agent_id(hass, agent_id)
        if found is not None:
            return found
    if APP_HOST.match(norm_host(item["address"])):
        found = _app_agent(hass)
        if found is not None:
            return found
    return _by_address(hass, item["address"], item["port"])


# ── Settings, gaps only ──────────────────────────────────────────────────────


async def _known_cards(hass: HomeAssistant, entry) -> tuple[list[dict[str, Any]], bool]:
    """(cards, read): the machine's drive, pool and filesystem cards from its
    read this run or its last good one; read False when there is neither."""
    response = fresh_response(hass, entry)
    if response is None:
        response = await panel_data(hass).views.async_get(entry.entry_id)
    if not isinstance(response, dict) or response.get("items") is None:
        return [], False
    return card_list(response), True


def _counts() -> dict[str, int]:
    return {"names": 0, "hidden": 0, "thresholds": 0}


async def async_fill(hass: HomeAssistant, entry, cards: dict[str, Any], *,
                     name: str | None = None, host_hidden: bool = False,
                     removed: list[str] | None = None) -> tuple[dict[str, int], dict[str, Any]]:
    """Fill this machine's gaps from the App's settings: (counts filled,
    what waits for the machine's drives). Never overwrites a setting the
    integration has."""
    eid = entry.entry_id
    counts = _counts()
    waiting: dict[str, Any] = {"names": {}, "thresholds": {}, "host_hidden": False}
    pd = panel_data(hass)
    await pd.state.async_load()
    known, read = await _known_cards(hass, entry)
    known_keys = [split_card_id(c["card_id"])[1] for c in known]
    drive_ids = {split_card_id(c["card_id"])[1]: str(c.get("id")) for c in known
                 if c.get("type") == "drive" and c.get("id") is not None}
    target = ha_target(hass, entry)
    registry = dr.async_get(hass)
    fixed = host_object(hass, entry)["fixed"]

    def change(state: dict[str, Any]) -> None:
        if name and not fixed and eid not in state["machine_names"]:
            state["machine_names"][eid] = clean_name(name)
            counts["names"] += 1
        for key in removed or []:
            cid = f"{eid}|{key}"
            if cid not in state["removed"]:
                state["removed"][cid] = utc_text()
        hidden_keys = {k for k, c in cards.items() if c.get("hidden") is True}
        if host_hidden:
            hidden_keys |= set(known_keys)
        for key in sorted(hidden_keys):
            cid = f"{eid}|{key}"
            if cid not in state["hidden"]:
                state["hidden"][cid] = True
                counts["hidden"] += 1
        for key, card in cards.items():
            label = clean_name(card.get("name") or "")
            if label and key.startswith("fs:"):
                cid = f"{eid}|{key}"
                if cid not in state["card_names"]:
                    state["card_names"][cid] = label
                    counts["names"] += 1

    if host_hidden and not read:
        waiting["host_hidden"] = True
    try:
        await pd.state.async_change(change)
    except PanelStateNewer:
        pass
    # A drive's or pool's name is its device's name in Home Assistant.
    for key, card in cards.items():
        label = clean_name(card.get("name") or "")
        if not label or key.startswith("fs:"):
            continue
        device_id = (target.get("devices") or {}).get(key)
        device = registry.async_get(device_id) if device_id else None
        if device is None:
            waiting["names"][key] = label
            continue
        if device.name_by_user is None:
            registry.async_update_device(device.id, name_by_user=label)
            counts["names"] += 1
    # Thresholds are kept by drive id in the entry.
    stored = {**(entry.data.get("thresholds") or {})}
    wrote = False
    for key, card in cards.items():
        values = clean_overrides(card.get("thresholds"))
        if not values:
            continue
        drive_id = key[3:] if key.startswith("id:") else drive_ids.get(key)
        if drive_id is None:
            waiting["thresholds"][key] = values
            continue
        if drive_id not in stored:
            stored[drive_id] = values
            counts["thresholds"] += 1
            wrote = True
    if wrote:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "thresholds": stored})
        async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, eid)
    return counts, waiting


def _waiting_cards(waiting: dict[str, Any]) -> dict[str, Any]:
    cards: dict[str, Any] = {}
    for key, label in (waiting.get("names") or {}).items():
        cards.setdefault(key, {})["name"] = label
    for key, values in (waiting.get("thresholds") or {}).items():
        cards.setdefault(key, {})["thresholds"] = values
    return cards


def _has_waiting(waiting: dict[str, Any]) -> bool:
    return bool(waiting.get("names") or waiting.get("thresholds") or waiting.get("host_hidden"))


def _add_counts(row: dict[str, Any], counts: dict[str, int]) -> None:
    total = row.setdefault("counts", _counts())
    for key, value in counts.items():
        total[key] = int(total.get(key) or 0) + int(value)


async def _async_apply_waiting(hass: HomeAssistant, entry_id: str) -> None:
    """A machine was read: fill what waited for its drives."""
    ai = app_import(hass)
    data = await ai.async_load()
    if ai.read_only:
        return
    async with ai.lock:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            return
        changed = False
        pending_app = data.get("waiting_app")
        if isinstance(pending_app, dict) and entry_kind(entry) == "haos":
            data["waiting_app"] = None
            counts, waiting = await async_fill(hass, entry, pending_app.get("cards") or {})
            row = data["ledger"].get(pending_app.get("item_id") or "self")
            if row is not None:
                row["entry_id"] = entry.entry_id
                row["outcome"] = APPLIED
                _add_counts(row, counts)
            if _has_waiting(waiting):
                data["waiting"][entry.entry_id] = {"item_id": pending_app.get("item_id"), **waiting}
            changed = True
        held = data["waiting"].get(entry_id)
        if isinstance(held, dict) and (await _known_cards(hass, entry))[1]:
            counts, waiting = await async_fill(
                hass, entry, _waiting_cards(held), host_hidden=bool(held.get("host_hidden")))
            row = data["ledger"].get(held.get("item_id") or "")
            if row is not None and any(counts.values()):
                _add_counts(row, counts)
                changed = True
            # What has no device or drive yet stays for a later read (devices
            # are made after the first poll; a drive may be asleep).
            left = {"names": waiting["names"], "thresholds": waiting["thresholds"],
                    "host_hidden": False}
            if not _has_waiting(left):
                data["waiting"].pop(entry_id, None)
                changed = True
            elif left != {k: held.get(k) for k in left}:
                data["waiting"][entry_id] = {"item_id": held.get("item_id"), **left}
                changed = True
        if changed:
            ai.save()


# ── The job ──────────────────────────────────────────────────────────────────


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """A ledger row or skip as answered to the App: no password, ever (none
    is kept)."""
    return {k: row[k] for k in ("outcome", "entry_id", "reason", "counts", "removed_at")
            if row.get(k) is not None}


async def async_run(hass: HomeAssistant, items: list[dict[str, Any]]) -> dict[str, Any]:
    """One handover: every item, then the report {items: {item_id:
    {outcome, ...}}, done: [...], skipped: [...]}."""
    ai = app_import(hass)
    data = await ai.async_load()
    if ai.read_only:
        return {"items": {i["item_id"]: {"outcome": SKIPPED, "reason": "store_unreadable"}
                          for i in items}, "error": "store_unreadable"}
    async with ai.lock:
        ledger = data["ledger"]
        out: dict[str, dict[str, Any]] = {}
        hosts: dict[str, str] = {}  # the App's host key -> entry id
        for row in ledger.values():
            if row.get("host_key") and row.get("entry_id"):
                hosts[row["host_key"]] = row["entry_id"]
        todo: list[dict[str, Any]] = []
        for item in items:
            row = ledger.get(item["item_id"])
            if row is not None and row.get("outcome") in FINAL:
                out[item["item_id"]] = {**_public(row), "already": True}
            else:
                todo.append(item)

        fields = [i for i in todo if i["kind"] == "field"]
        sem = asyncio.Semaphore(PROBES)

        async def probe(item: dict[str, Any]) -> dict[str, Any] | None:
            if _match(hass, item, None) is not None:
                return None  # matched without asking the agent
            async with sem:
                return await async_health(hass, item["address"], item["port"])

        healths = await asyncio.gather(*(probe(i) for i in fields), return_exceptions=True)
        # One machine after the other: each flow's duplicate checks see the
        # machines made before it.
        for item, health in zip(fields, healths, strict=True):
            health = health if isinstance(health, dict) else None
            try:
                result = await _field(hass, data, item, health)
            except Exception:  # noqa: BLE001 - one machine never fails the others
                _LOGGER.exception("SMART Sniffer: moving %s from the App failed",
                                  host_key(item["address"], item["port"]))
                result = {"outcome": SKIPPED, "reason": "error"}
            _record(data, item, result)
            out[item["item_id"]] = result
            if result.get("entry_id"):
                hosts[host_key(item["address"], item["port"])] = result["entry_id"]

        for item in todo:
            if item["kind"] == "self":
                result = await _self(hass, data, item)
            elif item["kind"] == "replicas":
                result = {"outcome": REPLICAS_DROPPED}
            elif item["kind"] == "order":
                result = await _order(hass, item, hosts)
            else:
                continue
            _record(data, item, result)
            out[item["item_id"]] = result
        ai.save()
    return {"items": out,
            "done": sorted(k for k, v in out.items() if v.get("outcome") in FINAL),
            "skipped": sorted(k for k, v in out.items() if v.get("outcome") not in FINAL)}


def _record(data: dict[str, Any], item: dict[str, Any], result: dict[str, Any]) -> None:
    now = utc_text()
    if result.get("outcome") in FINAL:
        row = {"outcome": result["outcome"], "kind": item["kind"], "at": now,
               "entry_id": result.get("entry_id"), "counts": result.get("counts") or _counts()}
        if item["kind"] == "field":
            row["host_key"] = host_key(item["address"], item["port"])
        data["ledger"][item["item_id"]] = row
        data["skipped"].pop(item["item_id"], None)
        return
    if item["kind"] == "field":
        first = (data["skipped"].get(item["item_id"]) or {}).get("at") or now
        data["skipped"][item["item_id"]] = {
            "address": item["address"], "port": item["port"],
            "reason": result.get("reason") or "error", "at": first}


async def _field(hass: HomeAssistant, data: dict[str, Any], item: dict[str, Any],
                 health: dict[str, Any] | None) -> dict[str, Any]:
    address, port = item["address"].strip(), item["port"]
    if not address or any(c in address for c in "/?#@ \t"):
        return {"outcome": SKIPPED, "reason": "bad_address"}
    entry = _match(hass, item, health)
    outcome = MATCHED
    if entry is None:
        made, entry_id = await async_create(hass, address, port, item.get("password") or "")
        if made == "replicas_moved":
            return {"outcome": REPLICAS_DROPPED}
        if made == "created" and entry_id:
            entry = hass.config_entries.async_get_entry(entry_id)
            outcome = CREATED if health is not None else CREATED_OFFLINE
        elif made == "already_configured":
            # The same agent under another address or name: the flow knew it.
            entry = (_match(hass, item, health)
                     or await _by_resolved_address(hass, address, port)
                     or _by_hostname(hass, (health or {}).get("hostname"), port))
        if entry is None:
            return {"outcome": SKIPPED, "reason": "not_added" if made != "error" else "error"}
    elif (item.get("password") and not entry.data.get(CONF_TOKEN)
          and _match(hass, item, None) is entry):
        # The machine is here without its password: the App's fills the gap,
        # only when the App's own record names this machine (its agent_id,
        # the App's own agent, or its address). An agent_id read from
        # /api/health needs no password and is broadcast in mDNS: whatever
        # answers at the App's address could claim another machine's id.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: item["password"]})
    counts, waiting = await async_fill(
        hass, entry, item.get("cards") or {}, name=item.get("name"),
        host_hidden=bool(item.get("hidden")), removed=item.get("removed") or [])
    if _has_waiting(waiting):
        data["waiting"][entry.entry_id] = {"item_id": item["item_id"], **waiting}
    return {"outcome": outcome, "entry_id": entry.entry_id, "counts": counts}


def _by_hostname(hass: HomeAssistant, hostname: Any, port: int):
    want = norm_host(hostname)
    if not want:
        return None
    found = [e for e in entries(hass) if agent_port(e.data) == int(port)
             and norm_host(e.data.get("agent_hostname")) == want]
    return found[0] if len(found) == 1 else None


async def _self(hass: HomeAssistant, data: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """This box's own cards (HAOS): filled now when its agent is a machine
    here, else kept until it is."""
    cards = item.get("cards") or {}
    entry = _app_agent(hass)
    if entry is None:
        data["waiting_app"] = {"item_id": item["item_id"], "cards": cards}
        return {"outcome": PENDING}
    counts, waiting = await async_fill(hass, entry, cards)
    if _has_waiting(waiting):
        data["waiting"][entry.entry_id] = {"item_id": item["item_id"], **waiting}
    return {"outcome": APPLIED, "entry_id": entry.entry_id, "counts": counts}


async def _order(hass: HomeAssistant, item: dict[str, Any], hosts: dict[str, str]) -> dict[str, Any]:
    """The App's card order, when nobody ordered the cards here by hand."""
    pd = panel_data(hass)
    state = await pd.state.async_load()
    if state.get("order_set"):
        return {"outcome": KEPT}
    app_entry = _app_agent(hass)
    if app_entry is not None:
        hosts = {**hosts, "self": app_entry.entry_id}
    known = {e.entry_id for e in entries(hass)}
    wanted: list[str] = []
    for cid in item["order"]:
        key, card = split_card_id(cid)
        entry_id = hosts.get(str(key).lower()) if key else None
        if entry_id in known and card:
            mapped = f"{entry_id}|{card}"
            if mapped not in wanted:
                wanted.append(mapped)
    if not wanted:
        return {"outcome": APPLIED}

    def change(data: dict[str, Any]) -> None:
        kept = [i for i in data["order"] if i not in wanted]
        data["order"] = wanted + kept
        data["order_set"] = True

    try:
        await pd.state.async_change(change)
    except PanelStateNewer:
        return {"outcome": SKIPPED, "reason": "panel_state_newer"}
    return {"outcome": APPLIED}


async def async_forget_entry(hass: HomeAssistant, entry_id: str) -> None:
    """A machine is deleted: its ledger row says so (never made again), and
    what waited for it goes."""
    ai = app_import(hass)
    data = await ai.async_load()
    if ai.read_only:
        return
    changed = data["waiting"].pop(entry_id, None) is not None
    for row in data["ledger"].values():
        if row.get("entry_id") == entry_id and not row.get("removed_at"):
            row["removed_at"] = utc_text()
            changed = True
    if changed:
        ai.save()


# ── The report line in the panel ─────────────────────────────────────────────


def page_report(hass: HomeAssistant) -> dict[str, Any] | None:
    """What the panel's one-time report line says, while not dismissed;
    None when there is nothing to say (or the Store is not read yet)."""
    ai = hass.data.get(DATA)
    data = getattr(ai, "data", None)
    if not isinstance(data, dict):
        return None
    since = data.get("dismissed_at") or ""
    moved = here = 0
    counts = _counts()
    for row in data["ledger"].values():
        if str(row.get("at") or "") <= since:
            continue
        if row.get("outcome") in (CREATED, CREATED_OFFLINE):
            moved += 1
        elif row.get("outcome") == MATCHED:
            here += 1
        for key in counts:
            counts[key] += int((row.get("counts") or {}).get(key) or 0)
    skipped = [{"item_id": item_id, "address": s.get("address"), "port": s.get("port"),
                "reason": s.get("reason")}
               for item_id, s in sorted(data["skipped"].items())
               if str(s.get("at") or "") > since]
    if not (moved or here or skipped or any(counts.values())):
        return None
    return {"machines": moved, "already_here": here, **counts, "skipped": skipped}


async def async_page_report(hass: HomeAssistant) -> dict[str, Any] | None:
    await app_import(hass).async_load()
    return page_report(hass)


# ── Websocket commands ───────────────────────────────────────────────────────


def _new_job_id() -> str:
    return os.urandom(8).hex()


@websocket_api.websocket_command({
    vol.Required("type"): "smart_sniffer/app/import",
    vol.Required("items"): vol.All([ITEM], vol.Length(min=1, max=300)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_app_import(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Start a handover job; its id at once."""
    if not is_system_user(connection.user):
        connection.send_error(msg["id"], "unauthorized", "Only the SMART Sniffer App, through "
                                                         "the Supervisor")
        return
    ids = [i["item_id"] for i in msg["items"]]
    if len(set(ids)) != len(ids):
        connection.send_error(msg["id"], "invalid_format", "item ids must be unique")
        return
    ai = app_import(hass)
    job_id = _new_job_id()
    job: dict[str, Any] = {"state": "running"}
    ai.jobs[job_id] = job
    while len(ai.jobs) > JOBS_KEPT:
        ai.jobs.pop(next(iter(ai.jobs)))

    async def run() -> None:
        try:
            job["report"] = await async_run(hass, msg["items"])
            job["state"] = "done"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("SMART Sniffer: the App handover failed")
            job["state"] = "failed"

    hass.async_create_background_task(run(), f"smart_sniffer app import {job_id}")
    connection.send_result(msg["id"], {"job_id": job_id})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_sniffer/app/import/result",
    vol.Required("job_id"): vol.All(cv.string, vol.Length(min=1, max=32)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_app_import_result(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if not is_system_user(connection.user):
        connection.send_error(msg["id"], "unauthorized", "Only the SMART Sniffer App, through "
                                                         "the Supervisor")
        return
    job = app_import(hass).jobs.get(msg["job_id"])
    if job is None:
        connection.send_error(msg["id"], "not_found", "no such job (Home Assistant restarted?)")
        return
    answer = {"job_id": msg["job_id"], "state": job["state"]}
    if "report" in job:
        answer["report"] = job["report"]
    connection.send_result(msg["id"], answer)


@websocket_api.websocket_command({vol.Required("type"): "smart_sniffer/panel/app_import/dismiss"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_import_dismiss(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    ai = app_import(hass)
    data = await ai.async_load()
    if not ai.read_only:
        async with ai.lock:
            data["dismissed_at"] = utc_text()
            ai.save()
    connection.send_result(msg["id"], {"dismissed": True})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_sniffer/panel/app_import/discard",
    vol.Required("item_id"): ITEM_ID,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_import_discard(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """A skipped machine the user lets go: the App clears it next time."""
    ai = app_import(hass)
    data = await ai.async_load()
    if ai.read_only:
        connection.send_error(msg["id"], "panel_state_newer", "not changed")
        return
    async with ai.lock:
        skipped = data["skipped"].pop(msg["item_id"], None)
        if skipped is None:
            connection.send_error(msg["id"], "not_found", "no such skipped machine")
            return
        data["ledger"][msg["item_id"]] = {
            "outcome": DISCARDED, "kind": "field", "at": utc_text(), "entry_id": None,
            "host_key": host_key(skipped.get("address") or "", skipped.get("port") or 0),
            "counts": _counts()}
        ai.save()
    connection.send_result(msg["id"], {"discarded": msg["item_id"]})


APP_COMMANDS = (ws_app_import, ws_app_import_result)
PANEL_COMMANDS = (ws_import_dismiss, ws_import_discard)


@callback
def async_register(hass: HomeAssistant) -> None:
    """The commands, and the listener that fills waiting settings when a
    machine is read."""
    for command in APP_COMMANDS + PANEL_COMMANDS:
        websocket_api.async_register_command(hass, command)
    async_listen(hass)


@callback
def async_listen(hass: HomeAssistant) -> None:
    """Fill waiting settings when a machine is read (every poll signals)."""

    @callback
    def on_update(entry_id: str) -> None:
        ai = app_import(hass)
        data = ai.data
        if data is not None and not data.get("waiting") and not data.get("waiting_app"):
            return
        hass.async_create_task(_async_apply_waiting(hass, entry_id))

    async_dispatcher_connect(hass, SIGNAL_PANEL_UPDATE, on_update)
