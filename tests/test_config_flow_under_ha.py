"""The config flow's duplicate-agent checks, under real Home Assistant.

An agent added by hand or by the Control Center has a "{host}:{port}"
unique_id; one added through zeroconf has "smartha-{hostname}". When the
agent advertises another address than the one it was added with
(smart-sniffer-app#12: an OpenWrt router advertising its WAN address), the
integration kept offering it as discovered, and accepting made a second entry
whose entities were twins of the first (GH #64). These tests drive the flow
with a stand-in for hass.config_entries and for the agents on the network;
addresses, names and serials are invented.

A host name alone never merges two agents (two OpenWrt routers share the
default name): the existing entry's host must answer with the same host name
and the drives must match. And a rediscovery only moves an entry to a new
address when its own address no longer answers and the new one does.

Skipped without Home Assistant, like test_entities_under_ha.py.
"""

from __future__ import annotations

import asyncio
import importlib
from ipaddress import ip_address
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant")

import aiohttp  # noqa: E402

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF  # noqa: E402
from homeassistant.data_entry_flow import AbortFlow  # noqa: E402

try:  # noqa: E402
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2025.x
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

config_flow = importlib.import_module("custom_components.smart_sniffer.config_flow")

DOMAIN = "smart_sniffer"


class _Entries:
    def __init__(self, entries):
        self.entries = entries
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.flow = SimpleNamespace(async_progress_by_handler=lambda *a, **k: [])

    def async_entries(self, domain=None, include_ignore=True, include_disabled=True):
        return [e for e in self.entries if include_ignore or e.source != "ignore"]

    def async_entry_for_domain_unique_id(self, domain, unique_id):
        return next((e for e in self.entries if e.unique_id == unique_id), None)

    def async_update_entry(self, entry, **changes):
        self.updates.append((entry.entry_id, changes))
        if "data" in changes:
            entry.data = changes["data"]
        if "unique_id" in changes:
            entry.unique_id = changes["unique_id"]
        return True


def _entry(entry_id, unique_id, host, port=9099, title=None, **data):
    return SimpleNamespace(
        entry_id=entry_id,
        unique_id=unique_id,
        domain=DOMAIN,
        source="user",
        state=None,
        title=title or f"SMART Sniffer ({host}:{port})",
        data={"host": host, "port": port, "token": "", "scan_interval": 60, **data},
    )


class _Net:
    """Agents on the network: "host:port" -> (hostname, serials, token)."""

    def __init__(self, agents):
        self.agents = agents
        self.calls: list[str] = []

    def _agent(self, host, port, token):
        agent = self.agents.get(f"{host}:{port}")
        if agent is None:
            raise aiohttp.ClientError("unreachable")
        hostname, serials, needs = agent
        if needs and token != needs:
            raise aiohttp.ClientError("401")
        return hostname, serials

    async def get_json(self, host, port, token, path):
        self.calls.append(f"{host}:{port}{path}")
        try:
            hostname, serials = self._agent(host, port, token)
        except aiohttp.ClientError:
            return None
        if path == "/api/health":
            body = {"status": "ok", "version": "0.8.3"}
            if hostname:
                body["hostname"] = hostname
            return body
        return [{"id": f"id-{s}", "serial": s} for s in serials]

    async def test_connection(self, host, port, token):
        self.calls.append(f"{host}:{port}/api/health")
        hostname, _ = self._agent(host, port, token)
        return {"status": "ok", **({"hostname": hostname} if hostname else {})}


def _flow(entries, source, agents=None):
    hass = SimpleNamespace(data={}, config_entries=_Entries(entries))
    flow = config_flow.SmartSnifferConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "flow0001"
    flow.context = {"source": source}
    flow.net = _Net(agents or {})
    flow._get_json = flow.net.get_json
    flow._test_connection = flow.net.test_connection
    return flow


def _discovery(hostname, *addresses, port=9099, preferred=None, drives="2"):
    properties = {"hostname": hostname, "version": "0.8.3", "drives": drives, "auth": "0"}
    if preferred:
        properties["ip"] = preferred
    return ZeroconfServiceInfo(
        ip_address=ip_address(addresses[0]),
        ip_addresses=[ip_address(a) for a in addresses],
        port=port,
        hostname=f"{hostname}.local.",
        type="_smartha._tcp.local.",
        name=f"smartha-{hostname}._smartha._tcp.local.",
        properties=properties,
    )


def _run(coro):
    """Run a step; an AbortFlow becomes the abort result the manager makes."""
    try:
        return asyncio.run(coro)
    except AbortFlow as err:
        return {"type": "abort", "reason": err.reason}


# --- zeroconf: a hand-added entry for the same agent ---------------------------

