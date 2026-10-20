"""The SMART Sniffer panel's Replica commands (v0.9).

``smart_sniffer/panel/replica/*``, admins only, a schema on every field,
as the other panel commands (panel_ws.py):

  replica/library      the library files by source, with whether each can
                       be picked and why not; the user folder's state; the
                       caps and how many are used
  replica/add          pick files (ids from the listing): each becomes a
                       drive or pool card of the Replica, made first when
                       there is none; all or nothing within the caps
  replica/remove       remove one Replica card's pick (its file stays); the
                       last one takes the Replica with it
  replica/save         save a real drive or pool card into the user library
  replica/file         a library file, for download
  replica/edit         change raw values of a Replica drive card's file
                       (ATA attribute raw values, four NVMe health fields,
                       three Device Statistics entries):
                       a user file in place, a shipped or /share file as a
                       new user file "<name>, edited" the card then shows
  replica/delete       delete one of the user's own files (saved or
                       uploaded; never a shipped or /share file), refused
                       while a Replica card shows it

and the upload, an admin-only HTTP view (POST
/api/smart_sniffer/replica/upload, the file as the body, 1 MB at most,
read in pieces and refused past it), so a large file never holds the
user's websocket.

Clients send file ids and card ids only: ids are looked up in the listing,
never turned into a path. Files are parsed as JSON and checked, never run.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from . import replica as rep
from .panel_ws import CARD_ID, _card, _command, _find_item, _Refused
from .panel_data import entry_kind, utc_text
from .replica_engine import DEVSTAT_EDITS, MAX_REPLICA_BYTES, ReplicaError
from .source import is_replica_data

_LOGGER = logging.getLogger(__name__)

FILE_ID = vol.All(cv.string, vol.Length(min=3, max=200))
UPLOAD_URL = "/api/smart_sniffer/replica/upload"

MESSAGES = {
    "too_large": "That file is over 1 MB.",
    "invalid_replica": "That file is not a replica file.",
    "replica_folder_unavailable": "Your replica folder is not available.",
    "replica_folder_full": "Your replica folder is full.",
    "replica_cap": "The Replica holds up to 12 drives and 4 pools.",
    "not_found": "That replica file is gone.",
    "unsupported_drive": "This drive has no SMART data to copy.",
    "unsupported_pool": "This pool could not be saved.",
    "replica_store_newer": "The Replica was saved by a newer SMART Sniffer; not changed.",
    "replica_file_in_use": "Remove its replica drive first.",
    "not_deletable": "Only your own replica files can be deleted.",
}
RAW = vol.All(int, vol.Range(min=0, max=2 ** 48 - 1))


def _refused(err: ReplicaError) -> _Refused:
    return _Refused(err.code, MESSAGES.get(err.code) or err.message)


@_command({"type": "replica/library"})
async def ws_replica_library(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    return await rep.async_library(hass)


@_command({"type": "replica/add",
           vol.Required("file_ids"): vol.All([FILE_ID], vol.Length(min=1, max=16))})
async def ws_replica_add(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rep.async_add(hass, list(msg["file_ids"]))
    except ReplicaError as err:
        raise _refused(err) from err


@_command({"type": "replica/remove", vol.Required("card_id"): CARD_ID})
async def ws_replica_remove(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry, key = _card(hass, msg["card_id"])
    if not is_replica_data(entry.data):
        raise _Refused("not_found", "not a Replica card")
    try:
        result = await rep.async_remove_pick(hass, entry, key)
    except ReplicaError as err:
        raise _refused(err) from err
    await _forget_card(hass, msg["card_id"])
    return {"removed": msg["card_id"], **result}


async def _forget_card(hass: HomeAssistant, cid: str) -> None:
    """A removed Replica card's hidden mark, name and place go with it."""
    from .panel_data import panel_data  # noqa: PLC0415
    from .panel_state import PanelStateNewer  # noqa: PLC0415

    def change(state: dict[str, Any]) -> None:
        state["order"] = [i for i in state["order"] if i != cid]
        state["hidden"].pop(cid, None)
        state["card_names"].pop(cid, None)
        state["removed"].pop(cid, None)

    try:
        await panel_data(hass).state.async_change(change)
    except PanelStateNewer:
        pass


