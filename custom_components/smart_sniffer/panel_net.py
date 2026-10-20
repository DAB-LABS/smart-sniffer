"""The + card of the SMART Sniffer panel: Network list, Scan, Check, Add (v0.9).

- Network: every SMART Sniffer agent Home Assistant's zeroconf sees now (its
  shared mDNS cache of _smartha._tcp), the ones already added marked added
  and not selectable; plus the discovery flows that wait to be added. The
  SMART Sniffer App's own agent is never listed (it is HAOS, + This box).
- Scan: the HA App 0.3.1 block scan, run here by the integration: one IPv4
  block of at most /22 (1022 addresses), one port, 64 probes at a time, 0.4 s
  to connect and 1 s to read, GET /api/health and /api/drives only, never
  with a password; Home Assistant's own addresses are skipped. One scan at a
  time (a new one cancels the last); a finished one is kept 10 minutes.
- Add by hand: one host and port, asked for its health (Check). An agent
  that answers joins the list, ticked.
- Add: each ticked row through the integration's own config flow, so the
  checks are the same as in Home Assistant's dialog: a discovered row
  finishes its discovery flow; any other row runs the user step. An agent
  that wants a password nobody has given yet (the lock) is added through
  the import step without one; Home Assistant then asks for it and the
  machine's card shows the password field, as the Control Center always did.

The page never receives a password, and only sends one in the machine's
password field (panel_ws.machine/password).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .agent_identity import UNIQUE_ID_PREFIX, display_hostname, ip_score, norm_host
from .host_name import APP_HOST
from .source import agent_address, agent_port
from .const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, CONF_TOKEN, DEFAULT_PORT, \
    DEFAULT_SCAN_INTERVAL, DISCOVERY_CONTEXT, DOMAIN
from .panel_data import entries, is_hassio, panel_data

_LOGGER = logging.getLogger(__name__)

CHECK_TIMEOUT = 5  # seconds for one Check

# The HA App 0.3.1 scan's limits (app web/proxy.py).
SCAN_WORKERS = 64
SCAN_CONNECT_TIMEOUT = 0.4
SCAN_READ_TIMEOUT = 1.0
SCAN_KEEP_SECONDS = 600  # a finished scan can be read for 10 minutes
MIN_SCAN_PREFIX = 22  # wider blocks are refused with a suggestion
SCANS = "smart_sniffer_scans"

SERVICE_TYPE = "_smartha._tcp.local."
SEEN_LOOKUP = 1.5  # seconds to ask the network for a record not in the cache
_TYPE_PTR = 12
_CLASS_IN = 1
HASSIO_NET = ipaddress.ip_network("172.30.32.0/23")


def row_id(address: str, port: int) -> str:
    return f"{str(address).lower()}:{int(port)}"


def _added(hass: HomeAssistant, address: str, port: int) -> str | None:
    """The entry already at this address and port, by name; None."""
    for entry in entries(hass):
        entry_port = agent_port(entry.data)
        if entry_port is None:
            continue  # the Replica has no address
        if entry_port == int(port) and norm_host(agent_address(entry.data)) == norm_host(address):
            return entry.entry_id
    return None


def discovered_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Agents found by Home Assistant's discovery, waiting to be added."""
    rows: list[dict[str, Any]] = []
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        context = flow.get("context") or {}
        facts = context.get(DISCOVERY_CONTEXT)
        if context.get("source") != SOURCE_ZEROCONF or not isinstance(facts, dict):
            continue
        address, port = str(facts.get("host") or ""), facts.get("port") or DEFAULT_PORT
        if not address:
            continue
        drives = facts.get("drives")
        try:
            drives = int(drives)
        except (TypeError, ValueError):
            drives = None
        rows.append({
            "id": row_id(address, port), "address": address, "port": int(port),
            "hostname": str(facts.get("hostname") or address),
            "version": facts.get("version") or None, "drives": drives,
            "auth": "password" if facts.get("auth") else "none",
            "added": _added(hass, address, port) is not None,
            "flow_id": flow.get("flow_id"), "source": "discovered",
        })
    return rows


