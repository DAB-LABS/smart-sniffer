"""SMART data that was there and is not now (GH #61), smart_presence.py.

A drive that never gave SMART data stays UNSUPPORTED, as before. One that
gave it and stopped is "lost": the coordinator raises no notification for it
and the attention sensors show unknown, until the data comes back.
"""

from __future__ import annotations

import importlib

import pytest

presence = importlib.import_module("custom_components.smart_sniffer.smart_presence")
attention = importlib.import_module("custom_components.smart_sniffer.attention")

# What the agent publishes for a drive that answers smartctl with its identity
# only: the macOS SAT driver case. Fake identity.
NO_SMART = {
    "model_name": "FAKE MODEL 2TB",
    "serial_number": "FAKE00000061",
    "device": {"type": "ata", "protocol": "ATA"},
    "smart_support": {"available": True, "enabled": False},
}


def _no_smart(**agent_fields):
    payload = {"id": "fake-drive", "readable": True, "smart_data": dict(NO_SMART)}
    payload.update(agent_fields)
    return payload


@pytest.fixture
def healthy(drive):
    def _healthy():
        payload = drive("ata_healthy")
        payload["id"] = "fake-drive"
        payload["readable"] = True
        payload["smart_available"] = True
        return payload

    return _healthy


def test_the_payloads_mean_what_the_tests_say(healthy):
    assert presence.has_usable_smart_data(healthy())
    assert not presence.has_usable_smart_data(_no_smart())
    # The rules module still calls the drive UNSUPPORTED either way: the
    # Control Center reads evaluate_attention, whose interface is unchanged.
    assert attention.evaluate_attention(_no_smart())[0] == attention.STATE_UNSUPPORTED


def test_never_had_smart_is_not_lost():
    p = presence.SmartPresence()
    for _ in range(3):
        assert p.update("fake-drive", _no_smart(smart_available=False,
                                                smart_unavailable_reason="disabled")) is None
        assert not p.is_lost("fake-drive")


def test_old_agent_without_the_fields_never_had_smart():
    p = presence.SmartPresence()
    assert p.update("fake-drive", {"id": "fake-drive", "smart_data": None}) is None
    assert not p.is_lost("fake-drive")


def test_available_then_lost_then_back(healthy):
    p = presence.SmartPresence()
    events = []
    sequence = [healthy(), healthy(), _no_smart(), _no_smart(), _no_smart(), healthy(), healthy()]
    lost_flags = []
    for payload in sequence:
        events.append(p.update("fake-drive", payload))
        lost_flags.append(p.is_lost("fake-drive"))
    assert events == [None, None, presence.EVENT_LOST, None, None, presence.EVENT_BACK, None]
    assert lost_flags == [False, False, True, True, True, False, False]


def test_agent_memory_alone_marks_it_lost():
    # Home Assistant restarted while SMART was missing; the agent did not.
    p = presence.SmartPresence()
    assert p.update("fake-drive", _no_smart(smart_lost=True)) == presence.EVENT_LOST
    assert p.is_lost("fake-drive")


def test_integration_memory_alone_keeps_it_lost(healthy):
    # The agent restarted while SMART was missing; Home Assistant did not.
    p = presence.SmartPresence()
    p.update("fake-drive", healthy())
    assert p.update("fake-drive", _no_smart(smart_lost=True)) == presence.EVENT_LOST
    assert p.update("fake-drive", _no_smart()) is None
    assert p.is_lost("fake-drive")


def test_drives_are_tracked_separately(healthy):
    p = presence.SmartPresence()
    p.update("fake-drive", healthy())
    p.update("other-drive", _no_smart())
    p.update("fake-drive", _no_smart())
    assert p.is_lost("fake-drive") and not p.is_lost("other-drive")


def test_forget_starts_afresh(healthy):
    p = presence.SmartPresence()
    p.update("fake-drive", healthy())
    p.update("fake-drive", _no_smart())
    p.forget("fake-drive")
    assert not p.is_lost("fake-drive")
    assert p.update("fake-drive", _no_smart()) is None


def test_smart_lost_needs_no_data_now(healthy):
    assert not presence.smart_lost(healthy(), had_smart=True)
    assert not presence.smart_lost({**healthy(), "smart_lost": True}, had_smart=False)
    assert presence.smart_lost(_no_smart(), had_smart=True)
    assert not presence.smart_lost(_no_smart(), had_smart=False)
    assert not presence.smart_lost(_no_smart(smart_lost="yes"), had_smart=False)
