"""The + card's Network list and Scan under Home Assistant (v0.9 round 3).

The Network list is every SMART Sniffer agent Home Assistant's zeroconf
sees (its shared mDNS cache), the ones already added marked added, plus
the discoveries waiting; the Scan is the HA App 0.3.1 block scan, run by
the integration with the same limits. A real core with the board of
tests/panel_harness.py; the zeroconf cache is a real zeroconf instance's
cache with records put in by hand; the probes go to a real local agent
stand-in over HTTP, or to a stand-in function. Skipped without Home
Assistant.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

pytest.importorskip("homeassistant")

from aiohttp import web  # noqa: E402

from tests import panel_harness as H  # noqa: E402

NAS = "01NAS00000000000000000000B"
net = H.panel_net


def run(tmp_path, body, monkeypatch, specs=None):
    async def main():
        hass = await H._new_hass(tmp_path)
        try:
            board = await H.board(hass, specs, monkeypatch)
            await body(hass, board, H.FakeConnection(hass))
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _no_app(monkeypatch):
    monkeypatch.setattr(H.panel_data, "app_info", lambda hass: None)


def _service(host, hostname, port=9099, **props):
    return {"name": f"{hostname}._smartha._tcp.local.", "port": port, "addresses": [host],
            "server": f"{hostname}.local.", "properties": {"hostname": hostname, **props}}


# ── The Network list ───────────────────────────────────────────────────────


def test_every_agent_zeroconf_sees_with_the_added_ones_marked(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        services = [
            _service("10.0.10.20", "nas-01", drives="4", version="0.8.5"),   # added (address)
            _service("10.0.10.77", "garage-pi", drives="2", auth="1"),       # new
            _service("192.168.7.3", "studio-mac"),                           # added (host name)
            _service("172.30.33.5", "a0d7b954-smart-sniffer-agent"),         # the App's agent
        ]

        async def seen(_hass):
            return services
        monkeypatch.setattr(net, "_zeroconf_services", seen)
        hass.config.components.add("hassio")
        result = await H.call(hass, conn, "net/list")
        rows = {r["hostname"]: r for r in result["found"]}
        assert set(rows) == {"nas-01", "garage-pi", "studio-mac"}  # never the App's agent
        assert rows["nas-01"]["added"] is True and rows["studio-mac"]["added"] is True
        new = rows["garage-pi"]
        assert (new["added"], new["auth"], new["drives"], new["address"], new["source"]) == \
            (False, "password", 2, "10.0.10.77", "seen")
        # New ones first, so they are the first to see.
        assert result["found"][0]["hostname"] == "garage-pi"
        # Off HAOS the App's host name is an ordinary agent.
        hass.config.components.remove("hassio")
        rows = {r["hostname"] for r in (await H.call(hass, conn, "net/list"))["found"]}
        assert "a0d7b954-smart-sniffer-agent" in rows
    run(tmp_path, body, monkeypatch)


def test_the_ip_txt_record_and_unique_id_decide(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        entry = board.entries[NAS]
        hass.config_entries.async_update_entry(entry, unique_id="smartha-nas-01")
        # Advertised on a container address, its TXT record names its LAN one.
        row = net.seen_row(hass, _service("172.17.0.4", "nas-01", ip="10.0.10.99"))
        assert row["address"] == "10.0.10.99" and row["added"] is True  # by its unique id
        row = net.seen_row(hass, {**_service("172.17.0.4", "other"), "addresses": ["172.17.0.4", "10.0.10.150"]})
        assert row["address"] == "10.0.10.150" and row["added"] is False  # the LAN address first
        assert net.seen_row(hass, {**_service("", "x"), "addresses": []}) is None
    run(tmp_path, body, monkeypatch)


def test_waiting_discoveries_and_seen_agents_are_one_list(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        flow_row = {"id": "10.0.10.61:9099", "address": "10.0.10.61", "port": 9099,
                    "hostname": "office-nuc", "version": "0.8.5", "drives": 1, "auth": "none",
                    "added": False, "flow_id": "f1", "source": "discovered"}
        monkeypatch.setattr(net, "discovered_rows", lambda _hass: [dict(flow_row)])

        async def seen(_hass):
            return [_service("10.0.10.61", "office-nuc"), _service("10.0.10.20", "nas-01")]
        monkeypatch.setattr(net, "_zeroconf_services", seen)
        rows = await net.async_network_rows(hass)
        assert [(r["hostname"], r["source"], r["added"]) for r in rows] == [
            ("office-nuc", "discovered", False), ("nas-01", "seen", True)]
        assert rows[0]["flow_id"] == "f1"  # Add finishes its discovery
    run(tmp_path, body, monkeypatch)


def test_nothing_seen_without_zeroconf_and_no_instance_started(tmp_path, monkeypatch):
    async def body(hass, board, conn):
        assert "zeroconf" not in hass.config.components
        assert await net._zeroconf_services(hass) == []
        assert "zeroconf" not in hass.data
        result = await H.call(hass, conn, "net/list")
        assert result["found"] == []
    run(tmp_path, body, monkeypatch)


def test_the_shared_zeroconf_cache_is_read(tmp_path, monkeypatch):
    """A real zeroconf instance (loopback only) whose cache holds one agent's
    records, as Home Assistant's browser leaves them."""
    pytest.importorskip("zeroconf")
    from zeroconf import DNSAddress, DNSPointer, DNSService, DNSText  # noqa: PLC0415
    from zeroconf.asyncio import AsyncZeroconf  # noqa: PLC0415

    async def body(hass, board, conn):
        try:
            aiozc = AsyncZeroconf(interfaces=["127.0.0.1"])
        except OSError as err:  # pragma: no cover - no sockets here
            pytest.skip(f"no zeroconf sockets: {err}")
        try:
            zc = aiozc.zeroconf
            name = "garage-pi._smartha._tcp.local."
            txt = b"".join(bytes([len(x)]) + x for x in (b"hostname=garage-pi", b"drives=2", b"version=0.8.5"))
            ttl = 4500
            zc.cache.async_add_records([
                DNSPointer(net.SERVICE_TYPE, 12, 1, ttl, name),
                DNSService(name, 33, 1 | 0x8000, 120, 0, 0, 9099, "garage-pi.local."),
                DNSText(name, 16, 1 | 0x8000, ttl, txt),
                DNSAddress("garage-pi.local.", 1, 1 | 0x8000, 120, socket.inet_aton("10.0.10.77")),
            ])
            hass.data["zeroconf"] = aiozc
            hass.config.components.add("zeroconf")

            async def instance(_hass):
                return aiozc
            import homeassistant.components.zeroconf as ha_zc  # noqa: PLC0415
            monkeypatch.setattr(ha_zc, "async_get_async_instance", instance)
            services = await net._zeroconf_services(hass)
            assert len(services) == 1
            svc = services[0]
            assert (svc["port"], svc["addresses"], svc["properties"]["hostname"]) == \
                (9099, ["10.0.10.77"], "garage-pi")
            rows = (await H.call(hass, conn, "net/list"))["found"]
            assert [(r["hostname"], r["address"], r["drives"], r["added"]) for r in rows] == \
                [("garage-pi", "10.0.10.77", 2, False)]
        finally:
            hass.data.pop("zeroconf", None)
            await aiozc.async_close()
    run(tmp_path, body, monkeypatch)


