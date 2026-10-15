"""The platforms and coordinator under real Home Assistant, when installed.

CI's Python job has no Home Assistant, so this module skips there; the plain
tests cover the decisions (entity_plan, devstat, attention). Run it in a venv
with the declared floor (homeassistant==2024.4.0) to check the entities
themselves: the v0.7.0 golden rebuilt by the v0.8.0 platforms, the Data
Written and Data Read sensors, the listener, and the D11 announcement.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime

import pytest

pytest.importorskip("homeassistant")

from tests.devstat_payloads import HGST_WRITES, agent_drive  # noqa: E402
from tests.golden_payloads import ENTRY_ID, GOLDEN, REGISTERED_POOLS, golden_payload  # noqa: E402

DOMAIN = "smart_sniffer"
sensor = importlib.import_module("custom_components.smart_sniffer.sensor")
binary = importlib.import_module("custom_components.smart_sniffer.binary_sensor")
coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")
devstat = importlib.import_module("custom_components.smart_sniffer.devstat")
smart_presence = importlib.import_module("custom_components.smart_sniffer.smart_presence")


class _Entry:
    def __init__(self, force=False):
        self.entry_id = ENTRY_ID
        self.data = {"host": "10.0.0.5", "port": 9099, "scan_interval": 60, "force_update": force}
        self.title = "SMART Sniffer (fixturehost)"
        self.options = {}
        self.unload = []

    def async_on_unload(self, func):
        self.unload.append(func)


class _Hass:
    def __init__(self):
        self.data = {}


class _Coord:
    def __init__(self, hass, entry, data):
        self.hass = hass
        self.config_entry = entry
        self.data = data
        self.last_update_success = True
        self._hostname = "fixturehost"
        self.host = "10.0.0.5"
        self.created = {}
        self.drive_classes = {}
        self.smart_presence = smart_presence.SmartPresence()
        self.listeners = []

    def registered_pool_names(self):
        return list(REGISTERED_POOLS)

    def async_add_listener(self, func, *args):
        self.listeners.append(func)
        return lambda: None


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value") and not isinstance(v, (int, float, str)):
        return v.value
    return v


def _describe(e, platform):
    desc = getattr(e, "entity_description", None)
    rec = {
        "platform": platform,
        "unique_id": e.unique_id,
        "class": type(e).__name__,
        "name": (desc.name if desc is not None and isinstance(desc.name, str)
                 else getattr(e, "_attr_name", None)),
        "translation_key": e.translation_key,
        "device_class": _jsonable(e.device_class),
        "entity_category": _jsonable(e.entity_category),
        "enabled_default": e.entity_registry_enabled_default,
        "force_update": e.force_update,
        "device_identifiers": sorted(list(x) for x in (e.device_info or {}).get("identifiers", [])),
    }
    if platform == "sensor":
        rec["state_class"] = _jsonable(e.state_class)
        rec["unit"] = e.native_unit_of_measurement
        rec["value"] = _jsonable(e.native_value)
    else:
        rec["value"] = e.is_on
    rec["attributes"] = _jsonable(e.extra_state_attributes)
    rec["icon"] = e.icon
    rec["available"] = e.available
    return rec


def _setup(data, force=False):
    hass, entry = _Hass(), _Entry(force)
    coord = _Coord(hass, entry, data)
    health = _Coord(hass, entry, {"connected": True, "version": "0.7.0", "os": "linux",
                                  "uptime_seconds": 100,
                                  "last_seen": "2026-10-01T12:00:00+00:00"})
    hass.data[DOMAIN] = {entry.entry_id: {"coordinator": coord, "health_coordinator": health,
                                          "agent_device_id": "agentdevice0001"}}
    added = {"sensor": [], "binary_sensor": []}

    async def run():
        for platform, mod in (("sensor", sensor), ("binary_sensor", binary)):
            await mod.async_setup_entry(
                hass, entry,
                lambda ents, update_before_add=False, p=platform: added[p].extend(ents),
            )

    asyncio.run(run())
    return coord, added


@pytest.mark.parametrize("force", [False, True])
def test_the_v070_golden_rebuilt(force):
    with (GOLDEN / "v070-entities.json").open(encoding="utf-8") as handle:
        golden = json.load(handle)["force_update_on" if force else "force_update_off"]
    data = golden_payload()
    _, added = _setup(data, force)
    rows = [_describe(e, "sensor") for e in added["sensor"]] + [
        _describe(e, "binary_sensor") for e in added["binary_sensor"]
    ]
    assert rows == golden


def _devstat_drive(name, **kwargs):
    drive = agent_drive(name, **kwargs)
    drive[devstat.DEVSTAT_KEY], _ = devstat.merge_devstat(drive, None)
    return drive


def test_data_written_and_read_entities():
    drive = _devstat_drive("ata_devstat_hgst")
    _, added = _setup({drive["id"]: drive})
    by_key = {e.entity_description.key: e for e in added["sensor"] if hasattr(e, "entity_description")}
    written, read = by_key["data_written"], by_key["data_read"]
    assert written.unique_id == f"{ENTRY_ID}_{drive['id']}_data_written"
    assert written.translation_key == "data_written"
    assert written.device_class == "data_size" and written.state_class == "total"
    assert written.native_unit_of_measurement == "B"
    assert written.entity_description.suggested_unit_of_measurement == "TB"
    assert written.entity_description.suggested_display_precision == 2
    assert written.entity_registry_enabled_default is True
    assert written.entity_category is None
    assert written.last_reset is None
    assert written.native_value == HGST_WRITES
    assert written.extra_state_attributes == {"source": "ata_device_statistics"}
    assert read.entity_registry_enabled_default is False
    assert read.entity_category == "diagnostic"
    temp = by_key["temperature"]
    assert temp.extra_state_attributes == {
        "lifetime_max": 46, "lifetime_min": 17, "time_over_limit_minutes": 0,
    }


def test_the_listener_adds_new_entities_once():
    data = golden_payload()
    coord, added = _setup(data)
    before = len(added["sensor"]), len(added["binary_sensor"])
    data["_pools"].append({"name": "newpool", "state": "ONLINE"})
    upgraded = _devstat_drive("ata_devstat_wdc_unc")
    data[upgraded["id"]] = upgraded
    for listener in coord.listeners:
        listener()
    for listener in coord.listeners:
        listener()
    new_sensors = [e.unique_id for e in added["sensor"][before[0]:]]
    new_binary = [e.unique_id for e in added["binary_sensor"][before[1]:]]
    assert new_sensors == [
        f"{ENTRY_ID}_fx-ata-devstat-wdc-unc_data_written",
        f"{ENTRY_ID}_fx-ata-devstat-wdc-unc_data_read",
        f"{ENTRY_ID}_zpool_newpool_pool_state",
        f"{ENTRY_ID}_zpool_newpool_pool_read_errors",
        f"{ENTRY_ID}_zpool_newpool_pool_write_errors",
        f"{ENTRY_ID}_zpool_newpool_pool_checksum_errors",
        f"{ENTRY_ID}_zpool_newpool_pool_last_scrub",
    ]
    assert new_binary == [
        f"{ENTRY_ID}_zpool_newpool_pool_data_errors",
        f"{ENTRY_ID}_zpool_newpool_pool_problem",
    ]


def test_pool_devices_are_named_for_their_host():
    """Two hosts with an rpool must not collide (GH #50): the pool device
    carries the host the way the Disk Usage device does."""
    data = golden_payload()
    _, added = _setup(data)
    pool_entities = [e for e in added["sensor"] + added["binary_sensor"]
                     if e.unique_id.startswith(f"{ENTRY_ID}_zpool_")]
    assert pool_entities
    names = {e.device_info["name"] for e in pool_entities}
    assert all(name.endswith(" (fixturehost)") for name in names), names
    assert "ZFS pool oldpool (fixturehost)" in names


def test_a_failing_listener_does_not_raise(caplog):
    data = golden_payload()
    coord, _ = _setup(data)
    data["_filesystems"].append({"mountpoint": "/no-id"})  # KeyError on "id"
    for listener in coord.listeners:
        listener()
    assert "adding new entities failed" in caplog.text


# --- the coordinator: merge, D11, forget ------------------------------------------------


class _Store:
    def __init__(self):
        self.saves = []

    def async_delay_save(self, func, delay):
        self.saves.append((func(), delay))


def _coordinator(stored=None):
    coord = object.__new__(coordinator_mod.SmartSnifferCoordinator)
    coord.hass = _Hass()
    coord._prev_state, coord._prev_reasons, coord._prev_pool_reasons = {}, {}, {}
    coord._store = _Store()
    coord._devstat = devstat.normalize_store(stored)
    coord.created, coord.drive_classes = {}, {}
    coord.smart_presence = smart_presence.SmartPresence()

    class _E:
        entry_id = ENTRY_ID
        data = {}
    coord.config_entry = _E()
    return coord


def _poll(coord, drive, notes):
    result = {drive["id"]: drive}
    coord._merge_devstat(result)
    asyncio.run(coord._handle_attention_notifications(result))


def test_d11_announces_once_across_a_restart(monkeypatch):
    notes = []
    monkeypatch.setattr(coordinator_mod, "pn_create", lambda hass, **kw: notes.append(kw))
    monkeypatch.setattr(coordinator_mod, "pn_dismiss", lambda hass, nid: notes.append(("dismiss", nid)))
    coord = _coordinator()
    _poll(coord, agent_drive("ata_devstat_wdc_unc"), notes)
    assert len(notes) == 1
    assert notes[0]["notification_id"] == "smart_sniffer_attention_fx-ata-devstat-wdc-unc"
    assert notes[0]["message"] == (
        "**CRITICAL — Back up your data immediately.**\n\n"
        "• Reported Uncorrectable Errors: 18 (expected 0; from device statistics)\n\n"
        "First reading from this drive's Device Statistics."
    )
    assert notes[0]["title"] == "🔴 Drive Attention Required — WDC  WUH721414ALE604 (FIXTURE-m01-sdl)"
    stored, delay = coord._store.saves[-1]
    # An announcement record is written straight away, not on the 600 s
    # delayed save, so an unclean stop cannot lose it and re-announce.
    assert delay == 1
    assert stored["announced"] == ["FIXTURE-m01-sdl|unc"]
    assert stored["held"]["fx-ata-devstat-wdc-unc"]["unc"] == 18
    # Next poll, same reasons: nothing.
    _poll(coord, agent_drive("ata_devstat_wdc_unc"), notes)
    assert len(notes) == 1
    # Home Assistant restarts with the Store: baseline again, no announcement,
    # and a failed devstat read keeps YES from the hold.
    restarted = _coordinator(json.loads(json.dumps(stored)))
    _poll(restarted, agent_drive("ata_devstat_wdc_unc", status="unavailable", reason="stopped"), notes)
    assert len(notes) == 1
    assert restarted._prev_state["fx-ata-devstat-wdc-unc"] == "YES"


def test_a_transition_with_the_suffix_records(monkeypatch):
    notes = []
    monkeypatch.setattr(coordinator_mod, "pn_create", lambda hass, **kw: notes.append(kw))
    monkeypatch.setattr(coordinator_mod, "pn_dismiss", lambda hass, nid: None)
    coord = _coordinator()
    _poll(coord, agent_drive("ata_devstat_wdc_unc", status=None), notes)  # old agent: NO
    assert notes == []
    _poll(coord, agent_drive("ata_devstat_wdc_unc"), notes)  # agent upgraded: NO -> YES
    assert len(notes) == 1 and "First reading" not in notes[0]["message"]
    assert coord._devstat["announced"] == ["FIXTURE-m01-sdl|unc"]


def test_no_announcement_without_a_serial(monkeypatch):
    notes = []
    monkeypatch.setattr(coordinator_mod, "pn_create", lambda hass, **kw: notes.append(kw))
    coord = _coordinator()
    drive = agent_drive("ata_devstat_wdc_unc")
    drive["serial"] = ""
    _poll(coord, drive, notes)
    assert notes == [] and coord._devstat == {"announced": [], "held": {}}


def test_forget_device_drops_hold_records_and_created(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "pn_create", lambda hass, **kw: None)
    coord = _coordinator()
    _poll(coord, agent_drive("ata_devstat_wdc_unc"), [])
    coord.created = {"sensor": {}}
    from custom_components.smart_sniffer.entity_plan import EntitySpec
    coord.created["sensor"]["u"] = EntitySpec("sensor", "attention", "u", "fx-ata-devstat-wdc-unc")
    coord.forget_device("fx-ata-devstat-wdc-unc", "FIXTURE-m01-sdl")
    assert coord._devstat == {"announced": [], "held": {}}
    assert coord.created == {"sensor": {}}


# --- GH #61: SMART data that was there and is not now --------------------

def _smart_gone(drive):
    """The drive as the agent reports it once the macOS SAT driver stops
    passing SMART through: identity kept, SMART data gone."""
    gone = json.loads(json.dumps(drive))
    keep = ("json_format_version", "smartctl", "device", "model_name", "serial_number",
            "user_capacity", "logical_block_size")
    gone["smart_data"] = {k: v for k, v in gone["smart_data"].items() if k in keep}
    gone["smart_data"]["smart_support"] = {"available": True, "enabled": False}
    gone["smart_available"] = False
    gone["smart_unavailable_reason"] = "disabled"
    gone["smart_lost"] = True
    gone.pop("device_statistics", None)
    gone.pop("derived", None)
    return gone


def _notes(monkeypatch):
    notes = []
    monkeypatch.setattr(coordinator_mod, "pn_create", lambda hass, **kw: notes.append(kw))
    monkeypatch.setattr(coordinator_mod, "pn_dismiss", lambda hass, nid: notes.append(("dismiss", nid)))
    return notes


def test_lost_smart_raises_no_notification_and_recovers(monkeypatch, caplog):
    notes = _notes(monkeypatch)
    coord = _coordinator()
    healthy = agent_drive("ata_devstat_hgst")
    drive_id = healthy["id"]
    _poll(coord, healthy, notes)
    assert coord._prev_state[drive_id] == "NO"
    gone = _smart_gone(healthy)
    gone["smart_lost"] = False  # the integration's own memory is enough
    with caplog.at_level("INFO", logger=coordinator_mod.__name__):
        for _ in range(3):
            _poll(coord, gone, notes)
        assert notes == []
        assert coord.smart_presence.is_lost(drive_id)
        assert coord._prev_state[drive_id] == "NO"  # the baseline is kept
        _poll(coord, healthy, notes)
        _poll(coord, healthy, notes)
    assert notes == []
    assert not coord.smart_presence.is_lost(drive_id)
    lost_lines = [r for r in caplog.records if "stopped giving SMART data" in r.getMessage()]
    back_lines = [r for r in caplog.records if "giving SMART data again" in r.getMessage()]
    assert len(lost_lines) == 1 and len(back_lines) == 1
    assert "agent says: no reason given" not in lost_lines[0].getMessage()


def test_lost_smart_keeps_an_open_attention_notification_quiet(monkeypatch):
    notes = _notes(monkeypatch)
    coord = _coordinator()
    _poll(coord, agent_drive("ata_devstat_wdc_unc", status=None), notes)  # NO
    _poll(coord, agent_drive("ata_devstat_wdc_unc"), notes)  # YES: one notification
    assert len(notes) == 1
    yes = agent_drive("ata_devstat_wdc_unc")
    _poll(coord, _smart_gone(yes), notes)
    _poll(coord, yes, notes)  # back with the same reasons: not news
    assert len(notes) == 1


def test_lost_after_a_home_assistant_restart(monkeypatch):
    # Only the agent remembers the drive had SMART data.
    notes = _notes(monkeypatch)
    coord = _coordinator()
    healthy = agent_drive("ata_devstat_hgst")
    _poll(coord, _smart_gone(healthy), notes)
    assert notes == [] and coord.smart_presence.is_lost(healthy["id"])
    assert healthy["id"] not in coord._prev_state
    _poll(coord, healthy, notes)
    assert notes == [] and coord._prev_state[healthy["id"]] == "NO"


def test_never_had_smart_is_unchanged(monkeypatch):
    notes = _notes(monkeypatch)
    coord = _coordinator()
    never = _smart_gone(agent_drive("ata_devstat_hgst"))
    never["smart_lost"] = False
    never["smart_unavailable_reason"] = "no_data"
    for _ in range(3):
        _poll(coord, never, notes)
    assert notes == []
    assert coord._prev_state[never["id"]] == "UNSUPPORTED"
    assert not coord.smart_presence.is_lost(never["id"])
    coord_data = {never["id"]: never}
    stub, added = _setup(coord_data)
    stub.smart_presence.update(never["id"], never)
    by_uid = {e.unique_id: e for e in added["sensor"]}
    prefix = f"{ENTRY_ID}_{never['id']}"
    assert by_uid[f"{prefix}_attention"].native_value == "UNSUPPORTED"
    assert by_uid[f"{prefix}_attention_reasons"].native_value == "No usable SMART data"


def test_lost_smart_entities(monkeypatch):
    healthy = _devstat_drive("ata_devstat_hgst")
    data = {healthy["id"]: healthy}
    coord, added = _setup(data)
    coord.smart_presence.update(healthy["id"], healthy)
    by_uid = {e.unique_id: e for e in added["sensor"] + added["binary_sensor"]}
    prefix = f"{ENTRY_ID}_{healthy['id']}"
    attention_entity = by_uid[f"{prefix}_attention"]
    reasons_entity = by_uid[f"{prefix}_attention_reasons"]
    health_entity = by_uid[f"{prefix}_health"]
    assert attention_entity.native_value == "NO"
    assert health_entity.is_on is False

    gone = _smart_gone(healthy)
    data[healthy["id"]] = gone
    coord.smart_presence.update(healthy["id"], gone)
    assert attention_entity.native_value is None
    assert attention_entity.icon == "mdi:help-circle-outline"
    assert attention_entity.extra_state_attributes == {
        "severity": "none",
        "reasons": ["SMART data not available right now"],
        "issue_count": 0,
        "accepted": [],
    }
    assert reasons_entity.native_value == "SMART data not available right now"
    assert health_entity.is_on is None
    assert by_uid[f"{prefix}_temperature"].native_value is None

    data[healthy["id"]] = healthy
    coord.smart_presence.update(healthy["id"], healthy)
    assert attention_entity.native_value == "NO"
    assert reasons_entity.native_value == "No issues detected"
    assert health_entity.is_on is False


class _TimeoutSession:
    """aiohttp session stand-in: /api/drives answers, a drive times out."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)

        class _Resp:
            async def __aenter__(self):
                if url.endswith("/api/drives"):
                    return self
                raise TimeoutError

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def json(self):
                return [{"id": "fake-serial-01"}]

        return _Resp()


def test_a_poll_timeout_names_the_host_and_request(monkeypatch, caplog):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    session = _TimeoutSession()
    monkeypatch.setattr(coordinator_mod, "async_get_clientsession", lambda hass: session)
    coord = _coordinator()
    coord.host, coord.port, coord.token = "10.0.0.5", 9099, "secret-token"
    coord._timeouts = coordinator_mod.PollTimeouts(
        coordinator_mod._LOGGER, coord.host, coord.port, coordinator_mod.AGENT_POLL_TIMEOUT
    )
    for _ in range(3):
        with pytest.raises(UpdateFailed) as err:
            asyncio.run(coord._async_update_data())
        assert str(err.value) == (
            "SMART Sniffer agent at 10.0.0.5:9099 did not answer "
            "/api/drives/fake-serial-01 within 30 seconds"
        )
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "did not answer" in r.getMessage()]
    assert len(warnings) == 1
    assert "secret-token" not in caplog.text


