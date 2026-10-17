"""The host part of device names (GH #50, v0.8.4). Names and addresses are
invented. Plain tests: host_name.py imports nothing from Home Assistant."""

from __future__ import annotations

from custom_components.smart_sniffer.host_name import (
    address_label,
    agent_name,
    app_kind_change,
    clean_host,
    discovered_name,
    disk_usage_name,
    drive_name,
    entry_host,
    legacy_host,
    pick_shared_host,
    pool_name,
)

APP = "0449a086-smart-sniffer-agent"


def test_clean_host():
    # The case the agent reports is kept (LAPTOP-2ONH2TAS on Windows).
    assert clean_host("NAS1.lan.") == "NAS1"
    assert clean_host("nas1.local") == "nas1"
    assert clean_host("nas1.LOCAL.") == "nas1"
    assert clean_host(" LAPTOP-2ONH2TAS ") == "LAPTOP-2ONH2TAS"
    assert clean_host(APP) == APP
    for none in ("localhost", "LocalHost", "localhost.localdomain", "a1b2c3d4e5f6",
                 "A1B2C3D4E5F6", "", "  ", None, 42):
        assert clean_host(none) is None, none


def test_address_label():
    assert address_label("192.168.1.20") == "192.168.1.20"
    assert address_label("[fe80::1]") == "fe80::1"
    assert address_label("fe80::1") == "fe80::1"
    assert address_label("NAS1.lan.") == "NAS1.lan"
    # Never a port.
    assert address_label("192.168.1.20:9099") == "192.168.1.20"
    assert address_label("[fe80::1]:9099") == "fe80::1"
    assert address_label("") is None and address_label(None) is None


def test_entry_host_prefers_the_stored_host_name():
    assert entry_host({"host": "192.168.1.20", "agent_hostname": "nas1"}, "x") == "nas1"
    assert entry_host({"host": "192.168.1.20"}, "SMART Sniffer (192.168.1.20:9099)") == "192.168.1.20"


def test_entry_host_falls_back_to_the_address():
    # A stored name that names no machine.
    assert entry_host({"host": "192.168.1.20", "agent_hostname": "localhost"}, "") == "192.168.1.20"
    # The title is not parsed: a discovered agent before v0.8.3 is named by its address.
    assert entry_host({"host": "192.168.1.20"}, "SMART Sniffer (nas1)") == "192.168.1.20"
    assert entry_host({"host": "192.168.1.20", "agent_hostname": "nas2"}, "SMART Sniffer (nas1)") == "nas2"
    assert entry_host({"host": "NAS1.lan"}, "") == "NAS1.lan"
    assert entry_host({}, "") is None


def test_entry_host_for_the_app():
    for data, title in (
        ({"host": "192.168.1.5", "agent_hostname": APP}, "SMART Sniffer (192.168.1.5:9099)"),
        ({"host": "192.168.1.5"}, f"SMART Sniffer ({APP}:9099)"),
        ({"host": APP}, "My App"),
        ({"host": "local-smart-sniffer-agent"}, ""),
    ):
        assert entry_host({**data, "agent_mock": False}, title) == "HAOS", data
        assert entry_host({**data, "agent_mock": True}, title) == "Replica", data
        assert entry_host(data, title) is None, data
    for name in ("smart-sniffer-agent", "0449a086x-smart-sniffer-agent", "0449A08-smart-sniffer-agent"):
        assert entry_host({"host": name, "agent_mock": False}, "") == name
    # A standalone mock is named by its own host name.
    assert entry_host({"host": "10.0.0.9", "agent_hostname": "nas1", "agent_mock": True}, "") == "nas1"


def test_app_kind_change():
    title = f"SMART Sniffer ({APP}:9100)"
    assert app_kind_change({"host": APP}, title, {"hostname": APP, "mock": True}) == {"agent_mock": True}
    # An App agent before v0.8.3: no host name, no mock.
    assert app_kind_change({"host": APP}, title, {"status": "ok"}) == {"agent_mock": False}
    # Added by the HA host's address: the live host name tells.
    assert app_kind_change({"host": "192.168.1.5"}, "", {"hostname": APP}) == {"agent_mock": False}
    # Not the App.
    assert app_kind_change({"host": "192.168.1.20"}, "", {"hostname": "nas1", "mock": True}) == {}
    # Unchanged.
    assert app_kind_change({"host": APP, "agent_mock": True}, title, {"mock": True}) == {}
    # Another agent answers at the App's address; a failed read.
    assert app_kind_change({"host": APP}, title, {"hostname": "nas1"}) == {}
    assert app_kind_change({"host": APP}, title, None) == {}


