"""The rule that decides whether an agent already has a config entry
(smart-sniffer-app#12, GH #64). Names and serials are invented.

A host name alone never decides it: two OpenWrt routers share the default
name. The existing entry's agent must answer with the same host name and
report the same drives as the new address.
"""

from __future__ import annotations

from custom_components.smart_sniffer.agent_identity import (
    confirms_same_agent,
    drive_set,
    norm_host,
)

HEALTH = {"status": "ok", "hostname": "TestRouter"}
DRIVES = [{"id": "id-1", "serial": "SER-1"}, {"id": "id-2", "serial": "SER-2"}]


def test_norm_host():
    assert norm_host("NAS.local.") == "nas"
    assert norm_host(" Router ") == "router"
    assert norm_host(None) == ""


def test_drive_set():
    assert drive_set(DRIVES) == {"SER-1", "SER-2"}
    assert drive_set([{"id": "id-9", "serial": ""}]) == {"id-9"}
    assert drive_set([]) == frozenset()
    assert drive_set({"error": "unauthorized"}) is None
    assert drive_set([{"model": "x"}]) is None


def test_same_hostname_and_drives():
    assert confirms_same_agent("testrouter", HEALTH, DRIVES, list(reversed(DRIVES)))


def test_hostname_alone_is_not_enough():
    other = [{"id": "id-3", "serial": "SER-3"}]
    assert not confirms_same_agent("testrouter", HEALTH, DRIVES, other)
    assert not confirms_same_agent("testrouter", HEALTH, DRIVES, None)  # no count either


def test_unreadable_new_address_falls_back_to_the_drive_count():
    assert confirms_same_agent("testrouter", HEALTH, DRIVES, None, "2")
    assert not confirms_same_agent("testrouter", HEALTH, DRIVES, None, "3")
    assert not confirms_same_agent("testrouter", HEALTH, DRIVES, None, "?")


def test_existing_side_must_answer():
    assert not confirms_same_agent("testrouter", None, DRIVES, DRIVES)
    assert not confirms_same_agent("testrouter", HEALTH, None, DRIVES)
    assert not confirms_same_agent("testrouter", {"status": "ok"}, DRIVES, DRIVES)


def test_other_hostname_or_no_drives():
    assert not confirms_same_agent("othernas", HEALTH, DRIVES, DRIVES)
    assert not confirms_same_agent("", HEALTH, DRIVES, DRIVES)
    assert not confirms_same_agent("testrouter", HEALTH, [], [])


# --- discovery address preference ---------------------------------------------

from custom_components.smart_sniffer.agent_identity import ip_score  # noqa: E402


def test_ip_order_lan_then_cgnat_then_link_local():
    """Private LAN > container 172.16/12 > CGNAT/Tailscale > IPv6 > public >
    link-local. Before v0.8.3, 100.64/10 fell to the public score (Python's
    is_private is False for it) and 169.254/16 outranked it."""
    order = [
        "192.168.40.1",
        "10.1.2.3",
        "172.20.0.5",
        "100.101.102.103",
        "fd00::5",
        "9.9.9.9",
        "169.254.10.20",
    ]
    scores = [ip_score(ip) for ip in order]
    assert scores == sorted(scores)
    assert ip_score("192.168.40.1") == ip_score("10.1.2.3")
    assert ip_score("100.101.102.103") < ip_score("169.254.10.20")
    assert ip_score("100.101.102.103") < ip_score("9.9.9.9")
    assert ip_score("172.217.1.1") == ip_score("9.9.9.9")  # public, not RFC 1918
    assert ip_score("not-an-ip") == 99
    assert sorted(["169.254.10.20", "100.101.102.103"], key=ip_score)[0] == "100.101.102.103"
