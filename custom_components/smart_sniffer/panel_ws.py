"""The SMART Sniffer panel's websocket commands (v0.9).

Every command is ``smart_sniffer/panel/<name>``, for admins only, with a
schema on every field. Card ids are "<entry_id>|<card key>" and are checked
against the user's entries before anything is written. No answer ever holds
an agent's password: machines carry has_password only.

Reads: info, cards (everything the page draws), machine (one), subscribe
(pushes: machine, machine_removed, state, info), agentic, card/file,
thresholds/get, net/list, net/check, net/scan/get.

Writes: card/hidden, cards/order, card/name (a drive or pool writes its
device's name_by_user; a filesystem card's name is kept in the panel
Store), machine/name (a panel label), card/remove (the page only, or with
its device deleted or disabled, or the machine's entry deleted),
thresholds/set (the entry's thresholds, applied without a reload),
machine/password (the entry's reauth flow), agent/add, note/dismiss,
refresh, net/scan and net/scan/cancel (the + card's block scan).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .attention import get_thresholds
from .agent_identity import ENTRY_AGENT_ID, norm_host
from .const import CONF_TOKEN, DOMAIN
from .device_link import device_entry_ids
from .panel_data import (
    VERSION,
    SIGNAL_PANEL_STATE,
    SIGNAL_PANEL_UPDATE,
    async_agentic,
    async_cards,
    async_load_import,
    async_machine,
    entries,
    entry_kind,
    fresh_response,
    host_object,
    page_info,
    page_state,
    panel_data,
    utc_text,
)
from . import panel_net
from .panel_net import ScanRefused, async_add, async_check, parse_block
from .panel_state import PanelStateNewer, clean_name
from .panel_view import (
    HA_REMOVAL_MODES,
    accept_value,
    card_list,
    clean_overrides,
    evaluate,
    split_card_id,
)
from .replica_file import file_name, replica_from_drive
from .source import agent_address, agent_port, is_replica_data

_LOGGER = logging.getLogger(__name__)

PREFIX = "smart_sniffer/panel"
PUSH_DELAY = 0.25  # seconds: updates that come together are sent together
CARD_ID = vol.All(cv.string, vol.Length(min=3, max=400))
ENTRY_ID = vol.All(cv.string, vol.Length(min=1, max=64))
NOTES = ("app_hint", "replicas_moved")


class _Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.source == "ignore":
        raise _Refused("not_found", "no such machine")
    return entry


def _card(hass: HomeAssistant, cid: str) -> tuple[ConfigEntry, str]:
    entry_id, key = split_card_id(cid)
    if not key:
        raise _Refused("not_found", "no such card")
    return _entry(hass, entry_id), key


def _command(schema: dict[Any, Any]):
    """A panel command: its schema, admin only, async, refusals as errors."""
    def wrap(func):
        @websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/{schema.pop('type')}",
                                          **schema})
        @websocket_api.require_admin
        @websocket_api.async_response
        async def handler(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
            try:
                result = await func(hass, msg)
            except _Refused as err:
                connection.send_error(msg["id"], err.code, err.message)
                return
            except PanelStateNewer:
                connection.send_error(
                    msg["id"], "panel_state_newer",
                    "The panel settings were saved by a newer SMART Sniffer; not changed.")
                return
            connection.send_result(msg["id"], result)

        handler.__name__ = func.__name__
        handler.panel_func = func  # for tests
        return handler
    return wrap


# ── Reads ──────────────────────────────────────────────────────────────────


@_command({"type": "info"})
async def ws_info(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    await panel_data(hass).state.async_load()
    await async_load_import(hass)
    return page_info(hass)


@_command({"type": "cards"})
async def ws_cards(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    return await async_cards(hass)


@_command({"type": "machine", vol.Required("entry_id"): ENTRY_ID})
async def ws_machine(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry = _entry(hass, msg["entry_id"])
    return {"machine": await async_machine(hass, entry), "entry_id": entry.entry_id}


@_command({"type": "refresh"})
async def ws_refresh(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Ask every loaded machine's agent again now (Refresh)."""
    await async_refresh_all(hass)
    return await async_cards(hass)