async def async_check(hass: HomeAssistant, host: str, port: int) -> dict[str, Any]:
    """One machine asked for its health: a Network row, or an error code
    (not_answering, not_an_agent)."""
    session = async_get_clientsession(hass)
    base = f"http://{host}:{port}"
    timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
    try:
        async with session.get(f"{base}/api/health", timeout=timeout) as resp:
            if resp.status != 200:
                return {"error": "not_an_agent"}
            health = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
        return {"error": "not_answering"}
    except ValueError:
        return {"error": "not_an_agent"}
    if not isinstance(health, dict) or health.get("status") != "ok":
        return {"error": "not_an_agent"}
    auth = "none"
    drives = health.get("drives") if isinstance(health.get("drives"), int) else None
    try:
        async with session.get(f"{base}/api/drives", timeout=timeout) as resp:
            if resp.status in (401, 403):
                auth = "password"
            elif resp.status == 200 and drives is None:
                listing = await resp.json(content_type=None)
                drives = len(listing) if isinstance(listing, list) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError):
        pass
    added = _added(hass, host, port)
    if added is not None and _every_card_removed(hass, added):
        added = None  # on the page again with Add
    hostname = health.get("hostname") if isinstance(health.get("hostname"), str) else None
    return {
        "id": row_id(host, port), "address": host, "port": int(port),
        "hostname": hostname or host, "version": health.get("version"),
        "drives": drives, "auth": auth, "added": added is not None,
        "flow_id": None, "source": "manual",
    }


def _every_card_removed(hass: HomeAssistant, entry_id: str) -> bool:
    removed = panel_data(hass).state.data["removed"]
    return any(cid.startswith(f"{entry_id}|") for cid in removed)


def _kind(result: dict[str, Any]) -> str:
    kind = result.get("type")
    return str(getattr(kind, "value", kind))


def _outcome(result: dict[str, Any]) -> tuple[str, str | None]:
    """(result, entry id) of a flow step: created, already_added,
    password_needed, cannot_connect, or error."""
    kind = _kind(result)
    if kind == "create_entry":
        entry = result.get("result")
        return "created", getattr(entry, "entry_id", None)
    if kind == "abort":
        reason = result.get("reason")
        if reason == "already_configured":
            return "already_added", None
        if reason == "cannot_connect":
            return "cannot_connect", None
        return "error", None
    if kind == "form":
        base = (result.get("errors") or {}).get("base")
        if base == "invalid_auth":
            return "password_needed", None
        if base == "cannot_connect":
            return "cannot_connect", None
    return "error", None


async def _add_one(hass: HomeAssistant, row: dict[str, Any]) -> dict[str, Any]:
    flows = hass.config_entries.flow
    address, port = str(row["address"]), int(row["port"])
    out = {"id": row_id(address, port), "address": address, "port": port}
    # A machine whose cards were all removed from the page comes back.
    existing = _added(hass, address, port)
    if existing is not None:
        removed = [cid for cid in panel_data(hass).state.data["removed"]
                   if cid.startswith(f"{existing}|")]
        if removed:
            def restore(state: dict[str, Any]) -> None:
                for cid in removed:
                    state["removed"].pop(cid, None)
            await panel_data(hass).state.async_change(restore)
            return {**out, "result": "restored", "entry_id": existing}
    flow_id = row.get("flow_id")
    if flow_id:
        try:
            result = await flows.async_configure(flow_id, {})
        except Exception:  # noqa: BLE001 - the discovery ended meanwhile
            result = {"type": "abort", "reason": "unknown"}
        outcome, entry_id = _outcome(result)
        if outcome == "password_needed":
            hostname, drives = None, row.get("drives")
            for item in discovered_rows(hass):
                if item.get("flow_id") == flow_id:
                    hostname = item.get("hostname")
                    if drives is None:
                        drives = item.get("drives")
            try:
                flows.async_abort(flow_id)
            except Exception:  # noqa: BLE001
                pass
            outcome, entry_id = await _import(hass, address, port, hostname, drives)
        return {**out, "result": outcome, "entry_id": entry_id}
    result = await flows.async_init(DOMAIN, context={"source": "user"})
    try:
        result = await flows.async_configure(result["flow_id"], {
            CONF_HOST: address, CONF_PORT: port, CONF_TOKEN: "",
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        })
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SMART Sniffer panel: adding %s:%s failed", address, port)
        result = {"type": "abort", "reason": "unknown"}
    outcome, entry_id = _outcome(result)
    if _kind(result) == "form" and result.get("flow_id"):
        try:
            flows.async_abort(result["flow_id"])
        except Exception:  # noqa: BLE001
            pass
    if outcome == "password_needed":
        outcome, entry_id = await _import(hass, address, port, None, row.get("drives"))
    return {**out, "result": outcome, "entry_id": entry_id}