LAN, WAN = "192.168.40.1", "203.0.113.7"
ROUTER = ("testrouter", ["SER-A1", "SER-A2"], "")


def test_same_hostname_and_drives_other_ip_aborts():
    """app#12: added at its LAN address, advertising its WAN address, both
    answering as the same agent. Already configured; host left alone."""
    entry = _entry("e1", f"{LAN}:9099", LAN)
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9099": ROUTER, f"{WAN}:9099": ROUTER})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert entry.data["host"] == LAN
    assert entry.unique_id == f"{LAN}:9099"
    assert flow.hass.config_entries.updates == []


def test_advertised_address_unreachable_uses_the_drive_count():
    """The WAN address cannot be read: the TXT drive count (2) stands in."""
    entry = _entry("e1", f"{LAN}:9099", LAN)
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9099": ROUTER})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == LAN


def test_same_hostname_different_drives_is_offered():
    """Two routers both called OpenWrt are two agents."""
    entry = _entry("e1", f"{LAN}:9099", LAN)
    other = "192.168.50.1"
    flow = _flow([entry], SOURCE_ZEROCONF, {
        f"{LAN}:9099": ("OpenWrt", ["SER-A1"], ""),
        f"{other}:9099": ("OpenWrt", ["SER-B1"], ""),
    })
    result = _run(flow.async_step_zeroconf(_discovery("OpenWrt", other, drives="1")))
    assert result["type"] == "form" and result["step_id"] == "zeroconf_confirm"


def test_same_hostname_unreadable_and_other_drive_count_is_offered():
    entry = _entry("e1", f"{LAN}:9099", LAN)
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9099": ("OpenWrt", ["SER-A1"], "")})
    result = _run(flow.async_step_zeroconf(_discovery("OpenWrt", WAN, preferred=WAN, drives="3")))
    assert result["type"] == "form"


def test_existing_host_unreachable_is_offered():
    """Nothing shows the two are one agent: offered, as before."""
    entry = _entry("e1", f"{LAN}:9099", LAN)
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{WAN}:9099": ROUTER})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["type"] == "form"


def test_an_older_agent_without_a_hostname_is_offered():
    entry = _entry("e1", f"{LAN}:9099", LAN)
    old = ("", ["SER-A1", "SER-A2"], "")
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9099": old, f"{WAN}:9099": old})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["type"] == "form"


def test_a_different_hostname_is_offered():
    entry = _entry("e1", f"{LAN}:9099", LAN)
    flow = _flow([entry], SOURCE_ZEROCONF, {
        f"{LAN}:9099": ROUTER, "192.168.40.20:9099": ("othernas", ["SER-C1"], ""),
    })
    result = _run(flow.async_step_zeroconf(_discovery("othernas", "192.168.40.20", drives="1")))
    assert result["type"] == "form" and flow.context["unique_id"] == "smartha-othernas"


def test_same_agent_on_another_port_is_offered():
    """The App runs a second agent on another port."""
    entry = _entry("e1", f"{LAN}:9100", LAN, port=9100)
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9100": ROUTER, f"{WAN}:9099": ROUTER})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["type"] == "form"


def test_entry_with_a_token_is_read_with_it():
    entry = _entry("e1", f"{LAN}:9099", LAN, token="sekrit")
    secured = ("testrouter", ["SER-A1", "SER-A2"], "sekrit")
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{LAN}:9099": secured, f"{WAN}:9099": secured})
    result = _run(flow.async_step_zeroconf(_discovery("testrouter", WAN, preferred=WAN)))
    assert result["reason"] == "already_configured"


def test_a_hand_added_entry_entered_by_name_keeps_its_host():
    """Entered by the agent's own name (the App's container name): no longer
    caught by the title substring test and repointed."""
    name = "abcd1234-smart-sniffer-agent"
    entry = _entry("e1", f"{name}:9099", name)
    agent = (name, ["SER-D1"], "")
    flow = _flow([entry], SOURCE_ZEROCONF, {f"{name}:9099": agent, "172.30.33.9:9099": agent})
    result = _run(flow.async_step_zeroconf(_discovery(name, "172.30.33.9", drives="1")))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == name and entry.unique_id == f"{name}:9099"


# --- zeroconf: rediscovering an entry discovery made ---------------------------

NAS = ("nas", ["SER-N1"], "")


def test_a_working_host_is_not_rewritten():
    entry = _entry("e1", "smartha-nas", "192.168.40.5", title="SMART Sniffer (nas)")
    flow = _flow([entry], SOURCE_ZEROCONF, {"192.168.40.5:9099": NAS, "192.168.40.6:9099": NAS})
    result = _run(flow.async_step_zeroconf(_discovery("nas", "192.168.40.6")))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.40.5"
    assert flow.hass.config_entries.updates == []


