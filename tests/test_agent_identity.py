"""The rule that decides whether an agent already has a config entry
(smart-sniffer-app#12, GH #64). Names and serials are invented.

A host name alone never decides it: two OpenWrt routers share the default
name. The existing entry's agent must answer with the same host name and
report the same drives as the new address.
"""

from __future__ import annotations

from custom_components.smart_sniffer.agent_identity import (
    confirms_same_agent,
    drive_digest,
    drive_set,
    identity_changes,
    norm_agent_id,
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


# --- what an entry keeps about its agent ---------------------------------------

AGENT_ID = "3f9c0e5a7b1d4c2e8a6f0b9d1e2c3a4b"


def test_norm_agent_id():
    assert norm_agent_id(" 3F9C0E5A7B1D4C2E8A6F0B9D1E2C3A4B ") == AGENT_ID
    assert norm_agent_id("short") == ""
    assert norm_agent_id("has space here") == ""
    assert norm_agent_id(None) == ""
    assert norm_agent_id(12345678) == ""


def test_drive_digest_is_by_serial_only():
    assert drive_digest(DRIVES) == drive_digest(list(reversed(DRIVES)))
    assert len(drive_digest(DRIVES)) == 32 and "SER-1" not in drive_digest(DRIVES)
    assert drive_digest(DRIVES) != drive_digest(DRIVES[:1])
    # A drive without a serial is left out; none at all gives "".
    assert drive_digest([*DRIVES, {"id": "/dev/sdz", "serial": ""}]) == drive_digest(DRIVES)
    assert drive_digest([{"id": "/dev/sda"}]) == ""
    assert drive_digest([]) == "" and drive_digest(None) == ""


def test_identity_first_poll_stores_name_drives_and_id():
    health = {**HEALTH, "agent_id": AGENT_ID}
    changes, other = identity_changes({}, health, DRIVES)
    assert not other
    assert changes == {
        "agent_hostname": "TestRouter",  # stored as reported; compared without case
        "agent_drives": drive_digest(DRIVES),
        "agent_id": AGENT_ID,
    }
    # Nothing new: nothing to store.
    assert identity_changes(changes, health, DRIVES) == ({}, False)


def test_identity_follows_its_own_agent():
    stored = {"agent_hostname": "TestRouter", "agent_drives": drive_digest(DRIVES)}
    # A drive added: the digest follows.
    more = [*DRIVES, {"id": "id-3", "serial": "SER-3"}]
    assert identity_changes(stored, HEALTH, more) == ({"agent_drives": drive_digest(more)}, False)
    # An agent_id appears (agent upgraded) or changes (reinstalled).
    assert identity_changes(stored, {**HEALTH, "agent_id": AGENT_ID}, DRIVES)[0] == {
        "agent_id": AGENT_ID
    }


def test_identity_kept_when_the_address_answers_as_another_host():
    """S6: DHCP gave the entry's address to another agent. What the entry
    knew about its own agent is kept, agent_id included."""
    stored = {"agent_hostname": "testrouter", "agent_drives": "d" * 32, "agent_id": AGENT_ID}
    other = {"status": "ok", "hostname": "othernas", "agent_id": "f" * 32}
    assert identity_changes(stored, other, [{"id": "x", "serial": "SER-9"}]) == ({}, True)


def test_identity_nothing_from_an_older_agent_or_a_failed_read():
    assert identity_changes({}, {"status": "ok"}, DRIVES) == ({}, False)
    assert identity_changes({}, None, DRIVES) == ({}, False)


def test_identity_empty_values_never_replace_stored_ones():
    stored = {"agent_hostname": "TestRouter", "agent_drives": "d" * 32, "agent_id": AGENT_ID}
    assert identity_changes(stored, HEALTH, []) == ({}, False)
    assert identity_changes(stored, {**HEALTH, "agent_id": "bad id"}, None) == ({}, False)


# --- a renamed host (v0.8.4): agent_id first, the drive digest without it -----------

OTHER_DRIVES = [{"id": "id-8", "serial": "SER-8"}]


def test_identity_rename_with_the_same_agent_id_follows_it():
    """agent_id decides when both sides have one, even with other drives."""
    stored = {"agent_hostname": "testrouter", "agent_drives": drive_digest(DRIVES), "agent_id": AGENT_ID}
    health = {"status": "ok", "hostname": "NewName", "agent_id": AGENT_ID}
    assert identity_changes(stored, health, OTHER_DRIVES) == (
        {"agent_hostname": "NewName", "agent_drives": drive_digest(OTHER_DRIVES)},
        False,
    )


def test_identity_rename_with_another_agent_id_is_another_agent():
    """Same drives (a cloned VM) but another agent_id: not the same agent."""
    stored = {"agent_hostname": "testrouter", "agent_drives": drive_digest(DRIVES), "agent_id": AGENT_ID}
    health = {"status": "ok", "hostname": "newname", "agent_id": "f" * 32}
    assert identity_changes(stored, health, DRIVES) == ({}, True)


def test_identity_rename_without_agent_id_uses_the_drive_digest():
    with_id = {"status": "ok", "hostname": "newname", "agent_id": AGENT_ID}
    without_id = {"status": "ok", "hostname": "newname"}
    for stored_id, health in (({}, with_id), ({"agent_id": AGENT_ID}, without_id), ({}, without_id)):
        stored = {"agent_hostname": "testrouter", "agent_drives": drive_digest(DRIVES), **stored_id}
        changes, other = identity_changes(stored, health, DRIVES)
        assert not other and changes["agent_hostname"] == "newname", (stored_id, health)
        # Other drives, or none readable: another agent.
        assert identity_changes(stored, health, OTHER_DRIVES) == ({}, True)
        assert identity_changes(stored, health, None) == ({}, True)
    # No stored digest: never shown to be the same.
    stored = {"agent_hostname": "testrouter"}
    assert identity_changes(stored, without_id, DRIVES) == ({}, True)


def test_identity_a_change_of_case_is_stored_not_another_agent():
    """v0.8.3 stored names in lower case; the device names show the case the
    agent reports, so the next poll stores it. Not a rename, not another agent."""
    stored = {"agent_hostname": "laptop-2onh2tas", "agent_drives": "d" * 32, "agent_id": AGENT_ID}
    health = {"status": "ok", "hostname": "LAPTOP-2ONH2TAS", "agent_id": AGENT_ID}
    assert identity_changes(stored, health, None) == ({"agent_hostname": "LAPTOP-2ONH2TAS"}, False)