async def async_refresh_all(hass: HomeAssistant) -> None:
    """Every loaded machine's agent asked again, waited for."""
    loaded = hass.data.get(DOMAIN) or {}
    waits = []
    for data in list(loaded.values()):
        if not isinstance(data, dict):
            continue
        for key in ("coordinator", "health_coordinator"):
            request = getattr(data.get(key), "async_request_refresh", None)
            if request is not None:
                waits.append(request())
    if waits:
        await asyncio.gather(*waits, return_exceptions=True)


def agentic_text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def agentic_file_name(document: dict[str, Any]) -> str:
    """smart-sniffer-<UTC date>.json, the date the document was made."""
    return f"smart-sniffer-{document['generated_at'][:10]}.json"


@_command({"type": "agentic"})
async def ws_agentic(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    document = await async_agentic(hass)
    text = await hass.async_add_executor_job(agentic_text, document)
    counts = document["counts"]
    # The page's header line: the document's own counts, real agents apart
    # from the Replica.
    return {"text": text, "file": agentic_file_name(document),
            "generated_at": document["generated_at"], "agents": counts["agents"],
            "drives": counts["drives"], "pools": counts["pools"],
            "down": counts["unreachable_agents"], "counts": dict(counts)}


def _find_item(hass: HomeAssistant, entry: ConfigEntry, cid: str) -> dict[str, Any]:
    """A drive or pool card from the machine's read this run."""
    response = fresh_response(hass, entry)
    found = next((c for c in card_list(response or {}) if c["card_id"] == cid), None)
    if found is None:
        raise _Refused("not_answering", "The agent did not answer.")
    return found


@_command({"type": "card/file", vol.Required("card_id"): CARD_ID})
async def ws_card_file(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """A card as a file: a drive as a replica file (serial and WWN
    replaced), a pool as the agent reported it."""
    entry, _key = _card(hass, msg["card_id"])
    item = _find_item(hass, entry, msg["card_id"])
    if item.get("type") == "pool":
        pool = item.get("pool") or {}
        doc = {"pool": pool, "filesystems": item.get("filesystems") or []}
        return {"name": f"pool-{pool.get('name')}.json",
                "text": json.dumps(doc, indent=2) + "\n"}
    if item.get("type") != "drive":
        raise _Refused("not_found", "no such card")
    verdict = item.get("verdict") or {}
    if verdict.get("state") == "UNSUPPORTED" or "smart_data" not in (item.get("drive") or {}):
        raise _Refused("unsupported_drive", "This drive has no SMART data to copy.")
    file_verdict = {"state": verdict.get("state"), "severity": verdict.get("severity"),
                    "reasons": verdict.get("reasons"),
                    "rules_version": verdict.get("rules_version"), "at": utc_text()}
    try:
        doc = await hass.async_add_executor_job(replica_from_drive, item["drive"], file_verdict)
    except Exception as err:  # noqa: BLE001 - BadRequest or InvalidReplica
        raise _Refused("unsupported_drive", "This drive has no SMART data to copy.") from err
    return {"name": file_name(doc), "text": json.dumps(doc, indent=2, ensure_ascii=False) + "\n"}


def _threshold_item(hass: HomeAssistant, cid: str) -> tuple[ConfigEntry, dict[str, Any]]:
    entry, _key = _card(hass, cid)
    item = _find_item(hass, entry, cid)
    if item.get("type") != "drive" or "smart_data" not in (item.get("drive") or {}):
        raise _Refused("not_found", "no such drive")
    return entry, item


def _limits(hass: HomeAssistant, entry: ConfigEntry, item: dict[str, Any],
            overrides: dict[str, int]) -> list[dict[str, Any]]:
    coordinator = ((hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}).get("coordinator")
    payload = (getattr(coordinator, "data", None) or {}).get(item["id"]) or item["drive"]
    return evaluate(dict(payload), overrides)["limits"]


@_command({"type": "thresholds/get", vol.Required("card_id"): CARD_ID})
async def ws_thresholds_get(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry, item = _threshold_item(hass, msg["card_id"])
    overrides = clean_overrides(get_thresholds(entry, item["id"]))
    return {"card_id": msg["card_id"], "source": "integration",
            "labels": _limits(hass, entry, item, overrides)}


@_command({"type": "net/list"})
async def ws_net_list(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The + card's Network list: every agent Home Assistant's zeroconf sees
    (added ones marked), the discoveries waiting, and the Scan's default
    block."""
    return {"found": await network_rows(hass),
            "network": await panel_net.async_network_info(hass)}


async def network_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return await panel_net.async_network_rows(hass)


@_command({"type": "net/scan",
           vol.Required("block"): vol.All(cv.string, vol.Length(min=1, max=64)),
           vol.Required("port"): cv.port})
async def ws_net_scan(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Start the block scan (one at a time; it cancels the last)."""
    try:
        scan = await panel_net.scans(hass).async_start(parse_block(msg["block"]), msg["port"])
    except ScanRefused as err:
        if err.code == "too_wide":
            # The page offers the suggested block.
            return {"error": "too_wide", **err.extra}
        raise _Refused(err.code, err.message) from err
    return {"scan_id": scan.id, "total": scan.total}


@_command({"type": "net/scan/get", vol.Required("scan_id"): vol.All(cv.string, vol.Length(max=32))})
async def ws_net_scan_get(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    scan = panel_net.scans(hass).get(msg["scan_id"])
    if scan is None:
        raise _Refused("not_found", "no such scan, or it has expired")
    return scan.snapshot()


@_command({"type": "net/scan/cancel",
           vol.Required("scan_id"): vol.All(cv.string, vol.Length(max=32))})
async def ws_net_scan_cancel(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    if panel_net.scans(hass).cancel(msg["scan_id"]) is None:
        raise _Refused("not_found", "no such scan, or it has expired")
    return {"state": "cancelled"}


@_command({"type": "net/check", vol.Required("host"): vol.All(cv.string, vol.Length(min=1, max=253)),
           vol.Required("port"): cv.port})
async def ws_net_check(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    host = msg["host"].strip()
    if not host or any(c in host for c in "/?#@ \t"):
        raise _Refused("not_answering", "not a host")
    row = await async_check(hass, host, msg["port"])
    if "error" in row:
        raise _Refused(row["error"], row["error"])
    return row


# ── Writes ─────────────────────────────────────────────────────────────────


@_command({"type": "card/hidden", vol.Required("card_id"): CARD_ID,
           vol.Required("hidden"): bool})
async def ws_card_hidden(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    cid = msg["card_id"]
    _card(hass, cid)

    def change(state: dict[str, Any]) -> None:
        state["hidden"][cid] = msg["hidden"]

    state = await panel_data(hass).state.async_change(change)
    return {"id": cid, "hidden": msg["hidden"],
            "hidden_cards": [c for c, h in state["hidden"].items() if h]}


@_command({"type": "cards/order",
           vol.Required("order"): vol.All([CARD_ID], vol.Length(max=2000))})
async def ws_cards_order(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    known = {e.entry_id for e in entries(hass)}
    wanted = []
    for cid in msg["order"]:
        if split_card_id(cid)[0] in known and cid not in wanted:
            wanted.append(cid)

    def change(state: dict[str, Any]) -> None:
        # Cards the page did not send keep their place after the ones it did,
        # while their machine is there.
        kept = [i for i in state["order"] if i not in wanted and split_card_id(i)[0] in known]
        state["order"] = wanted + kept
        state["order_set"] = True

    state = await panel_data(hass).state.async_change(change)
    return {"order": state["order"]}


@_command({"type": "card/name", vol.Required("card_id"): CARD_ID,
           vol.Required("name"): vol.All(cv.string, vol.Length(max=400))})
async def ws_card_name(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """A drive or pool card's name is its device's name in Home Assistant
    (name_by_user); a filesystem card's is kept by the panel."""
    cid = msg["card_id"]
    entry, key = _card(hass, cid)
    name = clean_name(msg["name"])
    machine = await async_machine(hass, entry)
    device_id = ((machine or {}).get("ha") or {}).get("target", {}).get("devices", {}).get(key)
    if device_id is not None and not key.startswith("fs:"):
        registry = dr.async_get(hass)
        try:
            registry.async_update_device(device_id, name_by_user=name or None)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("SMART Sniffer panel: renaming device %s failed", device_id)
            return {"id": cid, "name": name, "home_assistant": "failed"}
        return {"id": cid, "name": name, "home_assistant": "saved"}

    def change(state: dict[str, Any]) -> None:
        if name:
            state["card_names"][cid] = name
        else:
            state["card_names"].pop(cid, None)

    await panel_data(hass).state.async_change(change)
    return {"id": cid, "name": name, "home_assistant": None}


@_command({"type": "machine/name", vol.Required("entry_id"): ENTRY_ID,
           vol.Required("name"): vol.All(cv.string, vol.Length(max=400))})
async def ws_machine_name(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry = _entry(hass, msg["entry_id"])
    if host_object(hass, entry)["fixed"]:
        raise _Refused("fixed_name", "HAOS and REPLICA keep their names")
    name = clean_name(msg["name"])

    def change(state: dict[str, Any]) -> None:
        if name:
            state["machine_names"][entry.entry_id] = name
        else:
            state["machine_names"].pop(entry.entry_id, None)

    await panel_data(hass).state.async_change(change)
    return {"id": entry.entry_id, "name": name}


@_command({"type": "card/remove", vol.Required("card_id"): CARD_ID,
           vol.Optional("ha"): vol.In(HA_REMOVAL_MODES)})
async def ws_card_remove(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The trash: the card leaves the page (kept in the panel Store), and
    with ``ha`` its device is deleted or disabled, or for the machine's last
    card its entry is deleted."""
    cid, mode = msg["card_id"], msg.get("ha")
    entry, key = _card(hass, cid)
    if host_object(hass, entry)["kind"] == "haos":
        raise _Refused("cannot_remove", "A drive of this machine can be hidden, not removed.")
    if is_replica_data(entry.data):
        # A Replica card: its pick goes (its file stays), with its device.
        from .panel_replica import ws_replica_remove  # noqa: PLC0415

        return await ws_replica_remove.panel_func(hass, {"card_id": cid})
    pd = panel_data(hass)
    machine = await async_machine(hass, entry)
    if machine is None:
        raise _Refused("not_found", "no such card")
    target = machine["ha"]["target"]
    shown = card_list(machine["data"])
    if cid not in {c["card_id"] for c in shown}:
        raise _Refused("not_found", "no such card")
    machine_gone = len(shown) == 1

    def change(state: dict[str, Any]) -> None:
        state["removed"][cid] = utc_text()
        state["order"] = [i for i in state["order"] if i != cid]
        state["hidden"].pop(cid, None)
        state["card_names"].pop(cid, None)

    await pd.state.async_change(change)
    answer: dict[str, Any] = {"removed": cid, "host_removed": machine_gone}
    if mode is None:
        return answer
    reason = await _ha_remove(hass, entry, mode, target, key, machine_gone)
    answer["home_assistant"] = "failed" if reason else ("disabled" if mode == "disable" else "deleted")
    answer["ha_error"] = reason
    return answer


async def _ha_remove(hass: HomeAssistant, entry: ConfigEntry, mode: str,
                     target: dict[str, Any], key: str, machine_gone: bool) -> str | None:
    """The Home Assistant side of a removal. None when done, else why not."""
    if mode == "entry":
        if not machine_gone:
            return "other cards of this machine are still on the page"
        await hass.config_entries.async_remove(entry.entry_id)
        return None
    device_id = (target.get("devices") or {}).get(key)
    registry = dr.async_get(hass)
    device = registry.async_get(device_id) if device_id else None
    if device is None:
        return "the card has no device in Home Assistant"
    if mode == "disable":
        registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
        return None
    # The integration's own rule for deleting a device, as Home Assistant's
    # device page asks it (device_removal.py): never a drive still reported.
    check = getattr(sys.modules.get(__package__), "async_remove_config_entry_device", None)
    if check is None:
        return "the integration refused"
    try:
        allowed = await check(hass, entry, device)
    except HomeAssistantError as err:
        return str(err) or "the integration refused"
    if not allowed:
        return "the integration refused"
    registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    return None


@_command({"type": "thresholds/set", vol.Required("card_id"): CARD_ID,
           vol.Required("values"): vol.Schema({cv.string: vol.All(vol.Coerce(int),
                                                                   vol.Range(min=0))})})
async def ws_thresholds_set(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Save a drive's thresholds into its entry (only values that differ
    from the default), read-modify-write of that drive alone. The entry's
    update listener refreshes it without a reload (v0.8.5)."""
    cid = msg["card_id"]
    entry, item = _threshold_item(hass, cid)
    drive_id = str(item["id"])
    overrides = clean_overrides(get_thresholds(entry, drive_id))
    limits = _limits(hass, entry, item, overrides)
    by_label = {lim["label"]: lim for lim in limits}
    unknown = [label for label in msg["values"] if label not in by_label]
    if unknown:
        raise _Refused("bad_request", f"this drive has no threshold {unknown[0]}")
    wanted = {**overrides, **{label: int(v) for label, v in msg["values"].items()}}
    wanted = {label: value for label, value in wanted.items()
              if label in by_label and value != by_label[label]["default"]}
    data = {**entry.data}
    thresholds = {**(data.get("thresholds") or {})}
    if wanted:
        thresholds[drive_id] = wanted
    else:
        thresholds.pop(drive_id, None)
    data["thresholds"] = thresholds
    if data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=data)
    async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, entry.entry_id)
    return {"card_id": cid, "source": "integration",
            "labels": _limits(hass, entry, item, wanted)}


@_command({"type": "thresholds/accept", vol.Required("card_id"): CARD_ID})
async def ws_thresholds_accept(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry, item = _threshold_item(hass, msg["card_id"])
    overrides = clean_overrides(get_thresholds(entry, item["id"]))
    flagged = [lim for lim in _limits(hass, entry, item, overrides) if lim["flagged"]]
    if not flagged:
        raise _Refused("nothing_flagged", "no reading of this drive is flagged")
    values = {lim["label"]: accept_value(int(lim["reading"]), lim["compare"]) for lim in flagged}
    return await ws_thresholds_set.panel_func(hass, {"card_id": msg["card_id"], "values": values})


@_command({"type": "machine/password", vol.Required("entry_id"): ENTRY_ID,
           vol.Required("password"): vol.All(cv.string, vol.Length(min=1, max=1000))})
async def ws_machine_password(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The machine's password, through its entry's reauth flow: checked
    against the agent, then kept in the entry. Never sent back."""
    entry = _entry(hass, msg["entry_id"])
    result = await _reauth(hass, entry, msg["password"])
    if result == "ok":
        async_dispatcher_send(hass, SIGNAL_PANEL_UPDATE, entry.entry_id)
        return {"host": host_object(hass, entry)}
    if result == "invalid_auth":
        raise _Refused("password_refused", "That password was refused.")
    if result == "cannot_connect":
        raise _Refused("not_answering", "That agent is not answering.")
    raise _Refused("error", "The password was not saved.")


async def _reauth(hass: HomeAssistant, entry: ConfigEntry, password: str) -> str:
    """The entry's reauth flow (Home Assistant's own, or one started here)
    given this password: ok, invalid_auth, cannot_connect or error."""
    flows = hass.config_entries.flow
    active = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    if active:
        flow_id = active[0]["flow_id"]
    else:
        started = await flows.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id,
                     "title_placeholders": {"name": entry.title}, "unique_id": entry.unique_id},
            data=dict(entry.data),
        )
        flow_id = started.get("flow_id")
        if not flow_id:
            return "error"
    result = await flows.async_configure(flow_id, {CONF_TOKEN: password})
    kind = getattr(result.get("type"), "value", result.get("type"))
    if kind == "abort" and result.get("reason") == "reauth_successful":
        return "ok"
    return (result.get("errors") or {}).get("base") or "error"


ROW = vol.Schema({
    vol.Required("address"): vol.All(cv.string, vol.Length(min=1, max=253)),
    vol.Required("port"): cv.port,
    vol.Optional("flow_id"): vol.Any(None, cv.string),
    # The drive count the row shows (scan, Check or discovery), if any.
    vol.Optional("drives"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=10000))),
}, extra=vol.REMOVE_EXTRA)


@_command({"type": "agent/add", vol.Required("hosts"): vol.All([ROW], vol.Length(min=1, max=64))})
async def ws_agent_add(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    results = await async_add(hass, msg["hosts"])
    return {"results": results,
            "added": [r for r in results if r.get("result") in ("created", "restored")]}


@_command({"type": "note/dismiss", vol.Required("note"): vol.In(NOTES)})
async def ws_note_dismiss(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    def change(state: dict[str, Any]) -> None:
        state["notes"][msg["note"]] = True

    await panel_data(hass).state.async_change(change)
    return {"note": msg["note"], "dismissed": True}


# ── For the SMART Sniffer App ──────────────────────────────────────────────


def haos_entry(hass: HomeAssistant, agent_id: str | None = None, host: str | None = None,
               port: int | None = None) -> ConfigEntry | None:
    """The entry that is the SMART Sniffer App's agent, matched in this
    order: the agent_id the App reports, an entry that is the App's agent
    (is_app_entry, not its replica), the address."""
    candidates = entries(hass)
    if agent_id:
        found = next((e for e in candidates if e.data.get(ENTRY_AGENT_ID) == agent_id), None)
        if found is not None:
            return found
    found = next((e for e in candidates if entry_kind(e) == "haos"), None)
    if found is not None:
        return found
    if host:
        for entry in candidates:
            entry_port = agent_port(entry.data)
            if entry_port is None:
                continue  # the Replica, or no port
            if norm_host(agent_address(entry.data)) == norm_host(host) and (
                    port is None or entry_port == int(port)):
                return entry
    return None


@websocket_api.websocket_command({
    vol.Required("type"): "smart_sniffer/info",
    vol.Optional("agent_id"): vol.All(cv.string, vol.Length(max=64)),
    vol.Optional("host"): vol.All(cv.string, vol.Length(max=253)),
    vol.Optional("port"): cv.port,
})
@websocket_api.require_admin
@callback
def ws_smart_sniffer_info(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """What the SMART Sniffer App asks over the Supervisor's Core websocket
    (the Supervisor user is an admin): the integration version, whether the
    panel is up and where, and whether this box's agent is a machine here.
    No secrets."""
    from .panel_setup import PANEL_SETUP, PANEL_URL_PATH  # noqa: PLC0415

    connection.send_result(msg["id"], {
        "version": VERSION,
        "panel": bool(hass.data.get(PANEL_SETUP)),
        "panel_path": f"/{PANEL_URL_PATH}",
        "haos_entry": haos_entry(hass, msg.get("agent_id"), msg.get("host"),
                                 msg.get("port")) is not None,
        # The App's field machines can be handed over (panel_import.py),
        # version 1 of that exchange.
        "app_import": 1,
    })


# ── Live updates ───────────────────────────────────────────────────────────


@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/subscribe"})
@websocket_api.require_admin
@callback
def ws_subscribe(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Pushes for the open page: a machine's new read, a machine gone, the
    panel Store changed. Listens to signals only; everything it listens to
    is let go when the page unsubscribes or the socket closes."""
    msg_id = msg["id"]
    pending: set[str] = set()
    extra: dict[str, bool] = {"state": False, "info": False}
    timer: dict[str, Any] = {"cancel": None}
    closed = {"done": False}

    def send(payload: dict[str, Any]) -> None:
        if not closed["done"]:
            connection.send_message(websocket_api.event_message(msg_id, payload))

    async def flush() -> None:
        ids, pending_state, pending_info = set(pending), extra["state"], extra["info"]
        pending.clear()
        extra["state"] = extra["info"] = False
        pd = panel_data(hass)
        if pending_state:
            send({"type": "state", "state": page_state(pd)})
        if pending_info or pending_state:
            send({"type": "info", "info": page_info(hass)})
        for entry_id in ids:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                send({"type": "machine_removed", "entry_id": entry_id})
                continue
            try:
                machine = await async_machine(hass, entry)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("SMART Sniffer panel: push for %s failed", entry_id)
                continue
            if machine is None:
                send({"type": "machine_removed", "entry_id": entry_id})
            else:
                send({"type": "machine", "machine": machine})

    @callback
    def fire(_now: Any = None) -> None:
        timer["cancel"] = None
        if not closed["done"]:
            hass.async_create_task(flush())

    @callback
    def later() -> None:
        if timer["cancel"] is None and not closed["done"]:
            timer["cancel"] = async_call_later(hass, PUSH_DELAY, fire)

    @callback
    def on_update(entry_id: str) -> None:
        pending.add(entry_id)
        later()

    @callback
    def on_state() -> None:
        extra["state"] = True
        pending.update(e.entry_id for e in entries(hass))
        later()

    @callback
    def on_entry(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != DOMAIN:
            return
        pending.add(entry.entry_id)
        if change in (ConfigEntryChange.ADDED, ConfigEntryChange.REMOVED):
            extra["info"] = True
        later()

    @callback
    def on_device(event: Any) -> None:
        device_id = event.data.get("device_id")
        device = dr.async_get(hass).async_get(device_id) if device_id else None
        ours = {e.entry_id for e in entries(hass)}
        if device is None:
            pending.update(ours)
        else:
            pending.update(device_entry_ids(device) & ours)
        if pending:
            later()

    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_PANEL_UPDATE, on_update),
        async_dispatcher_connect(hass, SIGNAL_PANEL_STATE, on_state),
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, on_entry),
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, on_device),
    ]

    @callback
    def unsubscribe() -> None:
        closed["done"] = True
        if timer["cancel"] is not None:
            timer["cancel"]()
            timer["cancel"] = None
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)


COMMANDS = (
    ws_info, ws_cards, ws_machine, ws_refresh, ws_agentic, ws_card_file, ws_thresholds_get,
    ws_net_list, ws_net_check, ws_net_scan, ws_net_scan_get, ws_net_scan_cancel, ws_card_hidden, ws_cards_order, ws_card_name, ws_machine_name,
    ws_card_remove, ws_thresholds_set, ws_thresholds_accept, ws_machine_password,
    ws_agent_add, ws_note_dismiss, ws_subscribe,
)

# The Replica's commands (panel_replica.py), which use the helpers above.
from .panel_replica import REPLICA_COMMANDS  # noqa: E402

COMMANDS = COMMANDS + REPLICA_COMMANDS


# ── Agentic access over HTTP, for scripts and AI tools ─────────────────────

AGENTIC_URL = "/api/smart_sniffer/agentic.json"


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


async def async_agentic_http(hass: HomeAssistant, user: Any,
                             query: Any) -> tuple[int, str, dict[str, str]]:
    """(HTTP status, body, headers) of one GET of the raw Agentic access
    JSON by this user: the panel's document, for admins only. ?fresh=1 asks
    every agent first (the page's Refresh); ?download=1 adds a file name."""
    if user is None or not getattr(user, "is_admin", False):
        body = json.dumps({"error": "unauthorized", "message": "Admins only."})
        return 403, body + "\n", {}
    if _flag(query.get("fresh")):
        await async_refresh_all(hass)
    document = await async_agentic(hass)
    text = await hass.async_add_executor_job(agentic_text, document)
    headers = {"Cache-Control": "no-store"}
    if _flag(query.get("download")):
        headers["Content-Disposition"] = f'attachment; filename="{agentic_file_name(document)}"'
    return 200, text, headers


def register_agentic_view(hass: HomeAssistant) -> None:
    from aiohttp import web  # noqa: PLC0415

    from homeassistant.components.http import HomeAssistantView  # noqa: PLC0415

    class AgenticView(HomeAssistantView):
        """GET the Agentic access JSON: Home Assistant's own auth (a login or
        a long-lived access token), admins only. No password is in it."""

        url = AGENTIC_URL
        name = "api:smart_sniffer:agentic"
        requires_auth = True

        async def get(self, request: Any) -> Any:
            status, body, headers = await async_agentic_http(
                hass, request.get("hass_user"), request.query)
            return web.Response(text=body, status=status, headers=headers,
                                content_type="application/json", charset="utf-8")

    hass.http.register_view(AgenticView())


@callback
def async_register_info(hass: HomeAssistant) -> None:
    """smart_sniffer/info, on its own: the App can ask even when the panel
    could not be set up (it then answers panel: false)."""
    websocket_api.async_register_command(hass, ws_smart_sniffer_info)
    # The App's handover (panel_import.py): beside info, so it never waits
    # for the panel.
    from . import panel_import  # noqa: PLC0415

    panel_import.async_register(hass)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in COMMANDS:
        websocket_api.async_register_command(hass, command)
    try:
        from .panel_replica import register_upload_view  # noqa: PLC0415

        register_upload_view(hass)
    except Exception:  # noqa: BLE001 - the page works without uploads
        _LOGGER.exception("SMART Sniffer panel: the replica upload could not be registered")
    try:
        register_agentic_view(hass)
    except Exception:  # noqa: BLE001 - the page has its own Agentic access
        _LOGGER.exception("SMART Sniffer panel: the raw Agentic access could not be registered")