def test_a_dead_host_moves_to_a_working_advertised_address():
    """DHCP gave the agent a new address: the old one no longer answers."""
    entry = _entry("e1", "smartha-nas", "192.168.40.5", title="SMART Sniffer (nas)")
    flow = _flow([entry], SOURCE_ZEROCONF, {"192.168.40.6:9099": NAS})
    result = _run(flow.async_step_zeroconf(_discovery("nas", "192.168.40.6")))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.40.6"


def test_a_failing_advertised_address_leaves_the_entry_alone():
    entry = _entry("e1", "smartha-nas", "192.168.40.5", title="SMART Sniffer (nas)")
    flow = _flow([entry], SOURCE_ZEROCONF, {})
    result = _run(flow.async_step_zeroconf(_discovery("nas", WAN, preferred=WAN)))
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.40.5"
    assert flow.hass.config_entries.updates == []


def test_a_legacy_discovered_entry_is_migrated_not_repointed():
    """A pre-v0.4.24 discovered entry ("{ip}:{port}", titled with the host
    name) still moves to the hostname unique_id; its working host stays."""
    entry = _entry("e1", "192.168.40.5:9099", "192.168.40.5", title="SMART Sniffer (nas)")
    flow = _flow([entry], SOURCE_ZEROCONF, {"192.168.40.5:9099": NAS, "192.168.40.6:9099": NAS})
    result = _run(flow.async_step_zeroconf(_discovery("nas", "192.168.40.6")))
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "smartha-nas"
    assert entry.data["host"] == "192.168.40.5"


def test_title_containing_the_hostname_is_not_migrated():
    """"pve" is in "SMART Sniffer (pve2)": a different agent, untouched."""
    entry = _entry("e1", "192.168.40.7:9099", "192.168.40.7", title="SMART Sniffer (pve2)")
    flow = _flow([entry], SOURCE_ZEROCONF, {"192.168.40.8:9099": ("pve", ["SER-P1"], "")})
    result = _run(flow.async_step_zeroconf(_discovery("pve", "192.168.40.8", drives="1")))
    assert result["type"] == "form"
    assert entry.unique_id == "192.168.40.7:9099"


# --- user step -----------------------------------------------------------------


def _user(flow, host, port=9099, token=""):
    return _run(flow.async_step_user(
        {"host": host, "port": port, "token": token, "scan_interval": 60}
    ))


def test_user_step_creates_an_entry():
    flow = _flow([], SOURCE_USER, {f"{LAN}:9099": ROUTER})
    result = _user(flow, LAN)
    assert result["type"] == "create_entry"
    assert flow.context["unique_id"] == f"{LAN}:9099"
    assert result["data"]["host"] == LAN


def test_user_step_aborts_on_a_discovered_entry_for_the_same_agent():
    """#64: discovered first, then added again by name from the Control
    Center. Same host name and drives at both addresses."""
    entry = _entry("e1", "smartha-testnas", "172.30.33.9", title="SMART Sniffer (testnas)")
    agent = ("testnas", ["SER-T1"], "")
    flow = _flow([entry], SOURCE_USER, {"172.30.33.9:9099": agent, "testnas-box:9099": agent})
    result = _user(flow, "testnas-box")
    assert result["type"] == "abort" and result["reason"] == "already_configured"


def test_user_step_same_hostname_other_drives_is_created():
    """A second OpenWrt router added by hand is a second agent."""
    entry = _entry("e1", "smartha-OpenWrt", LAN, title="SMART Sniffer (OpenWrt)")
    flow = _flow([entry], SOURCE_USER, {
        f"{LAN}:9099": ("OpenWrt", ["SER-A1"], ""),
        "192.168.50.1:9099": ("OpenWrt", ["SER-B1"], ""),
    })
    result = _user(flow, "192.168.50.1")
    assert result["type"] == "create_entry"


def test_user_step_existing_unreachable_is_created():
    entry = _entry("e1", "smartha-testnas", "172.30.33.9", title="SMART Sniffer (testnas)")
    flow = _flow([entry], SOURCE_USER, {"testnas-box:9099": ("testnas", ["SER-T1"], "")})
    result = _user(flow, "testnas-box")
    assert result["type"] == "create_entry"


def test_pick_best_ip_prefers_tailscale_over_link_local():
    info = _discovery("nas", "169.254.10.20", "100.101.102.103", "9.9.9.9")
    assert config_flow.SmartSnifferConfigFlow._pick_best_ip(info) == "100.101.102.103"
    info = _discovery("nas", "169.254.10.20", "100.101.102.103", "192.168.40.5")
    assert config_flow.SmartSnifferConfigFlow._pick_best_ip(info) == "192.168.40.5"