class _HealthSession:
    """Session stand-in for the health coordinator: one body, or a timeout."""

    def __init__(self, body):
        self.body = body

    def get(self, url, headers=None, timeout=None):
        body = self.body

        class _Resp:
            async def __aenter__(self):
                if body is None:
                    raise TimeoutError
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def json(self):
                return body

        return _Resp()


def test_health_keeps_the_agent_host_and_os_name(monkeypatch):
    session = _HealthSession({
        "status": "ok", "version": "0.8.3", "os": "darwin", "uptime_seconds": 5,
        "hostname": "fake-mac", "os_name": "macOS 15.1", "os_version": "15.1",
    })
    monkeypatch.setattr(coordinator_mod, "async_get_clientsession", lambda hass: session)
    health = object.__new__(coordinator_mod.AgentHealthCoordinator)
    health.hass, health.host, health.port, health.token = _Hass(), "10.0.0.5", 9099, ""
    health._last_version, health._last_seen, health._last_os, health._last_uptime = "", None, "", None
    health._last_hostname = health._last_os_name = health._last_os_version = ""

    data = asyncio.run(health._async_update_data())
    assert data["connected"] and data["os"] == "darwin"
    assert (data["hostname"], data["os_name"], data["os_version"]) == ("fake-mac", "macOS 15.1", "15.1")

    session.body = None  # agent down: last known values stay
    data = asyncio.run(health._async_update_data())
    assert not data["connected"] and data["hostname"] == "fake-mac" and data["os_name"] == "macOS 15.1"

    session.body = {"status": "ok", "version": "0.8.2", "os": "linux"}  # older agent
    data = asyncio.run(health._async_update_data())
    assert (data["hostname"], data["os_name"], data["os_version"]) == ("", "", "")