def test_builders():
    model, serial = "Samsung 870 EVO", "TEST-SER-1"
    assert drive_name("nas1", model, serial) == "nas1 Samsung 870 EVO (TEST-SER-1)"
    assert drive_name("192.168.1.20", model, serial) == "192.168.1.20 Samsung 870 EVO (TEST-SER-1)"
    assert pool_name("nas1", "rpool") == "nas1 ZFS pool rpool"
    assert pool_name("192.168.1.20", "rpool") == "192.168.1.20 ZFS pool rpool"
    assert disk_usage_name("nas1") == "nas1 Disk Usage"
    assert disk_usage_name("192.168.1.20") == "192.168.1.20 Disk Usage"
    assert agent_name("SMART Sniffer (192.168.1.20:9099)", "192.168.1.20", "nas1") == "SMART Sniffer (nas1)"
    assert agent_name("SMART Sniffer (nas1)", "192.168.1.20", "192.168.1.20") == "SMART Sniffer (192.168.1.20)"
    assert agent_name("SMART Sniffer (x:9100)", APP, "HAOS") == "SMART Sniffer (HAOS)"
    # A title the user gave is kept.
    assert agent_name("Basement NAS", "192.168.1.20", "nas1") == "Basement NAS"


def test_builders_without_a_host_keep_the_v083_names():
    """An App entry before its first health read."""
    assert drive_name(None, "M", "S") == "M (S)"
    assert pool_name(None, "rpool", "box:9099") == "ZFS pool rpool (box:9099)"
    assert disk_usage_name(None, "box:9099") == "Disk Usage (box:9099)"
    assert agent_name("SMART Sniffer (box:9099)", "box", None) == "SMART Sniffer (box:9099)"
    assert agent_name("", "box", None) == "SMART Sniffer (box)"


def test_legacy_host_is_the_title_text_or_the_address():
    assert legacy_host("SMART Sniffer (nas1)", "192.168.1.20") == "nas1"
    assert legacy_host("SMART Sniffer (192.168.1.20:9099)", "192.168.1.20") == "192.168.1.20:9099"
    assert legacy_host("Basement NAS", "192.168.1.20") == "192.168.1.20"


def test_pick_shared_host():
    owners = [("nas1", "E2"), ("nas0", "E3"), (None, "E1")]
    assert pick_shared_host(owners) == "nas0"
    assert pick_shared_host(reversed(owners)) == "nas0"
    # Ties by entry id: same answer either way.
    assert pick_shared_host([("nas1", "E2"), ("nas1", "E1")]) == "nas1"
    assert pick_shared_host([(None, "E1"), ("", "E2")]) is None
    # Case does not change the pick.
    assert pick_shared_host([("Nas1", "E1"), ("nas0", "E2")]) == "nas0"
    assert pick_shared_host([("NAS0", "E1"), ("nas1", "E2")]) == "NAS0"
    assert pick_shared_host([]) is None


# --- the name discovery found (owner decision after the VM999 bench) -----------------


def test_a_discovered_older_agent_keeps_its_discovery_name():
    """ZimaOS and brookdale on agent 0.6.2: no host name in /api/health, but
    discovery named them. The name keeps its case."""
    assert entry_host({"host": "10.0.0.10"}, "SMART Sniffer (ZimaOS)", "zeroconf") == "ZimaOS"
    assert entry_host({"host": "10.0.10.12"}, "SMART Sniffer (brookdale)", "zeroconf") == "brookdale"
    # The agent's own host name still comes first.
    assert entry_host({"host": "10.0.0.10", "agent_hostname": "zima-new"},
                      "SMART Sniffer (ZimaOS)", "zeroconf") == "zima-new"


def test_an_entry_added_by_address_keeps_the_address():
    for source in ("user", None, "import"):
        assert entry_host({"host": "10.0.10.210"}, "SMART Sniffer (10.0.10.210:9099)", source) == "10.0.10.210"
        assert entry_host({"host": "10.0.0.10"}, "SMART Sniffer (ZimaOS)", source) == "10.0.0.10"


def test_discovered_name_rejects_what_names_no_machine():
    for text in ("10.0.0.10:9099", "10.0.0.10", "fe80::1", "[fe80::1]:9099", "localhost",
                 "a1b2c3d4e5f6", APP, f"{APP}:9100", "local-smart-sniffer-agent", ""):
        assert discovered_name(f"SMART Sniffer ({text})", "zeroconf") is None, text
    assert discovered_name("Basement NAS", "zeroconf") is None
    assert discovered_name("SMART Sniffer (nas1.local)", "zeroconf") == "nas1"
    # An App entry found by discovery is still HAOS or Replica.
    assert entry_host({"host": "172.30.32.1", "agent_mock": False}, f"SMART Sniffer ({APP})", "zeroconf") == "HAOS"
    assert entry_host({"host": "172.30.32.1"}, f"SMART Sniffer ({APP})", "zeroconf") is None