@_command({"type": "replica/save", vol.Required("card_id"): CARD_ID})
async def ws_replica_save(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Save a real drive or pool card as a replica file in the user library
    (never a Replica card: that file is in the library already)."""
    entry, _key = _card(hass, msg["card_id"])
    if entry_kind(entry) == "replica":
        raise _Refused("not_found", "A Replica card is a library file already.")
    item = _find_item(hass, entry, msg["card_id"])
    if item.get("type") not in ("drive", "pool"):
        raise _Refused("not_found", "no such card")
    verdict = item.get("verdict") or {}
    file_verdict = None
    if item.get("type") == "drive":
        if verdict.get("state") == "UNSUPPORTED" or "smart_data" not in (item.get("drive") or {}):
            raise _Refused("unsupported_drive", MESSAGES["unsupported_drive"])
        file_verdict = {"state": verdict.get("state"), "severity": verdict.get("severity"),
                        "reasons": verdict.get("reasons"),
                        "rules_version": verdict.get("rules_version"), "at": utc_text()}
    try:
        saved = await rep.async_save_card(hass, item, file_verdict, item.get("filesystems"))
    except ReplicaError as err:
        raise _refused(err) from err
    return {"saved": saved}


@_command({"type": "replica/file", vol.Required("file_id"): FILE_ID})
async def ws_replica_file(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rep.async_file(hass, msg["file_id"])
    except ReplicaError as err:
        raise _refused(err) from err


@_command({"type": "replica/edit", vol.Required("card_id"): CARD_ID,
           vol.Optional("ata", default={}): vol.All(
               {vol.All(cv.string, vol.Match(r"^\d{1,3}$")): RAW}, vol.Length(max=64)),
           vol.Optional("nvme", default={}): vol.All(
               {vol.In(["temperature", "available_spare", "percentage_used", "media_errors"]): RAW},
               vol.Length(max=8)),
           vol.Optional("devstat", default={}): vol.All(
               {vol.In(sorted(DEVSTAT_EDITS)): RAW}, vol.Length(max=8))})
async def ws_replica_edit(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    entry, key = _card(hass, msg["card_id"])
    if not is_replica_data(entry.data):
        raise _Refused("not_found", "not a Replica card")
    if not msg["ata"] and not msg["nvme"] and not msg["devstat"]:
        raise _Refused("bad_value", "Nothing to change.")
    try:
        return await rep.async_edit(hass, entry, key, dict(msg["ata"]), dict(msg["nvme"]),
                                    dict(msg["devstat"]))
    except ReplicaError as err:
        raise _Refused(err.code, MESSAGES.get(err.code) or err.message) from err


@_command({"type": "replica/delete", vol.Required("file_id"): FILE_ID})
async def ws_replica_delete(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rep.async_delete_file(hass, msg["file_id"])
    except ReplicaError as err:
        raise _refused(err) from err


REPLICA_COMMANDS = (ws_replica_library, ws_replica_add, ws_replica_remove, ws_replica_save,
                    ws_replica_file, ws_replica_delete, ws_replica_edit)


# ── Upload ─────────────────────────────────────────────────────────────────


async def async_read_upload(content: Any, length: int | None) -> bytes:
    """The body, in pieces, never more than 1 MB plus one byte kept: a
    larger one is refused before it is all read."""
    if length is not None and length > MAX_REPLICA_BYTES:
        raise ReplicaError("too_large", MESSAGES["too_large"])
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await content.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_REPLICA_BYTES:
            raise ReplicaError("too_large", MESSAGES["too_large"])
        chunks.append(chunk)
    return b"".join(chunks)


async def async_handle_upload(hass: HomeAssistant, user: Any, content: Any,
                              length: int | None) -> tuple[int, dict[str, Any]]:
    """(HTTP status, body) for one upload by this user."""
    if user is None or not getattr(user, "is_admin", False):
        return 401, {"error": "unauthorized", "message": "Admins only."}
    try:
        raw = await async_read_upload(content, length)
        saved = await rep.async_upload(hass, raw)
    except ReplicaError as err:
        status = {"too_large": 413, "replica_folder_unavailable": 503,
                  "replica_folder_full": 507}.get(err.code, 422)
        return status, {"error": err.code, "message": MESSAGES.get(err.code) or err.message}
    return 200, {"saved": saved}


def register_upload_view(hass: HomeAssistant) -> None:
    from homeassistant.components.http import HomeAssistantView  # noqa: PLC0415

    class ReplicaUploadView(HomeAssistantView):
        """POST a replica file into the user library (admins only)."""

        url = UPLOAD_URL
        name = "api:smart_sniffer:replica_upload"
        requires_auth = True

        async def post(self, request: Any) -> Any:
            status, body = await async_handle_upload(
                hass, request.get("hass_user"), request.content, request.content_length)
            return self.json(body, status_code=status)

    hass.http.register_view(ReplicaUploadView())

