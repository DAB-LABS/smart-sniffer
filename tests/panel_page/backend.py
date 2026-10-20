"""The SMART Sniffer panel's page against a stand-in Home Assistant.

Serves a page that plays Home Assistant's shell (host.html: a hass object
whose websocket goes over HTTP to here), the integration's panel files at
/smart_sniffer_static/ as Home Assistant serves them, and answers the
page's websocket commands with the integration's real handlers
(panel_ws.py) over a real Home Assistant core: config entries, device
registry, Stores, dispatcher. The machines are the board of
tests/panel_harness.py, built by the repo's mock agent from its replica
files. Only the network is faked: Check, Add and the password.

    PYTHONPATH=. <ha venv>/bin/python -m tests.panel_page.backend --port 8765

POST /fake/reset {"scenario": ...} starts a board afresh:
    board       HAOS (the App's agent), nas-01, studio-mac (down), backup-pi
                (wants a password); on HAOS (hassio) with the App installed
    container   the same machines on a Container install (no hassio)
    no_app      HAOS without the App and without its agent; the App note
    empty       no machines (every entry deleted this run), HAOS, no App
    imported    the board after the SMART Sniffer App handed its machines
                over: one made (offline), nas-01 known, one not moved
    replica     the board plus the Replica machine with three picks (one
                whose user file is then deleted), a user file and a file
                in the /share drop folder; the replicas-moved note set
POST /fake/control {"action": ...}: down, up (a machine stops or starts
answering), poll (a machine's new read pushed), fill (the Replica up to
its caps).

+ > Replica is the integration's own: its library folders live in the
test core's config folder (the drop folder in a temporary one), and the
Replica entry's coordinators and source are the real ones
(panel_harness.setup_replica_entry).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

import tests.conftest  # noqa: F401 - registers the bare package
from homeassistant.helpers.dispatcher import async_dispatcher_send
from tests import panel_harness as H

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "custom_components" / "smart_sniffer" / "panel"
HERE = Path(__file__).resolve().parent

CATALOG = {
    "10.0.10.61": {"hostname": "office-nuc", "files": ["shipped.sata-ssd"], "auth": "none"},
    "office-nuc.lan": {"hostname": "office-nuc", "files": ["shipped.sata-ssd"], "auth": "none"},
    "10.0.20.14": {"hostname": "media-box", "files": ["shipped.nvme-usb"], "auth": "password"},
}
NOT_AGENT = {"10.0.10.98"}
# What the + card's Scan finds (the probe is faked): office-nuc (also the
# waiting discovery), nas-01 (added already), and garage-pi on 10.0.30.0/24.
SCAN_FOUND = {
    "10.0.10.61": {"hostname": "office-nuc", "drives": 1, "auth": "none"},
    "10.0.10.20": {"hostname": "nas-01", "drives": 4, "auth": "none"},
    "10.0.30.5": {"hostname": "garage-pi", "drives": 2, "auth": "password"},
}
# What zeroconf sees besides the discovery: nas-01, added already.
SEEN = [{"id": "10.0.10.20:9099", "address": "10.0.10.20", "port": 9099, "hostname": "nas-01",
         "version": "0.8.5", "drives": 4, "auth": "none", "added": True, "flow_id": None,
         "source": "seen"}]


class Backend:
    def __init__(self) -> None:
        self.hass = None
        self.board = None
        self.tmp = None
        self.subs: dict[int, H.FakeConnection] = {}
        self.next_sub = 1
        self.scenario = "board"
        self.discovered: list[dict[str, Any]] = []
        self.added: list[str] = []

    async def reset(self, scenario: str) -> None:
        if self.hass is not None:
            for conn in self.subs.values():
                for unsub in conn.subscriptions.values():
                    unsub()
            await self.hass.async_stop(force=True)
        self.subs = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.scenario = scenario
        hass = await H._new_hass(self.tmp.name)
        self.hass = hass
        specs = [] if scenario == "empty" else [
            s for s in H.BOARD if not (scenario == "no_app" and s["host"].endswith("smart-sniffer-agent"))
        ]
        H.patch_replica(hass)
        rt = H.replica.runtime(hass)
        self.share = Path(self.tmp.name) / "share-replicas"
        rt.library.folders["share"] = str(self.share)
        self.board = await H.board(hass, specs)
        if scenario != "container":
            hass.config.components.add("hassio")
        app = {} if scenario in ("no_app", "empty") else {
            "slug": "a0d7b954_smart_sniffer_agent", "state": "started"}
        H.panel_data.app_info = lambda _hass: app if _hass.config.components and \
            "hassio" in _hass.config.components else None
        self.discovered = [{
            "id": "10.0.10.61:9099", "address": "10.0.10.61", "port": 9099,
            "hostname": "office-nuc", "version": "0.8.5", "drives": 1, "auth": "none",
            "added": False, "flow_id": "flow-office-nuc", "source": "discovered",
        }]
        self.added = []
        self._patch()
        if scenario == "replica":
            await self._replica_board()
        if scenario == "imported":
            await self._imported()

    async def _replica_board(self) -> None:
        hass = self.hass
        seed = ROOT / "custom_components" / "smart_sniffer" / "replicas"
        user = Path(H.replica.user_dir(hass))
        user.mkdir(parents=True, exist_ok=True)
        self.share.mkdir(parents=True, exist_ok=True)
        doc = json.loads((seed / "sata-ssd-healthy.json").read_text())
        doc.update(name="Office SSD, saved 2026-10-12", origin={"type": "saved"}, order=None)
        (user / "office-ssd.json").write_text(json.dumps(doc))
        (user / "gone-soon.json").write_text(json.dumps({**doc, "name": "Old test drive"}))
        (user / "broken.json").write_text("{not json")
        doc = json.loads((seed / "nvme-wear.json").read_text())
        doc.update(name="Lab NVMe, from the share", order=None)
        (self.share / "lab-nvme.json").write_text(json.dumps(doc))
        await H.replica.async_add(hass, ["seed.sata-hdd-reallocated", "seed.zfs-pool-degraded",
                                         "user.gone-soon"])
        (user / "gone-soon.json").unlink()
        await H.replica.async_refresh(hass, H.REPLICA_ID)
        pd = H.panel_data.panel_data(hass)
        await pd.state.async_change(lambda st: st["notes"].update(replicas_moved=False))

    def _patch(self) -> None:
        backend = self
        hass = self.hass

        async def network_rows(_hass):
            if backend.scenario == "empty":
                return []
            return [r for r in backend.discovered if r["address"] not in backend.added] + \
                [dict(r) for r in SEEN]

        async def network_info(_hass):
            return {"address": "10.0.10.5", "block": "10.0.10.0/24"}

        async def probe(_hass, address, port):
            agent = SCAN_FOUND.get(address)
            if agent is None or port != 9099 or backend.scenario == "empty":
                return "not_answering", None
            return "ok", {"id": f"{address}:{port}", "address": address, "port": port,
                          "hostname": agent["hostname"], "version": "0.8.5",
                          "drives": agent["drives"], "auth": agent["auth"], "added": False,
                          "flow_id": None, "source": "scan"}

        async def check(_hass, host, port):
            if host in NOT_AGENT:
                return {"error": "not_an_agent"}
            agent = CATALOG.get(host)
            if agent is None:
                return {"error": "not_answering"}
            added = any(e.data.get("host") == host for e in backend.board.entries.values())
            return {"id": f"{host}:{port}", "address": host, "port": port,
                    "hostname": agent["hostname"], "version": "0.8.5", "drives": len(agent["files"]),
                    "auth": agent["auth"], "added": added, "flow_id": None, "source": "manual"}

        async def add(_hass, rows):
            out = []
            for row in rows:
                agent = CATALOG.get(row["address"]) or CATALOG.get("10.0.10.61")
                entry_id = f"01ADD{len(backend.board.entries):021d}"
                spec = {"id": entry_id, "host": row["address"], "port": row["port"],
                        "title": f"SMART Sniffer ({row['address']}:{row['port']})",
                        "data": {"agent_hostname": agent["hostname"]},
                        "files": agent["files"], "reauth": agent["auth"] == "password"}
                backend.board.add(spec)
                backend.added.append(row["address"])
                async_dispatcher_send(hass, H.panel_data.SIGNAL_PANEL_UPDATE, entry_id)
                out.append({"id": f"{row['address']}:{row['port']}", "address": row["address"],
                            "port": row["port"], "result": "created", "entry_id": entry_id})
            return out

        async def reauth(_hass, entry, password):
            if password != "fieldpass":
                return "invalid_auth"
            backend.board.reauth.discard(entry.entry_id)
            spec = {"id": entry.entry_id, "files": ["shipped.sata-hdd-healthy"]}
            data = H.agent_data(spec["files"])
            from types import SimpleNamespace  # noqa: PLC0415

            hass.data.setdefault("smart_sniffer", {})[entry.entry_id] = {
                "coordinator": H.FakeCoordinator(data),
                "health_coordinator": SimpleNamespace(data=H.health(entry.data.get("agent_hostname"))),
            }
            H.panel_data.panel_data(hass).read_at[entry.entry_id] = H.panel_data.utc_text()
            return "ok"

        async def remove_entry(entry_id):
            if entry_id == H.REPLICA_ID:
                await H.replica.async_forget_entry(hass, entry_id)
            backend.board.entries.pop(entry_id, None)
            entry = hass.config_entries._entries.pop(entry_id, None)
            hass.data.get("smart_sniffer", {}).pop(entry_id, None)
            from homeassistant.config_entries import (  # noqa: PLC0415
                SIGNAL_CONFIG_ENTRY_CHANGED,
                ConfigEntryChange,
            )
            async_dispatcher_send(hass, SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntryChange.REMOVED, entry)
            return {"require_restart": False}

        H.panel_ws.network_rows = network_rows
        H.panel_net.async_network_info = network_info
        H.panel_net.async_probe = probe
        H.panel_ws.async_check = check
        H.panel_ws.async_add = add
        H.panel_ws._reauth = reauth
        hass.config_entries.async_remove = remove_entry

    # ── HTTP ──

    async def ws(self, request: web.Request) -> web.Response:
        msg = await request.json()
        kind = str(msg.get("type", ""))
        if kind == "auth/sign_path":
            # Home Assistant signs a path for the user; here any signature
            # is the test's.
            return web.json_response({"result": {"path": f"{msg['path']}?authSig=test-signature"}})
        if kind.startswith("smart_sniffer/panel/app_import/"):
            return await self._import_command(msg)
        name = str(msg.pop("type", "")).removeprefix("smart_sniffer/panel/")
        conn = H.FakeConnection(self.hass)
        try:
            result = await H.call(self.hass, conn, name, **msg)
        except H.WsError as err:
            return web.json_response({"error": {"code": err.code, "message": err.message}})
        except KeyError:
            return web.json_response({"error": {"code": "unknown_command", "message": name}})
        return web.json_response({"result": result})

    async def _import_command(self, msg: dict[str, Any]) -> web.Response:
        from custom_components.smart_sniffer import panel_import  # noqa: PLC0415

        handler = {"dismiss": panel_import.ws_import_dismiss,
                   "discard": panel_import.ws_import_discard}.get(msg["type"].rsplit("/", 1)[1])
        if handler is None:
            return web.json_response({"error": {"code": "unknown_command", "message": msg["type"]}})
        conn = H.FakeConnection(self.hass)
        msg = {**msg, "id": next(H._ids)}
        try:
            if handler._ws_schema is not False:
                msg = handler._ws_schema(msg)
            fut = conn._future(msg["id"])
            handler(self.hass, conn, msg)
            result = await asyncio.wait_for(fut, 10)
        except H.WsError as err:
            return web.json_response({"error": {"code": err.code, "message": err.message}})
        except Exception as err:  # noqa: BLE001 - voluptuous Invalid
            return web.json_response({"error": {"code": "invalid_format", "message": str(err)}})
        async_dispatcher_send(self.hass, H.panel_data.SIGNAL_PANEL_STATE)
        return web.json_response({"result": result})

    async def agentic(self, request: web.Request) -> web.Response:
        """The raw Agentic access JSON: Home Assistant's signed link (any
        authSig here) for the page's admin; else 401."""
        from types import SimpleNamespace  # noqa: PLC0415

        if not request.query.get("authSig"):
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        status, body, headers = await H.panel_ws.async_agentic_http(
            self.hass, SimpleNamespace(is_admin=True), request.query)
        return web.Response(text=body, status=status, headers=headers,
                            content_type="application/json")

    async def _imported(self) -> None:
        """The SMART Sniffer App handed its machines over: nas-01 known, an
        old box made offline, one address the App kept that is no address."""
        from custom_components.smart_sniffer import panel_import  # noqa: PLC0415
        from tests.test_device_names_under_registry import _make_entry  # noqa: PLC0415

        hass = self.hass

        async def health(_hass, address, port):
            return None

        async def create(_hass, address, port, password):
            entry = _make_entry("01OLDBOX00000000000000000E", f"SMART Sniffer ({address}:{port})",
                                {"host": address, "port": port, "token": password,
                                 "scan_interval": 60}, "import")
            hass.config_entries._entries[entry.entry_id] = entry
            return "created", entry.entry_id

        panel_import.async_health = health
        panel_import.async_create = create
        await panel_import.async_run(hass, [
            {"item_id": "nas", "kind": "field", "address": "10.0.10.20", "port": 9099,
             "password": "", "hidden": False, "removed": [], "name": "Basement NAS", "cards": {}},
            {"item_id": "old", "kind": "field", "address": "10.0.10.250", "port": 9099,
             "password": "fieldpass", "hidden": False, "removed": [], "cards": {}},
            {"item_id": "bad", "kind": "field", "address": "printer room", "port": 9099,
             "password": "", "hidden": False, "removed": [], "cards": {}},
        ])

    async def subscribe(self, request: web.Request) -> web.Response:
        conn = H.FakeConnection(self.hass)
        handler = H.command_handler("subscribe")
        msg = {"id": 1, "type": "smart_sniffer/panel/subscribe"}
        msg = handler._ws_schema(msg) if handler._ws_schema else msg
        handler(self.hass, conn, msg)
        sub = self.next_sub
        self.next_sub += 1
        self.subs[sub] = conn
        return web.json_response({"sub": sub})

    async def events(self, request: web.Request) -> web.Response:
        sub = int(request.query.get("sub", "0"))
        after = int(request.query.get("after", "0"))
        conn = self.subs.get(sub)
        if conn is None:
            return web.json_response({"events": [], "next": after, "gone": True})
        for _ in range(100):
            if len(conn.events) > after:
                break
            await asyncio.sleep(0.05)
        return web.json_response({"events": conn.events[after:], "next": len(conn.events)})

    async def unsubscribe(self, request: web.Request) -> web.Response:
        body = await request.json()
        conn = self.subs.pop(int(body.get("sub", 0)), None)
        if conn is not None:
            for unsub in conn.subscriptions.values():
                unsub()
        return web.json_response({"ok": True})

    async def reset_route(self, request: web.Request) -> web.Response:
        body = await request.json()
        await self.reset(body.get("scenario", "board"))
        return web.json_response({"ok": True, "scenario": self.scenario})

    async def control(self, request: web.Request) -> web.Response:
        body = await request.json()
        action = body.get("action")
        entry_id = body.get("entry_id")
        coord = self.board.coordinator(entry_id) if entry_id else None
        if action == "down" and coord is not None:
            coord.last_update_success = False
        elif action == "fill":
            entry = H.replica.replica_entry(self.hass)
            picks = H.replica.runtime(self.hass).picks_of(entry.entry_id).items if entry else []
            drives, pools = H.replica.pick_counts(picks)
            files = ["seed.sata-hdd-healthy"] * (12 - drives) + ["seed.zfs-pool-healthy"] * (4 - pools)
            if files:
                await H.replica.async_add(self.hass, files)
        elif action == "up" and coord is not None:
            coord.last_update_success = True
            H.panel_data.panel_data(self.hass).read_at[entry_id] = H.panel_data.utc_text()
        if entry_id:
            async_dispatcher_send(self.hass, H.panel_data.SIGNAL_PANEL_UPDATE, entry_id)
        return web.json_response({"ok": True})

    async def upload(self, request: web.Request) -> web.Response:
        from types import SimpleNamespace  # noqa: PLC0415

        from custom_components.smart_sniffer import panel_replica  # noqa: PLC0415

        status, body = await panel_replica.async_handle_upload(
            self.hass, SimpleNamespace(is_admin=True), request.content, request.content_length)
        return web.json_response(body, status=status)

    async def host_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(HERE / "host.html")

    async def state(self, request: web.Request) -> web.Response:
        pd = H.panel_data.panel_data(self.hass)
        await pd.state.async_load()
        return web.json_response({"state": pd.state.data,
                                  "entries": {e.entry_id: dict(e.data) for e in
                                              self.hass.config_entries.async_entries("smart_sniffer")}})


async def serve(port: int) -> None:
    backend = Backend()
    await backend.reset("board")
    app = web.Application()
    app.router.add_get("/", backend.host_page)
    app.router.add_post("/fake/ws", backend.ws)
    app.router.add_post("/fake/subscribe", backend.subscribe)
    app.router.add_get("/fake/events", backend.events)
    app.router.add_post("/fake/unsubscribe", backend.unsubscribe)
    app.router.add_post("/fake/reset", backend.reset_route)
    app.router.add_post("/fake/control", backend.control)
    app.router.add_get("/fake/state", backend.state)
    app.router.add_post("/api/smart_sniffer/replica/upload", backend.upload)
    app.router.add_get("/api/smart_sniffer/agentic.json", backend.agentic)
    app.router.add_static("/smart_sniffer_static", PANEL)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"ready on {port}", flush=True)
    await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
