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
