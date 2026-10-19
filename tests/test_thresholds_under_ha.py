"""A threshold save refreshes the entry instead of reloading it (v0.8.5).

Runs the entry's setup in a real Home Assistant core with its own config
entries, registries, state machine and entity platforms, against a stand-in
agent with one NVMe drive at 50% wear (built-in limit 90%). The options
flow's own steps write the changes; only the reload is replaced, by a
recorder, since this suite cannot load the integration through the loader.
Skipped without Home Assistant, like the other *_under_ha tests.

Host names and serials are invented.
"""

from __future__ import annotations

import importlib
import logging
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")

import aiohttp  # noqa: E402

from homeassistant import config_entries  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from homeassistant.helpers.entity_platform import EntityPlatform  # noqa: E402

from tests.test_device_names_under_registry import (  # noqa: E402
    _make_entry,
    _package_init,
    _run,
)

DOMAIN = "smart_sniffer"
coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")
config_flow = importlib.import_module("custom_components.smart_sniffer.config_flow")
sensor = importlib.import_module("custom_components.smart_sniffer.sensor")
binary_sensor = importlib.import_module("custom_components.smart_sniffer.binary_sensor")
diagnostics = importlib.import_module("custom_components.smart_sniffer.diagnostics")

WEAR = "SSD Wear Percent Used"
ATTENTION = "sensor.nas1_acme_nvme_1tb_ser_n1_attention_needed"
DRIVE = "nvme0"
NOTICE = f"smart_sniffer_attention_{DRIVE}"


class _Agent:
    """The agent at 192.168.1.20:9099, for the coordinators and the flow."""

    def __init__(self):
        self.up = True
        self.drive_fetches = 0

    def get(self, url, headers=None, timeout=None):
        agent = self
        path = url.split("9099", 1)[1]

        class _Resp:
            async def __aenter__(self):
                if not agent.up:
                    raise aiohttp.ClientConnectionError("agent down")
                if path == f"/api/drives/{DRIVE}":
                    agent.drive_fetches += 1
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def json(self):
                if path == "/api/drives":
                    return [{"id": DRIVE, "serial": "SER-N1"}]
                if path == "/api/health":
                    return {"status": "ok", "version": "0.8.4", "hostname": "nas1",
                            "os": "linux", "uptime_seconds": 100}
                return {
                    "id": DRIVE, "model": "Acme NVMe 1TB", "serial": "SER-N1",
                    "protocol": "NVMe", "device_path": "/dev/nvme0",
                    "smart_data": {
                        "smart_status": {"passed": True},
                        "nvme_smart_health_information_log": {
                            "critical_warning": 0, "media_errors": 0,
                            "available_spare": 100, "available_spare_threshold": 10,
                            "percentage_used": 50, "temperature": 35,
                            "power_on_hours": 1000,
                        },
                    },
                }

        return _Resp()


@pytest.fixture
def agent(monkeypatch):
    agent = _Agent()
    for module in (coordinator_mod, config_flow):
        monkeypatch.setattr(module, "async_get_clientsession", lambda hass: agent)
    return agent


@pytest.fixture(autouse=True)
def _options_flow_base(monkeypatch):
    """From 2024.11 the options flow's deprecated base class reports itself.
    In an install the frame is a custom integration and that is a log line;
    here no integration frame is found and it would raise instead."""
    if hasattr(config_entries, "report_usage"):
        monkeypatch.setattr(config_entries, "report_usage", lambda *a, **kw: None)


@pytest.fixture
def notes(monkeypatch):
    """Persistent notifications created and dismissed, in order."""
    notes = []
    monkeypatch.setattr(coordinator_mod, "pn_create",
                        lambda hass, **kw: notes.append(("create", kw["notification_id"])))
    monkeypatch.setattr(coordinator_mod, "pn_dismiss",
                        lambda hass, nid: notes.append(("dismiss", nid)))
    return notes


class _Harness:
    def __init__(self, hass, init, entry):
        self.hass, self.init, self.entry = hass, init, entry
        self.reloads = []
        self.setups = 0
        self.states = []
        hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            lambda event: self.states.append(
                (event.data["entity_id"], event.data["new_state"].state)
            ) if event.data.get("new_state") is not None else None,
        )

    @property
    def coordinator(self):
        return self.hass.data[DOMAIN][self.entry.entry_id]["coordinator"]

    def entity_ids(self):
        registry = er.async_get(self.hass)
        return sorted(e.entity_id for e in er.async_entries_for_config_entry(
            registry, self.entry.entry_id))

    def state(self, entity_id):
        return self.hass.states.get(entity_id)

    def unavailable(self):
        return [eid for eid, state in self.states if state == STATE_UNAVAILABLE]

    def cooled_down(self):
        """The request-refresh debouncer's cooldown over, as after 10 s."""
        self.coordinator._debounced_refresh.async_cancel()

    async def options(self, step, user_input, drive_id=None):
        flow = config_flow.SmartSnifferOptionsFlow(self.entry)
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        flow.flow_id = "options"
        flow._threshold_drive_id = drive_id
        result = await getattr(flow, f"async_step_{step}")(user_input)
        assert result["type"] == "create_entry", result
        await self.hass.async_block_till_done()