async def _import(hass: HomeAssistant, address: str, port: int,
                  hostname: str | None, drives: Any = None) -> tuple[str, str | None]:
    """The import step for an agent that wants a password. ``drives``, the
    count its row shows, lets the step's duplicate check recognise it
    already configured under another address: without a password the new
    address does not show its drives."""
    data: dict[str, Any] = {CONF_HOST: address, CONF_PORT: port, CONF_TOKEN: ""}
    if hostname:
        data["hostname"] = hostname
    if isinstance(drives, int) and not isinstance(drives, bool):
        data["drives"] = drives
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data=data
    )
    return _outcome(result)


async def async_add(hass: HomeAssistant, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each row, one at a time (each flow's duplicate checks see the
    entries the ones before it made)."""
    results = []
    for row in rows:
        try:
            results.append(await _add_one(hass, row))
        except Exception:  # noqa: BLE001 - one row never fails the others
            _LOGGER.exception("SMART Sniffer panel: adding %s failed", row.get("address"))
            results.append({"id": row_id(row["address"], row["port"]),
                            "address": row["address"], "port": row["port"], "result": "error"})
    return results


# ── Network: what Home Assistant's zeroconf sees ───────────────────────────


def _entry_for_agent(hass: HomeAssistant, hostname: str, addresses: list[str],
                     port: int) -> str | None:
    """The entry that already monitors this advertised agent: by the unique
    id discovery gives it, by any address it advertises, or by the host
    name the entry keeps for its agent on the same port."""
    want = norm_host(hostname)
    for entry in entries(hass):
        entry_port = agent_port(entry.data)
        if entry_port is None:
            continue  # the Replica
        if want and entry.unique_id and norm_host(
                str(entry.unique_id).removeprefix(UNIQUE_ID_PREFIX)) == want \
                and str(entry.unique_id).startswith(UNIQUE_ID_PREFIX):
            return entry.entry_id
        if entry_port != int(port):
            continue
        address = norm_host(agent_address(entry.data))
        if any(norm_host(a) == address for a in addresses):
            return entry.entry_id
        if want and norm_host(entry.data.get("agent_hostname")) == want:
            return entry.entry_id
    return None


async def _zeroconf_services(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every _smartha._tcp service in Home Assistant's shared zeroconf cache:
    {name, port, addresses, properties}. Nothing when zeroconf is not
    running. A record the cache lacks is asked for once, briefly."""
    if "zeroconf" not in hass.config.components:
        return []  # never start an mDNS instance of our own
    try:
        from homeassistant.components.zeroconf import async_get_async_instance  # noqa: PLC0415
        from zeroconf.asyncio import AsyncServiceInfo  # noqa: PLC0415

        aiozc = await async_get_async_instance(hass)
        zc = aiozc.zeroconf
        names = sorted({str(getattr(rec, "alias", "")) for rec in
                        zc.cache.async_all_by_details(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN)} - {""})
    except Exception:  # noqa: BLE001 - no zeroconf here, or its API moved
        _LOGGER.debug("SMART Sniffer panel: zeroconf cache not readable", exc_info=True)
        return []

    async def one(name: str) -> dict[str, Any] | None:
        try:
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if not info.load_from_cache(zc):
                await info.async_request(zc, int(SEEN_LOOKUP * 1000))
            if not info.port:
                return None
            props = {}
            for key, value in (info.decoded_properties or {}).items():
                props[str(key)] = "" if value is None else str(value)
            return {"name": name, "port": int(info.port),
                    "addresses": [str(a) for a in info.parsed_scoped_addresses()],
                    "server": str(info.server or ""), "properties": props}
        except Exception:  # noqa: BLE001 - one record never fails the list
            _LOGGER.debug("SMART Sniffer panel: %s not read", name, exc_info=True)
            return None

    found = await asyncio.gather(*(one(n) for n in names[:200]))
    return [f for f in found if f]


def seen_row(hass: HomeAssistant, service: dict[str, Any]) -> dict[str, Any] | None:
    """One zeroconf service as a Network row; None for the SMART Sniffer
    App's own agent on HAOS (that machine is + This box)."""
    props = service.get("properties") or {}
    port = int(service.get("port") or DEFAULT_PORT)
    addresses = [a for a in service.get("addresses") or [] if a]
    host = props.get("ip") or (sorted(addresses, key=ip_score)[0] if addresses else "")
    if not host:
        return None
    hostname = display_hostname(props.get("hostname") or "") or display_hostname(
        str(service.get("server") or "")) or host
    if APP_HOST.match(norm_host(hostname)) and is_hassio(hass):
        return None
    try:
        drives = int(props.get("drives"))
    except (TypeError, ValueError):
        drives = None
    added = _entry_for_agent(hass, hostname, [host, *addresses], port)
    return {
        "id": row_id(host, port), "address": host, "port": port, "hostname": hostname,
        "version": props.get("version") or None, "drives": drives,
        "auth": "password" if props.get("auth") == "1" else "none",
        "added": added is not None, "flow_id": None, "source": "seen",
    }


async def async_seen_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    rows = []
    for service in await _zeroconf_services(hass):
        row = seen_row(hass, service)
        if row is not None:
            rows.append(row)
    return rows


async def async_network_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The Network list: the discovery flows waiting to be added first, then
    every other agent zeroconf sees (added ones marked), one row each."""
    out: dict[str, dict[str, Any]] = {}
    for row in discovered_rows(hass):
        out.setdefault(row["id"], row)
    for row in await async_seen_rows(hass):
        known = out.get(row["id"])
        if known is None:
            out[row["id"]] = row
        elif row["added"]:
            known["added"] = True
    rows = list(out.values())
    rows.sort(key=lambda r: (bool(r["added"]), str(r["hostname"]).lower(), r["id"]))
    return rows


async def async_network_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Home Assistant's own address and its /24, the Scan's default block."""
    try:
        from homeassistant.components.network import async_get_source_ip  # noqa: PLC0415

        address = await async_get_source_ip(hass)
        ip = ipaddress.IPv4Address(address)
    except Exception:  # noqa: BLE001 - no IPv4 source address
        return None
    return {"address": str(ip), "block": str(ipaddress.ip_network(f"{ip}/24", strict=False))}


# ── Scan: the HA App's block scan, run by the integration ──────────────────


class ScanRefused(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def parse_block(text: Any) -> ipaddress.IPv4Network:
    """An IPv4 CIDR block (a bare address is a /32); ScanRefused otherwise."""
    if not isinstance(text, str) or not text.strip():
        raise ScanRefused("bad_block", "the block must be an IPv4 address or CIDR block")
    try:
        net = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise ScanRefused("bad_block", "the block must be an IPv4 address or CIDR block") from None
    if net.version != 4:
        raise ScanRefused("bad_block", "only IPv4 blocks can be scanned")
    return net  # type: ignore[return-value]


def _is_agent_health(status: int, body: Any) -> bool:
    return status == 200 and isinstance(body, dict) and (
        (isinstance(body.get("version"), str) and isinstance(body.get("endpoints"), list)
         and "/api/drives" in body["endpoints"])
        or (body.get("status") == "ok" and isinstance(body.get("version"), str)))


def _wants_password(status: int, body: Any) -> bool:
    """A 401 with the agent's own body (a mock agent run with a token)."""
    return status == 401 and isinstance(body, dict) and body.get("error") == "unauthorized"


async def _get(session: aiohttp.ClientSession, url: str,
               timeout: aiohttp.ClientTimeout) -> tuple[int | None, Any]:
    try:
        async with session.get(url, timeout=timeout, allow_redirects=False) as resp:
            raw = await resp.content.read(256 * 1024)
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            return resp.status, body
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError, ValueError):
        return None, None


async def async_probe(hass: HomeAssistant, address: str, port: int) -> tuple[str, dict | None]:
    """Is there an agent on address:port? ("ok", row), ("not_answering",
    None) or ("not_an_agent", None). GET /api/health and /api/drives only,
    never with a password, the scan's short timeouts."""
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=SCAN_CONNECT_TIMEOUT + 2 * SCAN_READ_TIMEOUT,
                                    sock_connect=SCAN_CONNECT_TIMEOUT,
                                    sock_read=SCAN_READ_TIMEOUT)
    base = f"http://{address}:{port}"
    status, body = await _get(session, f"{base}/api/health", timeout)
    if status is None:
        return "not_answering", None
    if _is_agent_health(status, body):
        dstatus, listing = await _get(session, f"{base}/api/drives", timeout)
        if dstatus is None:
            return "not_answering", None
        auth = "password" if dstatus in (401, 403) else "none"
        count = len(listing) if dstatus == 200 and isinstance(listing, list) else None
    elif _wants_password(status, body):
        body, auth, count = {}, "password", None
    else:
        return "not_an_agent", None
    if count is None and isinstance(body.get("drives"), int):
        count = body["drives"]
    hostname = body.get("hostname") if isinstance(body.get("hostname"), str) else None
    return "ok", {
        "id": row_id(address, port), "address": address, "port": int(port),
        "hostname": display_hostname(hostname) or address,
        "version": body.get("version") if isinstance(body.get("version"), str) else None,
        "drives": count, "auth": auth, "added": False, "flow_id": None, "source": "scan",
    }


