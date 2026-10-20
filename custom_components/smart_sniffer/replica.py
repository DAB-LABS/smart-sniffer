"""The Replica machine in Home Assistant (v0.9).

One Replica per Home Assistant: a config entry with ``kind: replica``
(source.py) whose drives and pools are replica files the user picked from
the library, served in process by replica_engine.py. Its sensors, alerts
and cards are those of any machine.

- The library: the seed set shipped with the integration
  (``custom_components/smart_sniffer/replicas``), the user's own folder
  (``<config>/smart_sniffer/replicas``: Save as replica and uploads land
  there) and, when Home Assistant can read it, the drop folder
  ``/share/smart-sniffer/replicas`` (read only).
- The picks: one Store per Replica entry (``smart_sniffer.replica.<entry
  id>``), ``{"picks": [...]}``. A pick references its file by id; the file
  is read again on every poll, so a hand edit shows on the next Refresh.
  The Store is written on the event loop only.
- ReplicaSource: what the coordinators read instead of HTTP.
- The old Replica machine of the SMART Sniffer App (an HTTP entry at the
  App's replica agent) is removed at start, with a one-time notification.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .agent_identity import ENTRY_AGENT_ID, ENTRY_AGENT_MOCK
from .const import DOMAIN
from .host_name import is_app_entry
from .replica_engine import (
    MAX_DRIVES,
    MAX_POOLS,
    SEED,
    SHARE,
    USER,
    ReplicaError,
    ReplicaLibrary,
    card_key,
    clean_picks,
    edit_document,
    new_pick,
    parse_document,
    pick_counts,
    pool_document,
    render,
    upload_origin,
)
from .replica_file import replica_from_drive
from .source import KIND_REPLICA, agent_port, is_replica_data

_LOGGER = logging.getLogger(__name__)

RUNTIME = "smart_sniffer_replica"
PICKS_STORE_PREFIX = "smart_sniffer.replica"
PICKS_STORE_VERSION = 1
PICKS_STORE_MINOR = 1
SEED_DIR = str(Path(__file__).parent / "replicas")
SHARE_DIR = "/share/smart-sniffer/replicas"
MOVED_NOTIFICATION = "smart_sniffer_replicas_moved"
MOVED_TEXT = "Replicas moved into the integration. Create them again from + > Replica."
APP_REPLICA_PORT = 9100  # the SMART Sniffer App's replica agent (App 0.3.x)
SNAPSHOT_TTL = 2.0  # seconds a render serves the requests of one poll


class _PicksStore(Store):
    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        if old_major_version > PICKS_STORE_VERSION:
            raise ValueError("the Replica's picks were saved by a newer SMART Sniffer")
        return old_data


class Picks:
    """One Replica entry's picks, loaded once."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.entry_id = entry_id
        self._store = _PicksStore(hass, PICKS_STORE_VERSION, f"{PICKS_STORE_PREFIX}.{entry_id}",
                                  minor_version=PICKS_STORE_MINOR)
        self.items: list[dict[str, Any]] = []
        self.loaded = False
        self.read_only = False

    async def async_load(self) -> list[dict[str, Any]]:
        if self.loaded:
            return self.items
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a newer or unreadable file
            _LOGGER.warning("SMART Sniffer Replica: its picks could not be read (%s); the "
                            "Replica is not read and the file is not changed", err)
            self.read_only = True
            stored = None
        self.items = clean_picks((stored or {}).get("picks") if isinstance(stored, dict) else None)
        self.loaded = True
        return self.items

    async def async_save(self) -> None:
        if self.read_only:
            raise ReplicaError("replica_store_newer",
                               "The Replica was saved by a newer SMART Sniffer; not changed.")
        await self._store.async_save({"picks": copy.deepcopy(self.items)})

    async def async_remove(self) -> None:
        await self._store.async_remove()