async def _set_up(hass, thresholds=None):
    """Set the entry up the way core does, platforms included."""
    init = _package_init()
    await ir.async_load(hass)
    data = {"host": "192.168.1.20", "port": 9099, "token": "", "scan_interval": 60}
    if thresholds is not None:
        data["thresholds"] = thresholds
    entry = _make_entry("E1", "SMART Sniffer (nas1)", data)
    hass.config_entries._entries["E1"] = entry
    harness = _Harness(hass, init, entry)

    async def forward(entry, platforms):
        for platform in platforms:
            module = sensor if platform == "sensor" else binary_sensor
            await EntityPlatform(
                hass=hass, logger=logging.getLogger(__name__), domain=str(platform),
                platform_name=DOMAIN, platform=module,
                scan_interval=timedelta(seconds=30), entity_namespace=None,
            ).async_setup_entry(entry)

    async def reload(entry_id):
        harness.reloads.append(entry_id)
        return True

    hass.config_entries.async_forward_entry_setups = forward
    hass.config_entries.async_reload = reload

    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    config_entries.current_entry.set(entry)
    harness.setups += 1
    assert await init.async_setup_entry(hass, entry)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    await hass.async_block_till_done()
    harness.states.clear()
    return harness


def _attention(h):
    state = h.state(ATTENTION)
    return state.state, state.attributes.get("reasons")


async def _other_readers(h):
    """Attention as the get_drive_data service and diagnostics report it."""
    response = await h.hass.services.async_call(
        DOMAIN, "get_drive_data", {"config_entry_id": "E1"},
        blocking=True, return_response=True,
    )
    diag = await diagnostics.async_get_config_entry_diagnostics(h.hass, h.entry)
    attention = diag["drives"][DRIVE]["attention"]
    return (response["drives"][DRIVE]["attention"]["state"],
            attention["state"], attention["thresholds"])


def test_a_threshold_save_refreshes_without_a_reload(tmp_path, agent, notes):
    async def body(hass):
        h = await _set_up(hass)
        coordinator, ids = h.coordinator, h.entity_ids()
        assert ATTENTION in ids
        assert _attention(h) == ("NO", ["No issues detected"])
        assert await _other_readers(h) == ("NO", "NO", {})
        polls = agent.drive_fetches

        # Lower the wear limit below the 50% reading.
        await h.options("threshold_form", {WEAR: 40}, drive_id=DRIVE)
        assert h.entry.data["thresholds"] == {DRIVE: {WEAR: 40}}
        assert h.reloads == [] and h.setups == 1
        assert h.entry.state is ConfigEntryState.LOADED
        assert h.coordinator is coordinator and h.entity_ids() == ids
        assert agent.drive_fetches == polls + 1  # one refresh
        state, reasons = _attention(h)
        assert state == "MAYBE" and "wear at 50%" in reasons[0]
        assert notes == [("create", NOTICE)]
        assert await _other_readers(h) == ("MAYBE", "MAYBE", {WEAR: 40})
        assert h.unavailable() == []
        assert all(h.state(eid).state != STATE_UNAVAILABLE for eid in ids
                   if h.state(eid) is not None)

        # Back to the default: the override goes, the attention clears.
        h.cooled_down()
        await h.options("threshold_form", {WEAR: 90}, drive_id=DRIVE)
        assert h.entry.data["thresholds"] == {}
        assert h.reloads == [] and h.coordinator is coordinator
        assert agent.drive_fetches == polls + 2
        assert _attention(h) == ("NO", ["No issues detected"])
        assert notes[-1] == ("dismiss", NOTICE)
        assert await _other_readers(h) == ("NO", "NO", {})
        assert h.unavailable() == []
    _run(tmp_path, body)


def test_a_save_that_changes_nothing_does_not_poll(tmp_path, agent, notes):
    async def body(hass):
        h = await _set_up(hass, thresholds={DRIVE: {WEAR: 40}})
        assert _attention(h)[0] == "MAYBE"
        polls = agent.drive_fetches
        await h.options("threshold_form", {WEAR: 40}, drive_id=DRIVE)
        assert h.reloads == [] and agent.drive_fetches == polls
    _run(tmp_path, body)


def test_a_threshold_save_with_the_agent_down(tmp_path, agent, notes):
    """The refresh fails as a scheduled poll would; nothing reloads, and
    the next poll that succeeds applies the new limit."""
    async def body(hass):
        h = await _set_up(hass)
        coordinator, ids = h.coordinator, h.entity_ids()
        agent.up = False
        await h.options("threshold_form", {WEAR: 40}, drive_id=DRIVE)
        assert h.reloads == [] and h.coordinator is coordinator
        assert h.entry.state is ConfigEntryState.LOADED
        assert coordinator.last_update_success is False
        assert h.entity_ids() == ids

        agent.up = True
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _attention(h)[0] == "MAYBE"
        assert h.reloads == []
    _run(tmp_path, body)


def test_a_settings_change_still_reloads(tmp_path, agent, notes):
    async def body(hass):
        h = await _set_up(hass)
        polls = agent.drive_fetches
        await h.options("settings", {"port": 9099, "token": "new-token", "scan_interval": 120})
        assert h.reloads == ["E1"]
        assert agent.drive_fetches == polls  # the reload polls, not the listener
    _run(tmp_path, body)


@pytest.mark.parametrize("change", [
    {"token": "new-token"},
    {"port": 9100},
    {"scan_interval": 120},
    {"host": "192.168.1.21"},
])
def test_thresholds_saved_with_another_change_reload(tmp_path, agent, notes, change):
    async def body(hass):
        h = await _set_up(hass)
        polls = agent.drive_fetches
        hass.config_entries.async_update_entry(
            h.entry, data={**h.entry.data, **change, "thresholds": {DRIVE: {WEAR: 40}}}
        )
        await hass.async_block_till_done()
        assert h.reloads == ["E1"]
        assert agent.drive_fetches == polls
    _run(tmp_path, body)


def test_a_title_change_still_reloads(tmp_path, agent, notes):
    async def body(hass):
        h = await _set_up(hass)
        hass.config_entries.async_update_entry(h.entry, title="SMART Sniffer (nas one)")
        await hass.async_block_till_done()
        assert h.reloads == ["E1"]
    _run(tmp_path, body)