class Scan:
    def __init__(self, scan_id: str, block: ipaddress.IPv4Network, port: int) -> None:
        self.id = scan_id
        self.block = block
        self.port = port
        hosts = list(block.hosts()) if block.prefixlen < 31 else list(block)
        self.addresses = [str(a) for a in hosts]
        self.total = len(self.addresses)
        self.probed = 0
        self.found: list[dict[str, Any]] = []
        self.state = "running"
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.finished_at: str | None = None
        self.finished_mono: float | None = None
        self.task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"scan_id": self.id, "block": str(self.block), "port": self.port,
                "state": self.state, "probed": self.probed, "total": self.total,
                "found": [dict(row) for row in self.found],
                "started_at": self.started_at, "finished_at": self.finished_at}

    def finish(self, state: str) -> None:
        if self.state == "running":
            self.state = state
            self.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.finished_mono = time.monotonic()


async def _own_addresses(hass: HomeAssistant) -> set[str]:
    """This Home Assistant's own IPv4 addresses: nothing is sent to them."""
    own: set[str] = set()
    try:
        from homeassistant.components import network  # noqa: PLC0415

        for adapter in await network.async_get_adapters(hass):
            for ip in adapter.get("ipv4") or []:
                if ip.get("address"):
                    own.add(str(ip["address"]))
    except Exception:  # noqa: BLE001
        pass
    return own