class ReplicaRuntime:
    """The library and the Replica entries' picks, for this run."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.library = ReplicaLibrary({SEED: SEED_DIR, USER: user_dir(hass), SHARE: SHARE_DIR})
        self.started_at = time.time()
        self.picks: dict[str, Picks] = {}
        self.lock = asyncio.Lock()

    def picks_of(self, entry_id: str) -> Picks:
        picks = self.picks.get(entry_id)
        if picks is None:
            picks = self.picks[entry_id] = Picks(self.hass, entry_id)
        return picks


def user_dir(hass: HomeAssistant) -> str:
    return hass.config.path("smart_sniffer", "replicas")


@callback
def runtime(hass: HomeAssistant) -> ReplicaRuntime:
    rt = hass.data.get(RUNTIME)
    if rt is None:
        rt = hass.data[RUNTIME] = ReplicaRuntime(hass)
    return rt


def _make_user_dir(path: str) -> None:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as err:
        _LOGGER.info("SMART Sniffer: the replica folder %s could not be made: %s", path, err)


class ReplicaSource:
    """The Replica's agent API, served in process for its coordinators.

    The poll's first request (/api/drives) renders everything once in the
    executor; the poll's other requests read that render. Answers are deep
    copies: nothing the coordinator changes reaches the next poll."""

    kind = KIND_REPLICA

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, rt: ReplicaRuntime) -> None:
        self.hass = hass
        self.entry_id = entry.entry_id
        self.agent_id = str(entry.data.get(ENTRY_AGENT_ID) or "")
        self.rt = rt
        self._snapshot: dict[str, Any] | None = None
        self._at = 0.0

    def describe(self) -> str:
        return "Replica"

    async def async_render(self) -> dict[str, Any]:
        picks = self.rt.picks_of(self.entry_id)
        await picks.async_load()
        if picks.read_only:
            raise ReplicaError("replica_store_newer", "its picks could not be read")
        items = copy.deepcopy(picks.items)
        snapshot = await self.hass.async_add_executor_job(
            lambda: render(items, self.rt.library, agent_id=self.agent_id,
                           started_at=self.rt.started_at))
        self._snapshot, self._at = snapshot, time.monotonic()
        return snapshot

    async def get_json(self, path: str) -> Any:
        snapshot = self._snapshot
        if path == "/api/drives" or snapshot is None or time.monotonic() - self._at > SNAPSHOT_TTL:
            snapshot = await self.async_render()
        from .replica_engine import serve  # noqa: PLC0415

        status, body = serve(snapshot, path)
        if status != 200:
            raise ReplicaError("not_found", f"{path} not found")
        return copy.deepcopy(body)

    def card_info(self) -> dict[str, Any]:
        """The last render's card files and the picks whose file is gone."""
        snap = self._snapshot or {}
        return {"cards": snap.get("cards") or {}, "missing": snap.get("missing") or []}


async def async_source(hass: HomeAssistant, entry: ConfigEntry) -> ReplicaSource:
    """The Replica entry's source, its picks loaded, the user folder made."""
    rt = runtime(hass)
    await hass.async_add_executor_job(_make_user_dir, user_dir(hass))
    await rt.picks_of(entry.entry_id).async_load()
    return ReplicaSource(hass, entry, rt)


def replica_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The Replica machine's entry, when there is one."""
    return next((e for e in hass.config_entries.async_entries(DOMAIN)
                 if is_replica_data(e.data) and e.source != "ignore"), None)


def new_agent_id() -> str:
    return f"replica-{secrets.token_hex(6)}"


# ── The library and the picks, for the panel ─────────────────────────────


async def async_library(hass: HomeAssistant) -> dict[str, Any]:
    rt = runtime(hass)
    await hass.async_add_executor_job(_make_user_dir, user_dir(hass))
    listing = await hass.async_add_executor_job(rt.library.listing)
    entry = replica_entry(hass)
    drives = pools = 0
    if entry is not None:
        drives, pools = pick_counts(await rt.picks_of(entry.entry_id).async_load())
    return {**listing, "caps": {"drives": MAX_DRIVES, "pools": MAX_POOLS},
            "used": {"drives": drives, "pools": pools},
            "entry_id": entry.entry_id if entry is not None else None}


