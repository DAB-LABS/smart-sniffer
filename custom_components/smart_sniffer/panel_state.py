"""What the SMART Sniffer panel keeps between runs (v0.9).

Two kinds of Home Assistant Store, both loaded lazily, the first time the
panel needs them, never during setup:

- The panel Store (``smart_sniffer.panel``), one for the whole integration
  and shared by every admin: machine labels, filesystem card names, hidden
  cards, the card order, cards removed from the page with the trash, and the
  notes a user dismissed. Drive and pool names are not here: they are the
  devices' own ``name_by_user`` in Home Assistant.
- One view Store per config entry (``smart_sniffer.view.<entry_id>``): the
  machine's last good read as the panel drew it, so a machine that does not
  answer (or wants its password) still shows its cards, marked stale, with
  the time they were read. It holds no token. Removed with the entry.

Both have a version and a minor version and a migrate function from the
first release. A file written by a newer major version, or one that cannot
be read, puts the panel Store in read-only mode: the panel runs with empty
state, refuses writes and never overwrites the file. Monitoring is never
affected by either Store.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

PANEL_STORE_KEY = "smart_sniffer.panel"
PANEL_STORE_VERSION = 1
PANEL_STORE_MINOR = 1
PANEL_SAVE_DELAY = 5  # seconds

VIEW_STORE_PREFIX = "smart_sniffer.view"
VIEW_STORE_VERSION = 1
VIEW_STORE_MINOR = 1
VIEW_SAVE_DELAY = 120  # seconds; Home Assistant writes delayed saves at stop

MAX_NAME = 100  # the longest card or machine name kept, as the App kept them


def empty_panel_state() -> dict[str, Any]:
    """The panel Store's content before anything was saved."""
    return {
        "machine_names": {},  # entry_id -> label
        "card_names": {},  # card_id -> name (filesystem cards)
        "hidden": {},  # card_id -> True (hidden) or False (shown again)
        "order": [],  # card ids
        "order_set": False,  # the page was ever ordered by hand
        "removed": {},  # card_id -> ISO time it left the page
        "notes": {},  # note key -> True when dismissed
    }


def clean_name(raw: Any) -> str:
    """A name as stored: spaces collapsed, at most MAX_NAME characters."""
    return " ".join(str(raw or "").split())[:MAX_NAME]


def _migrate_panel(old_major: int, old_minor: int, data: Any) -> dict[str, Any]:
    """Bring an older panel Store file to this version. Minor steps only add
    keys, so a newer minor file is read as it is. A newer major file is
    refused (read-only mode)."""
    if old_major > PANEL_STORE_VERSION:
        raise ValueError(f"panel Store version {old_major} is newer than {PANEL_STORE_VERSION}")
    out = empty_panel_state()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in out and isinstance(value, type(out[key])):
                out[key] = value
    return out


class _PanelStore(Store):
    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        return _migrate_panel(old_major_version, old_minor_version, old_data)


class _ViewStore(Store):
    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        if old_major_version > VIEW_STORE_VERSION:
            raise ValueError("view Store is newer")
        return old_data


class PanelStateNewer(Exception):
    """A write while the panel Store is read only."""


class PanelState:
    """The panel Store, loaded once on first use."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = _PanelStore(
            hass, PANEL_STORE_VERSION, PANEL_STORE_KEY, minor_version=PANEL_STORE_MINOR
        )
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self.read_only = False
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        async with self._lock:
            if self._data is not None:
                return self._data
            try:
                stored = await self._store.async_load()
            except Exception as err:  # noqa: BLE001 - any unreadable file
                _LOGGER.warning(
                    "SMART Sniffer panel settings could not be read (%s); the panel "
                    "shows them empty and does not change the file", err,
                )
                self.read_only = True
                stored = None
            self._data = _migrate_panel(PANEL_STORE_VERSION, PANEL_STORE_MINOR, stored) \
                if stored is not None else empty_panel_state()
            return self._data

    @property
    def data(self) -> dict[str, Any]:
        """The loaded state; empty before the first load."""
        return self._data if self._data is not None else empty_panel_state()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)
        return remove

    async def async_change(self, change: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Apply change(state) to the state and save it, delayed. change may
        return False to mean nothing changed (no save)."""
        data = await self.async_load()
        if self.read_only:
            raise PanelStateNewer
        before = copy.deepcopy(data)
        if change(data) is False or data == before:
            return data
        self._store.async_delay_save(lambda: self._data, PANEL_SAVE_DELAY)
        for listener in list(self._listeners):
            listener()
        return data

    async def async_forget_entry(self, entry_id: str) -> None:
        """An entry is deleted: its label, names, hidden marks, order and
        removed marks go with it."""
        prefix = f"{entry_id}|"

        def change(state: dict[str, Any]) -> None:
            state["machine_names"].pop(entry_id, None)
            for key in ("card_names", "hidden", "removed"):
                state[key] = {k: v for k, v in state[key].items() if not k.startswith(prefix)}
            state["order"] = [i for i in state["order"] if not i.startswith(prefix)]

        try:
            await self.async_change(change)
        except PanelStateNewer:
            pass


class ViewStores:
    """The per-entry last good views, each read on first use."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._stores: dict[str, Store] = {}
        self._views: dict[str, dict[str, Any] | None] = {}
        self._loading: dict[str, asyncio.Future] = {}

    def _store(self, entry_id: str) -> Store:
        store = self._stores.get(entry_id)
        if store is None:
            store = _ViewStore(
                self.hass, VIEW_STORE_VERSION, f"{VIEW_STORE_PREFIX}.{entry_id}",
                minor_version=VIEW_STORE_MINOR,
            )
            self._stores[entry_id] = store
        return store

    def loaded(self, entry_id: str) -> bool:
        return entry_id in self._views

    def get(self, entry_id: str) -> dict[str, Any] | None:
        """The view in memory; None when there is none or it is not read yet."""
        return self._views.get(entry_id)

    async def async_get(self, entry_id: str) -> dict[str, Any] | None:
        if entry_id in self._views:
            return self._views[entry_id]
        try:
            data = await self._store(entry_id).async_load()
        except Exception as err:  # noqa: BLE001 - an unreadable view is no view
            _LOGGER.debug("SMART Sniffer: last view of %s not read: %s", entry_id, err)
            data = None
        if entry_id not in self._views:
            self._views[entry_id] = data if isinstance(data, dict) else None
        return self._views[entry_id]

    def put(self, entry_id: str, view: dict[str, Any]) -> None:
        """Keep a good view, written later (and at stop)."""
        if self._views.get(entry_id) == view:
            return
        self._views[entry_id] = view
        self._store(entry_id).async_delay_save(lambda: self._views.get(entry_id), VIEW_SAVE_DELAY)

    async def async_remove(self, entry_id: str) -> None:
        self._views.pop(entry_id, None)
        store = self._stores.pop(entry_id, None) or _ViewStore(
            self.hass, VIEW_STORE_VERSION, f"{VIEW_STORE_PREFIX}.{entry_id}",
            minor_version=VIEW_STORE_MINOR,
        )
        await store.async_remove()