async def _added_keys(hass: HomeAssistant) -> set[tuple[str, int]]:
    """(address, port) of every entry, and the IPv4 address its host name
    resolves to (1 s at most each)."""
    keys: set[tuple[str, int]] = set()
    loop = asyncio.get_running_loop()
    for entry in entries(hass):
        port = agent_port(entry.data)
        if port is None:
            continue
        address = str(agent_address(entry.data) or "").lower()
        keys.add((address, port))
        try:
            ipaddress.IPv4Address(address)
            continue
        except ValueError:
            pass
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(address, None, family=socket.AF_INET), timeout=1.0)
            for info in infos:
                keys.add((str(info[4][0]), port))
        except (OSError, asyncio.TimeoutError, TimeoutError):
            continue
    return keys


class ScanManager:
    """One scan at a time; a new one cancels the running one."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.scans: dict[str, Scan] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        for scan_id, scan in list(self.scans.items()):
            if scan.finished_mono is not None and now - scan.finished_mono > SCAN_KEEP_SECONDS:
                del self.scans[scan_id]

    def get(self, scan_id: str) -> Scan | None:
        self._purge()
        return self.scans.get(scan_id)

    def cancel(self, scan_id: str) -> Scan | None:
        scan = self.get(scan_id)
        if scan is not None:
            self._stop(scan)
        return scan

    def _stop(self, scan: Scan) -> None:
        scan.finish("cancelled")
        if scan.task is not None and not scan.task.done():
            scan.task.cancel()

    async def async_start(self, block: ipaddress.IPv4Network, port: int) -> Scan:
        if block.prefixlen < MIN_SCAN_PREFIX:
            info = await async_network_info(self.hass)
            suggest = info["block"] if info else str(
                ipaddress.ip_network(f"{block.network_address}/24", strict=False))
            raise ScanRefused("too_wide", f"blocks wider than /{MIN_SCAN_PREFIX} are not scanned",
                              addresses=2 ** (32 - block.prefixlen) - 2, suggest=suggest)
        self._purge()
        for other in self.scans.values():
            if other.state == "running":
                self._stop(other)
        scan = Scan(os.urandom(4).hex(), block, port)
        self.scans[scan.id] = scan
        own = await _own_addresses(self.hass)
        hassio = is_hassio(self.hass)
        added = await _added_keys(self.hass)

        def skip(address: str) -> bool:
            return address in own or (hassio and ipaddress.IPv4Address(address) in HASSIO_NET)

        scan.task = self.hass.async_create_background_task(
            self._run(scan, skip, added), f"smart_sniffer scan {scan.id}")
        return scan

    async def _run(self, scan: Scan, skip: Any, added: set[tuple[str, int]]) -> None:
        pending = iter(scan.addresses)
        # The module's own probe, looked up each time (tests stand one in).
        module = sys.modules[__name__]

        async def worker() -> None:
            for address in pending:
                if scan.state != "running":
                    return
                row = None
                if not skip(address):
                    outcome, row = await module.async_probe(self.hass, address, scan.port)
                    if outcome != "ok":
                        row = None
                if scan.state != "running":
                    return
                scan.probed += 1
                if row is not None:
                    row["added"] = (row["address"], row["port"]) in added or _added(
                        self.hass, row["address"], row["port"]) is not None
                    scan.found.append(row)

        try:
            await asyncio.gather(*(worker() for _ in range(min(SCAN_WORKERS, max(1, scan.total)))))
        except asyncio.CancelledError:
            scan.finish("cancelled")
            raise
        scan.finish("done")


def scans(hass: HomeAssistant) -> ScanManager:
    manager = hass.data.get(SCANS)
    if manager is None:
        manager = hass.data[SCANS] = ScanManager(hass)
    return manager