async def async_refresh(hass: HomeAssistant, entry_id: str) -> None:
    """Poll the Replica again now, so sensors and the page update at once."""
    loaded = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    for key in ("coordinator", "health_coordinator"):
        coordinator = loaded.get(key)
        if coordinator is None:
            continue
        refresh = getattr(coordinator, "async_refresh", None) or coordinator.async_request_refresh
        await refresh()


async def async_ensure_entry(hass: HomeAssistant) -> ConfigEntry:
    """The Replica entry, made (and set up) when there is none."""
    entry = replica_entry(hass)
    if entry is not None:
        return entry
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data={"kind": KIND_REPLICA})
    entry = replica_entry(hass)
    if entry is None:
        reason = result.get("reason") if isinstance(result, dict) else None
        raise ReplicaError("error", f"The Replica was not made ({reason or 'no reason'}).")
    return entry


async def async_add(hass: HomeAssistant, file_ids: list[str]) -> dict[str, Any]:
    """Pick these files: each becomes a drive or pool of the Replica, which
    is made first when there is none. All or nothing within the caps."""
    rt = runtime(hass)
    async with rt.lock:
        records = []
        for file_id in file_ids:
            rec = await hass.async_add_executor_job(rt.library.find, file_id)
            if rec is None:
                raise ReplicaError("not_found", "That replica file is gone.")
            if rec.get("doc") is None:
                raise ReplicaError("invalid_replica", rec.get("error") or "not a replica file")
            records.append(rec)
        entry = replica_entry(hass)
        current = (await rt.picks_of(entry.entry_id).async_load()) if entry is not None else []
        # Checked before the entry is made: a refused add changes nothing.
        trial: list[dict[str, Any]] = list(current)
        for rec in records:
            trial.append(new_pick(rec, trial, entry.entry_id if entry else "00000000"))
        if entry is None:
            entry = await async_ensure_entry(hass)
        picks = rt.picks_of(entry.entry_id)
        await picks.async_load()
        added = []
        work = list(picks.items)
        for rec in records:
            pick = new_pick(rec, work, entry.entry_id)
            work.append(pick)
            added.append(pick)
        picks.items = work
        await picks.async_save()
    await async_refresh(hass, entry.entry_id)
    return {"entry_id": entry.entry_id,
            "added": [{"card_id": f"{entry.entry_id}|{card_key(p)}", "file_id": p["file_id"],
                       "kind": p["kind"]} for p in added]}


def _forget_pick_device(hass: HomeAssistant, entry_id: str, pick: dict[str, Any]) -> None:
    """The pick's device leaves Home Assistant with its entities."""
    loaded = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    coordinator = loaded.get("coordinator")
    registry = dr.async_get(hass)
    if pick["kind"] == "pool":
        identifier = f"{entry_id}_zpool_{pick['pool_name']}"
        if coordinator is not None:
            coordinator.forget_pool(pick["pool_name"])
    else:
        identifier = pick["drive_id"]
    if coordinator is not None:
        coordinator.forget_device(identifier, pick.get("serial"))
    # This entry's own device (from 2026.8 identifiers are per entry).
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if (DOMAIN, identifier) in device.identifiers:
            registry.async_update_device(device.id, remove_config_entry_id=entry_id)


async def async_remove_pick(hass: HomeAssistant, entry: ConfigEntry, key: str) -> dict[str, Any]:
    """Remove the pick behind one card (its file stays). The Replica's last
    pick takes the Replica with it."""
    rt = runtime(hass)
    async with rt.lock:
        picks = rt.picks_of(entry.entry_id)
        await picks.async_load()
        pick = next((p for p in picks.items if card_key(p) == key), None)
        if pick is None:
            raise ReplicaError("not_found", "no such replica card")
        last = len(picks.items) == 1
        if not last:
            picks.items = [p for p in picks.items if p["pick_id"] != pick["pick_id"]]
            await picks.async_save()
    if last:
        # The Replica's last card: the entry goes, and its Store with it.
        await hass.config_entries.async_remove(entry.entry_id)
        return {"host_removed": True}
    await async_refresh(hass, entry.entry_id)
    _forget_pick_device(hass, entry.entry_id, pick)
    return {"host_removed": False}