# ── The Scan ───────────────────────────────────────────────────────────────


async def _until_done(hass, conn, scan_id):
    for _ in range(200):
        snap = await H.call(hass, conn, "net/scan/get", scan_id=scan_id)
        if snap["state"] != "running":
            return snap
        await asyncio.sleep(0.02)
    raise AssertionError("the scan did not finish")


def test_scan_a_block_finds_agents_marks_added_and_skips_this_box(tmp_path, monkeypatch):
    probed = []
    agents = {"10.0.10.20": "nas-01", "10.0.10.77": "garage-pi"}
    active = {"now": 0, "most": 0}

    async def probe(_hass, address, port):
        probed.append(address)
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        await asyncio.sleep(0.001)
        active["now"] -= 1
        if address not in agents:
            return "not_answering", None
        return "ok", {"id": f"{address}:{port}", "address": address, "port": port,
                      "hostname": agents[address], "version": "0.8.5", "drives": 1,
                      "auth": "none", "added": False, "flow_id": None, "source": "scan"}

    async def own(_hass):
        return {"10.0.10.5"}

    async def body(hass, board, conn):
        monkeypatch.setattr(net, "async_probe", probe)
        monkeypatch.setattr(net, "_own_addresses", own)
        hass.config.components.add("hassio")
        started = await H.call(hass, conn, "net/scan", block="10.0.10.0/24", port=9099)
        assert started["total"] == 254
        snap = await _until_done(hass, conn, started["scan_id"])
        assert snap["state"] == "done" and snap["probed"] == 254
        found = {r["hostname"]: r["added"] for r in snap["found"]}
        assert found == {"nas-01": True, "garage-pi": False}
        assert "10.0.10.5" not in probed and len(probed) == 253  # this box is never asked
        assert active["most"] <= net.SCAN_WORKERS
        # The Supervisor's network on HAOS is skipped too.
        probed.clear()
        started = await H.call(hass, conn, "net/scan", block="172.30.32.0/24", port=9099)
        await _until_done(hass, conn, started["scan_id"])
        assert probed == []
    run(tmp_path, body, monkeypatch)