def _wd_red_sa500(wear_value):
    from tests.golden_payloads import FIXTURES, drive_payload

    with (FIXTURES / "v083" / "ata_wd_red_sa500.json").open(encoding="utf-8") as handle:
        smart_data = json.load(handle)
    for attr in smart_data["ata_smart_attributes"]["table"]:
        if attr["id"] == 230:
            attr["value"] = wear_value
    return drive_payload("wd_red_sa500", smart_data)


def _attention(added):
    (entity,) = [e for e in added["sensor"] if type(e).__name__ == "SmartSnifferAttentionSensor"]
    return entity.native_value


def test_wd_wear_level_and_its_note():
    """GH #63: 230 read as percent used; a cross-check disagreement leaves the
    Wear Level unknown and says why in a wear_note attribute."""
    drive = _wd_red_sa500(1)
    _, added = _setup({drive["id"]: drive})
    by_key = {e.entity_description.key: e for e in added["sensor"] if hasattr(e, "entity_description")}
    wear = by_key["wear_leveling_count"]
    assert wear.native_value == 1
    assert "wear_note" not in wear.extra_state_attributes
    assert _attention(added) == "NO"

    drive = _wd_red_sa500(99)
    _, added = _setup({drive["id"]: drive})
    by_key = {e.entity_description.key: e for e in added["sensor"] if hasattr(e, "entity_description")}
    wear = by_key["wear_leveling_count"]
    assert wear.native_value is None
    assert wear.extra_state_attributes["wear_note"].startswith("wear unknown: attribute 230")
    assert _attention(added) == "NO"