async def async_forget_entry(hass: HomeAssistant, entry_id: str) -> None:
    """The Replica entry is deleted: its picks go; the library stays."""
    rt = runtime(hass)
    picks = rt.picks.pop(entry_id, None) or Picks(hass, entry_id)
    await picks.async_remove()


async def async_save_card(hass: HomeAssistant, item: dict[str, Any],
                          verdict: dict[str, Any] | None,
                          filesystems: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Save a real drive or pool card into the user library."""
    rt = runtime(hass)

    def build_and_write() -> dict[str, Any]:
        if item.get("type") == "pool":
            doc = pool_document(item.get("pool") or {}, filesystems)
        else:
            try:
                doc = replica_from_drive(item.get("drive"), verdict)
            except Exception as err:  # noqa: BLE001 - BadRequest or InvalidReplica
                raise ReplicaError("unsupported_drive", "This drive has no SMART data to copy.") \
                    from err
        return rt.library.write_user(doc)

    return await hass.async_add_executor_job(build_and_write)


async def async_upload(hass: HomeAssistant, raw: bytes) -> dict[str, Any]:
    """A file uploaded through the panel, into the user library."""
    rt = runtime(hass)

    def check_and_write() -> dict[str, Any]:
        doc = parse_document(raw)
        doc["origin"] = upload_origin(doc["origin"])
        return rt.library.write_user(doc)

    return await hass.async_add_executor_job(check_and_write)


async def async_delete_file(hass: HomeAssistant, file_id: str) -> dict[str, Any]:
    """Delete one of the user's own library files. A file a Replica card
    still shows is refused: its card goes first."""
    rt = runtime(hass)
    async with rt.lock:
        entry = replica_entry(hass)
        if entry is not None:
            picks = await rt.picks_of(entry.entry_id).async_load()
            if any(p.get("file_id") == file_id for p in picks):
                raise ReplicaError("replica_file_in_use", "Remove its replica drive first.")
        deleted = await hass.async_add_executor_job(rt.library.delete_user, file_id)
    return {"deleted": deleted}

EDITED_SUFFIX = ", edited"


async def async_edit(hass: HomeAssistant, entry: ConfigEntry, key: str,
                     ata: dict[str, int], nvme: dict[str, int],
                     devstat: dict[str, int] | None = None) -> dict[str, Any]:
    """Change raw values of one Replica drive card's file. A user file is
    changed in place; a shipped or /share file is copied into the user
    library as "<name>, edited" first and the card switched to the copy
    (its drive, device and entities stay the same)."""
    rt = runtime(hass)
    async with rt.lock:
        picks = rt.picks_of(entry.entry_id)
        await picks.async_load()
        pick = next((p for p in picks.items if card_key(p) == key), None)
        if pick is None or pick.get("kind") != "drive":
            raise ReplicaError("not_found", "no such replica drive")
        rec = await hass.async_add_executor_job(rt.library.find, pick["file_id"])
        if rec is None or rec.get("doc") is None:
            raise ReplicaError("not_found", "That replica file is gone.")
        doc = edit_document(rec["doc"], ata, nvme, devstat)

        def write() -> dict[str, Any]:
            if rec["source"] == USER:
                return rt.library.update_user(rec["id"], doc)
            name = str(doc["name"])
            if not name.endswith(EDITED_SUFFIX):
                name = name[:200 - len(EDITED_SUFFIX)] + EDITED_SUFFIX
            copied = {**doc, "name": name, "order": None,
                      "origin": {"type": "uploaded", "edited_from": rec["id"]}}
            return rt.library.write_user(copied)

        saved = await hass.async_add_executor_job(write)
        switched = saved["id"] != pick["file_id"]
        if switched:
            picks.items = [{**p, "file_id": saved["id"], "name": saved["name"]}
                           if p["pick_id"] == pick["pick_id"] else p for p in picks.items]
            await picks.async_save()
    await async_refresh(hass, entry.entry_id)
    return {"file": saved, "switched": switched}

async def async_file(hass: HomeAssistant, file_id: str) -> dict[str, Any]:
    """A library file, for download."""
    rt = runtime(hass)
    rec = await hass.async_add_executor_job(rt.library.find, file_id)
    if rec is None:
        raise ReplicaError("not_found", "That replica file is gone.")
    if rec.get("doc") is None:
        raise ReplicaError("invalid_replica", rec.get("error") or "not a replica file")
    text = json.dumps(rec["doc"], indent=2, ensure_ascii=False) + "\n"
    return {"name": f"{rec['stem']}.json", "text": text}


# ── The SMART Sniffer App's old Replica machine ────────────────────────────


def is_legacy_app_replica(entry: ConfigEntry) -> bool:
    """An HTTP entry at the SMART Sniffer App's replica agent (App 0.3.x).

    Conservative: one of the App's agents by its host name, title or
    address, and either it said it is the replica (agent_mock true), or its
    kind was never learnt and it is at the replica agent's port. Never the
    App's real agent (agent_mock false), never a field agent, never the
    integration's own Replica."""
    data = entry.data
    if is_replica_data(data) or not is_app_entry(data, entry.title):
        return False
    if getattr(entry, "source", None) == "hassio" or data.get("app_slug"):
        return False  # the Supervisor's discovery only ever offers the App's real agent
    mock = data.get(ENTRY_AGENT_MOCK)
    if mock is True:
        return True
    return mock is None and agent_port(data) == APP_REPLICA_PORT


async def async_remove_legacy(hass: HomeAssistant) -> list[str]:
    """Remove the App's old Replica machines; tell the user once."""
    gone = []
    for entry in list(hass.config_entries.async_entries(DOMAIN)):
        if not is_legacy_app_replica(entry):
            continue
        _LOGGER.info("SMART Sniffer: removing %s, the SMART Sniffer App's old Replica machine",
                     entry.title)
        try:
            await hass.config_entries.async_remove(entry.entry_id)
        except Exception:  # noqa: BLE001 - never stops the others
            _LOGGER.exception("SMART Sniffer: %s could not be removed", entry.title)
            continue
        gone.append(entry.entry_id)
    if gone:
        await _tell_moved(hass)
    return gone


async def _tell_moved(hass: HomeAssistant) -> None:
    """The one-time notification, and the panel's one-time line."""
    from .panel_data import panel_data  # noqa: PLC0415
    from .panel_state import PanelStateNewer  # noqa: PLC0415

    pd = panel_data(hass)
    state = await pd.state.async_load()
    if "replicas_moved" in state["notes"]:
        return
    from homeassistant.components.persistent_notification import (  # noqa: PLC0415
        async_create as pn_create,
    )

    pn_create(hass, MOVED_TEXT, title="SMART Sniffer", notification_id=MOVED_NOTIFICATION)

    def change(data: dict[str, Any]) -> None:
        data["notes"]["replicas_moved"] = False  # shown until dismissed

    try:
        await pd.state.async_change(change)
    except PanelStateNewer:
        pass


@callback
def async_schedule_legacy_removal(hass: HomeAssistant) -> None:
    """At start (once Home Assistant runs): remove the App's old Replica
    machines. Every start, so one an old App made again goes again."""

    async def run(_event: Any = None) -> None:
        try:
            await async_remove_legacy(hass)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("SMART Sniffer: the old Replica machines were not removed")

    if hass.state is CoreState.running:
        hass.async_create_task(run())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, run)