def test_scan_limits_wide_bad_cancel_and_admins_only(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def slow(_hass, address, port):
        await gate.wait()
        return "not_answering", None

    async def body(hass, board, conn):
        monkeypatch.setattr(net, "async_probe", slow)
        info = {"address": "10.0.10.5", "block": "10.0.10.0/24"}

        async def network_info(_hass):
            return info
        monkeypatch.setattr(net, "async_network_info", network_info)
        wide = await H.call(hass, conn, "net/scan", block="10.0.0.0/16", port=9099)
        # The block around this Home Assistant is offered instead.
        assert wide == {"error": "too_wide", "addresses": 65534, "suggest": "10.0.10.0/24"}
        info = None
        wide = await H.call(hass, conn, "net/scan", block="10.0.0.0/21", port=9099)
        assert wide == {"error": "too_wide", "addresses": 2046, "suggest": "10.0.0.0/24"}
        for block in ("nonsense", "fe80::/64"):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, conn, "net/scan", block=block, port=9099)
            assert err.value.code == "bad_block"
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "net/scan", block="10.0.0.0/24", port=70000)
        assert err.value.code == "invalid_format"
        first = await H.call(hass, conn, "net/scan", block="10.0.1.0/24", port=9099)
        second = await H.call(hass, conn, "net/scan", block="10.0.2.0/24", port=9099)
        # One at a time: the second cancelled the first.
        assert (await H.call(hass, conn, "net/scan/get", scan_id=first["scan_id"]))["state"] == "cancelled"
        assert (await H.call(hass, conn, "net/scan/cancel", scan_id=second["scan_id"])) == {"state": "cancelled"}
        await asyncio.sleep(0)
        assert (await H.call(hass, conn, "net/scan/get", scan_id=second["scan_id"]))["state"] == "cancelled"
        gate.set()
        with pytest.raises(H.WsError) as err:
            await H.call(hass, conn, "net/scan/get", scan_id="nope")
        assert err.value.code == "not_found"
        viewer = H.FakeConnection(hass, admin=False)
        for command, fields in (("net/scan", {"block": "10.0.0.0/24", "port": 9099}),
                                ("net/scan/get", {"scan_id": "x"}), ("net/scan/cancel", {"scan_id": "x"}),
                                ("net/list", {})):
            with pytest.raises(H.WsError) as err:
                await H.call(hass, viewer, command, **fields)
            assert err.value.code == "unauthorized", command
    run(tmp_path, body, monkeypatch)


def test_the_probe_asks_health_and_drives_only_without_a_password(tmp_path, monkeypatch):
    seen = []

    async def health(request):
        seen.append((request.path, request.headers.get("Authorization")))
        return web.json_response({"status": "ok", "version": "0.8.5", "hostname": "bench-agent",
                                  "endpoints": ["/api/health", "/api/drives"]})

    async def drives(request):
        seen.append((request.path, request.headers.get("Authorization")))
        return web.json_response([{"id": "a"}, {"id": "b"}])

    async def locked_drives(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    async def not_agent(request):
        return web.Response(text="<html>router</html>")

    async def serve(routes):
        app = web.Application()
        for path, handler in routes:
            app.router.add_get(path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, port

    async def body(hass, board, conn):
        open_, p1 = await serve([("/api/health", health), ("/api/drives", drives)])
        locked, p2 = await serve([("/api/health", health), ("/api/drives", locked_drives)])
        other, p3 = await serve([("/api/health", not_agent)])
        # Home Assistant's shared session, as a plain one (no network
        # component in this core).
        import aiohttp  # noqa: PLC0415
        session = aiohttp.ClientSession()
        monkeypatch.setattr(net, "async_get_clientsession", lambda _hass: session)
        try:
            outcome, row = await net.async_probe(hass, "127.0.0.1", p1)
            assert outcome == "ok" and (row["hostname"], row["drives"], row["auth"]) == ("bench-agent", 2, "none")
            assert seen == [("/api/health", None), ("/api/drives", None)]
            outcome, row = await net.async_probe(hass, "127.0.0.1", p2)
            assert outcome == "ok" and row["auth"] == "password"
            assert (await net.async_probe(hass, "127.0.0.1", p3))[0] == "not_an_agent"
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                closed = s.getsockname()[1]
            assert (await net.async_probe(hass, "127.0.0.1", closed))[0] == "not_answering"
            # A whole scan through the real probe: one address (this box's
            # own addresses, loopback among them, are skipped otherwise).
            async def none(_hass):
                return set()
            monkeypatch.setattr(net, "_own_addresses", none)
            started = await H.call(hass, conn, "net/scan", block="127.0.0.1/32", port=p1)
            snap = await _until_done(hass, conn, started["scan_id"])
            assert [r["hostname"] for r in snap["found"]] == ["bench-agent"]
        finally:
            await session.close()
            for runner in (open_, locked, other):
                await runner.cleanup()
    run(tmp_path, body, monkeypatch)
