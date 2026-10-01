"""ZFS pool health (GH #50): what the agent's /api/pools payload means.

The fixtures in fixtures/pools are the agent's own /api/pools output for the
zpool fixtures in agent/testdata/zpool, written by the Go parser, so these
tests read exactly what Home Assistant will receive. gh50-three-pools is the
reporter's three pools; the rest are constructed (see the provenance comment in
agent/zpool_status_test.go).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"
_POOL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pools"


def _load(filename: str):
    """Import one module of the integration by path, under a stand-in package so
    its relative imports resolve, since the real package __init__ imports Home
    Assistant."""
    package = "smart_sniffer_standalone"
    if package not in sys.modules:
        parent = ModuleType(package)
        parent.__path__ = [str(_COMPONENT)]
        sys.modules[package] = parent
    name = f"{package}.{Path(filename).stem}"
    spec = importlib.util.spec_from_file_location(name, _COMPONENT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ph = _load("pool_health.py")
ENTRY = "01KRS6Q8KDEN7SQJFHWCZAQ6EM"


def pools(stem: str) -> list[dict]:
    return json.loads((_POOL_FIXTURES / f"{stem}.json").read_text())


def one(stem: str) -> dict:
    (pool,) = pools(stem)
    return pool


# --- Entity values from the fixtures -------------------------------------------


def test_the_reporters_three_pools():
    found = {p["name"]: p for p in pools("gh50-three-pools")}
    assert sorted(found) == ["raid10", "rpool", "storage"]
    ends = {
        "raid10": datetime(2026, 9, 1, 3, 5, 34, tzinfo=timezone.utc),
        "rpool": datetime(2026, 9, 1, 1, 0, 33, tzinfo=timezone.utc),
        "storage": datetime(2026, 9, 1, 9, 39, 32, tzinfo=timezone.utc),
    }
    for name, pool in found.items():
        assert ph.state_option(pool) == "ONLINE"
        assert [ph.error_total(pool, k) for k in ph.ERROR_KEYS] == [0, 0, 0]
        assert ph.data_errors(pool) == 0
        assert ph.last_scrub_end(pool) == ends[name]
        assert ph.scrub_attributes(pool) == {
            "scrub_errors": 0,
            "repaired_bytes": 0,
            "scrub_in_progress": False,
            "scan_function": "SCRUB",
            "scan_state": "FINISHED",
        }


def test_features_not_enabled_is_not_a_problem():
    """The status line on all three of the reporter's pools is advice. It is
    passed through as text and must never raise the problem sensor."""
    for pool in pools("gh50-three-pools"):
        assert pool["status"].startswith("Some supported and requested features are not enabled")
        assert ph.pool_problems(pool) == []
        assert ph.is_unhealthy(pool) is False


def test_degraded_pool_with_a_faulted_disk():
    pool = one("degraded-faulted")
    assert ph.state_option(pool) == "DEGRADED"
    assert [ph.error_total(pool, k) for k in ph.ERROR_KEYS] == [18, 3, 2]
    assert ph.data_errors(pool) == 0
    assert ph.scrub_attributes(pool)["repaired_bytes"] == 1572864
    assert ph.pool_problems(pool) == [
        "State: DEGRADED",
        "Read errors: 18",
        "Write errors: 3",
        "Checksum errors: 2",
        "raidz2-0: DEGRADED",
        "ata-WDC_WD80EFAX-68KNBN0_VAGX0003: FAULTED, 18 read errors, 3 write errors",
        "ata-WDC_WD80EFAX-68KNBN0_VAGX0005: 2 checksum errors",
    ]
    assert [d["name"] for d in ph.problem_devices(pool)] == [
        "raidz2-0", "ata-WDC_WD80EFAX-68KNBN0_VAGX0003", "ata-WDC_WD80EFAX-68KNBN0_VAGX0005",
    ]


def test_data_errors_on_an_online_pool():
    pool = one("data-errors")
    assert ph.state_option(pool) == "ONLINE"
    assert ph.data_errors(pool) == 2
    assert ph.pool_problems(pool) == [
        "Checksum errors: 16",
        "Data errors: 2",
        "mirror-0: 4 checksum errors",
        "ata-ST2000DM008-2FR102_ZFL0001: 4 checksum errors",
        "ata-ST2000DM008-2FR102_ZFL0002: 4 checksum errors",
    ]
    assert ph.scrub_attributes(pool)["scrub_errors"] == 2


def test_scrub_in_progress_is_healthy_and_keeps_no_end_time():
    pool = one("scrub-in-progress")
    assert ph.pool_problems(pool) == []
    assert ph.last_scrub_end(pool) is None
    attrs = ph.scrub_attributes(pool)
    assert attrs["scrub_in_progress"] is True
    assert attrs["scan_function"] == "SCRUB" and attrs["scan_state"] == "SCANNING"


def test_never_scrubbed():
    pool = one("never-scrubbed")
    assert ph.pool_problems(pool) == []
    assert ph.last_scrub_end(pool) is None
    assert ph.scrub_attributes(pool) == {
        "scrub_errors": None,
        "repaired_bytes": None,
        "scrub_in_progress": False,
        "scan_function": None,
        "scan_state": None,
    }


def test_a_resilver_is_not_a_scrub():
    pool = one("resilver")
    assert ph.last_scrub_end(pool) is None
    attrs = ph.scrub_attributes(pool)
    assert attrs["scan_function"] == "RESILVER" and attrs["scrub_in_progress"] is False
    assert ph.pool_problems(pool) == [
        "State: DEGRADED",
        "Write errors: 37",
        "mirror-0: DEGRADED",
        "replacing-1: DEGRADED",
        "ata-ST4000VN008-2DR166_ZDH0002: FAULTED, 37 write errors",
    ]


# --- The rule, field by field --------------------------------------------------

_HEALTHY = {
    "name": "tank", "state": "ONLINE", "read_errors": 0, "write_errors": 0,
    "checksum_errors": 0, "data_errors": 0, "status": None, "action": None,
}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"state": "FAULTED"}, "State: FAULTED"),
        ({"state": "SUSPENDED"}, "State: SUSPENDED"),
        ({"read_errors": 1}, "Read errors: 1"),
        ({"write_errors": 4}, "Write errors: 4"),
        ({"checksum_errors": 9}, "Checksum errors: 9"),
        ({"data_errors": 3}, "Data errors: 3"),
    ],
)
def test_each_condition_alone_makes_a_pool_unhealthy(change, reason):
    assert ph.pool_problems({**_HEALTHY, **change}) == [reason]


def test_status_text_never_counts():
    pool = {**_HEALTHY, "status": "One or more devices has experienced an unrecoverable error.",
            "action": "Determine if the device needs to be replaced."}
    assert ph.pool_problems(pool) == []


def test_missing_errors_line_is_unknown_not_a_problem():
    pool = {**_HEALTHY, "data_errors": None}
    assert ph.data_errors(pool) is None
    assert ph.pool_problems(pool) == []


def test_a_state_outside_the_options_shows_as_unknown():
    pool = {**_HEALTHY, "state": "SOMETHING_NEW"}
    assert ph.state_option(pool) == "UNKNOWN"
    assert ph.pool_problems(pool) == ["State: SOMETHING_NEW"]


def test_the_state_options_are_what_libzfs_can_print():
    """zpool_get_state_str() and zpool_state_to_name(), libzfs_pool.c."""
    assert ph.POOL_STATES == [
        "ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL",
        "REMOVED", "SUSPENDED", "SPLIT", "UNKNOWN",
        # The integration's own, for a pool the agent no longer lists.
        "MISSING",
    ]


def test_an_agent_cannot_send_missing():
    """MISSING is decided here, never taken from the payload."""
    assert ph.state_option({**_HEALTHY, "state": "MISSING"}) == "UNKNOWN"


def test_bad_values_read_as_unknown():
    assert ph.error_total({"read_errors": True}, "read_errors") is None
    assert ph.error_total({"read_errors": "3"}, "read_errors") is None
    assert ph.last_scrub_end({"last_scrub_end": "not a date"}) is None
    assert ph.last_scrub_end({"last_scrub_end": "2026-09-01T03:05:34"}) is None  # naive


# --- Fetching ------------------------------------------------------------------


def _health(with_pools: bool) -> dict:
    endpoints = ["/api/health", "/api/drives", "/api/drives/{id}"]
    if with_pools:
        endpoints.append("/api/pools")
    return {"version": "0.6.6", "endpoints": endpoints}


def _fetch(health, payload):
    calls: list[str] = []

    async def get_json(path):
        calls.append(path)
        return payload

    return asyncio.run(ph.fetch_pools(health, get_json)), calls


def test_an_agent_that_does_not_advertise_pools_is_never_asked():
    result, calls = _fetch(_health(False), pools("gh50-three-pools"))
    assert result == [] and calls == []
    # Agents older than /api/health's endpoints list, too.
    result, calls = _fetch({"version": "0.4.28"}, pools("gh50-three-pools"))
    assert result == [] and calls == []


def test_an_advertising_agent_is_asked_for_its_pools():
    result, calls = _fetch(_health(True), pools("gh50-three-pools"))
    assert calls == ["/api/pools"]
    assert [p["name"] for p in result] == ["raid10", "rpool", "storage"]


def test_a_failed_fetch_is_unknown_not_empty():
    assert _fetch(_health(True), None)[0] is None
    assert _fetch(_health(True), {"error": "unauthorized"})[0] is None


def test_entries_without_a_name_are_dropped():
    result, _ = _fetch(_health(True), [{"state": "ONLINE"}, "junk", {"name": "tank"}])
    assert result == [{"name": "tank"}]


def test_the_coordinator_fetches_only_through_fetch_pools():
    source = (_COMPONENT / "coordinator.py").read_text()
    assert "pools = await fetch_pools(health, _get_json) if health is not None else None" in source
    assert "result[POOLS_KEY] = pools" in source
    assert "result[POOLS_MISSING_KEY] = missing_pools(" in source
    assert "advertises_pools(health), pools, self.registered_pool_names()" in source
    assert "result.get(POOLS_KEY), result.get(POOLS_MISSING_KEY)" in source


# --- Notifications -------------------------------------------------------------


def _poll(previous, pool_list, missing=()):
    return ph.notification_actions(previous, pool_list, missing)


def _kinds(actions):
    return [(a.kind, a.name, a.reasons) for a in actions]


def test_the_first_poll_is_a_baseline():
    """Like drives: a pool already unhealthy when Home Assistant starts raises
    nothing; its problem sensor already says so."""
    actions, state = _poll({}, pools("degraded-faulted"))
    assert actions == []
    assert state == {"tank": ph.pool_problems(one("degraded-faulted"))}


def test_healthy_to_unhealthy_notifies_once():
    healthy = [{**one("degraded-faulted"), "state": "ONLINE", "read_errors": 0,
                "write_errors": 0, "checksum_errors": 0}]
    _, state = _poll({}, healthy)
    actions, state = _poll(state, pools("degraded-faulted"))
    assert [(a.kind, a.name, a.reasons) for a in actions] == [
        ("create", "tank", ph.pool_problems(one("degraded-faulted")))
    ]
    for _ in range(3):
        actions, state = _poll(state, pools("degraded-faulted"))
        assert actions == []


def test_new_reasons_update_the_same_notification():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "read_errors": 1}])
    actions, _ = _poll(state, [{**_HEALTHY, "read_errors": 1, "state": "DEGRADED"}])
    assert _kinds(actions) == [("create", "tank", ["State: DEGRADED", "Read errors: 1"])]


def test_a_rising_counter_is_a_new_reason():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "checksum_errors": 1}])
    actions, _ = _poll(state, [{**_HEALTHY, "checksum_errors": 5}])
    assert _kinds(actions) == [("create", "tank", ["Checksum errors: 5"])]


def test_recovery_dismisses():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "data_errors": 2}])
    actions, state = _poll(state, [_HEALTHY])
    assert _kinds(actions) == [("dismiss", "tank", ["Data errors: 2"])]
    assert state == {"tank": []}


def test_a_pool_that_disappears_is_forgotten():
    _, state = _poll({}, [_HEALTHY, {**_HEALTHY, "name": "rpool"}])
    _, state = _poll(state, [_HEALTHY, {**_HEALTHY, "name": "rpool", "state": "DEGRADED"}])
    actions, state = _poll(state, [_HEALTHY])
    assert _kinds(actions) == [("dismiss", "rpool", ["State: DEGRADED"])]
    assert state == {"tank": []}
    # A healthy pool vanishing has no notification to dismiss.
    actions, state = _poll(state, [])
    assert actions == [] and state == {}


def test_a_failed_fetch_changes_nothing():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "state": "DEGRADED"}])
    actions, kept = _poll(state, None)
    assert actions == [] and kept == state
    # And the next good poll does not re-raise what was already raised.
    actions, _ = _poll(kept, [{**_HEALTHY, "state": "DEGRADED"}])
    assert actions == []


def test_notification_wording():
    title, message = ph.build_notification("rpool", "pve-nas", ["State: DEGRADED", "Read errors: 18"])
    assert title == "ZFS pool rpool on pve-nas needs attention"
    assert message == (
        "- State: DEGRADED\n- Read errors: 18\n\n"
        "Run `zpool status -v rpool` on pve-nas for details."
    )
    assert (title + message).isascii()


def test_ids_are_per_entry_and_pool():
    assert ph.notification_id(ENTRY, "rpool") == f"smart_sniffer_pool_{ENTRY}_rpool"
    assert ph.pool_identifier(ENTRY, "rpool") == f"{ENTRY}_zpool_rpool"
    assert ph.pool_name_from_identifier(f"{ENTRY}_zpool_rpool", ENTRY) == "rpool"
    assert ph.pool_name_from_identifier(f"{ENTRY}_zpool_", ENTRY) is None
    assert ph.pool_name_from_identifier(f"{ENTRY}_filesystems", ENTRY) is None
    assert ph.pool_name_from_identifier("OTHERENTRY_zpool_rpool", ENTRY) is None


def test_reading_the_coordinator_payload():
    data = {"s1": {}, "_pools": pools("gh50-three-pools")}
    assert ph.find_pool(data, "rpool")["name"] == "rpool"
    assert ph.find_pool(data, "gone") is None
    assert ph.reported_pools(None) is None
    assert ph.reported_pools({"s1": {}}) == []
    assert ph.reported_pools({"_pools": None}) is None


# --- Problem devices (round 2) -------------------------------------------------


def test_spares_fixture_names_the_problem_devices():
    found = {p["name"]: p for p in pools("spares")}
    assert ph.pool_problems(found["vault"]) == [
        "State: DEGRADED",
        "Read errors: 12",
        "Checksum errors: 5",
        "raidz1-0: DEGRADED",
        "sdb: 5 checksum errors",
        "spare-2: DEGRADED",
        "sdc: FAULTED, 12 read errors",
    ]
    # The spare standing in for sdc (INUSE) and the available one are not listed.
    names = [d["name"] for d in ph.problem_devices(found["vault"])]
    assert "sde" not in names and "sdf" not in names


def test_an_unavailable_spare_alone_makes_the_pool_a_problem():
    """fast is ONLINE with no errors; only its spare cannot be opened."""
    fast = {p["name"]: p for p in pools("spares")}["fast"]
    assert ph.pool_level_problems(fast) == []
    assert ph.pool_problems(fast) == ["sdh: UNAVAIL"]
    assert ph.is_unhealthy(fast)


def test_healthy_pools_list_no_devices():
    for stem in ("gh50-three-pools", "scrub-in-progress", "never-scrubbed"):
        for pool in pools(stem):
            assert pool["problem_vdevs"] == []
            assert ph.problem_devices(pool) == [] and ph.device_problems(pool) == []


def test_an_agent_without_problem_vdevs_names_no_devices():
    """Round-1 agents send no problem_vdevs; the pool-level rule still works."""
    pool = {**_HEALTHY, "state": "DEGRADED"}
    assert ph.problem_devices(pool) == []
    assert ph.pool_problems(pool) == ["State: DEGRADED"]
    assert ph.problem_devices({**_HEALTHY, "problem_vdevs": "junk"}) == []
    assert ph.problem_devices({**_HEALTHY, "problem_vdevs": [{"state": "FAULTED"}, 3]}) == []


@pytest.mark.parametrize(
    ("vdev", "line"),
    [
        ({"name": "sdb", "state": "FAULTED"}, "sdb: FAULTED"),
        ({"name": "sdb", "state": "ONLINE", "read_errors": 18, "checksum_errors": 2},
         "sdb: 18 read errors, 2 checksum errors"),
        ({"name": "mirror-0", "state": "DEGRADED"}, "mirror-0: DEGRADED"),
        ({"name": "sdb", "state": "FAULTED", "read_errors": 18}, "sdb: FAULTED, 18 read errors"),
        ({"name": "sdb", "state": "ONLINE", "write_errors": 1}, "sdb: 1 write error"),
        ({"name": "sdb", "state": "ONLINE"}, None),
    ],
)
def test_device_lines(vdev, line):
    assert ph.device_line({"read_errors": 0, "write_errors": 0, "checksum_errors": 0, **vdev}) == line


def _disks(n):
    return [{"name": f"sd{chr(97 + i)}", "type": "disk", "state": "FAULTED",
             "read_errors": 0, "write_errors": 0, "checksum_errors": 0} for i in range(n)]


def test_notification_with_named_devices():
    pool = one("degraded-faulted")
    title, message = ph.build_notification(
        "tank", "pve-nas", ph.pool_level_problems(pool), ph.device_problems(pool)
    )
    assert title == "ZFS pool tank on pve-nas needs attention"
    assert message == (
        "- State: DEGRADED\n- Read errors: 18\n- Write errors: 3\n- Checksum errors: 2\n"
        "- raidz2-0: DEGRADED\n"
        "- ata-WDC_WD80EFAX-68KNBN0_VAGX0003: FAULTED, 18 read errors, 3 write errors\n"
        "- ata-WDC_WD80EFAX-68KNBN0_VAGX0005: 2 checksum errors\n\n"
        "Run `zpool status -v tank` on pve-nas for details."
    )


def test_the_notification_shows_ten_devices_then_a_count():
    pool = {**_HEALTHY, "state": "DEGRADED", "problem_vdevs": _disks(13)}
    _, message = ph.build_notification(
        "tank", "nas", ph.pool_level_problems(pool), ph.device_problems(pool)
    )
    lines = message.split("\n\n")[0].split("\n")
    assert lines[0] == "- State: DEGRADED"
    assert lines[1:11] == [f"- sd{chr(97 + i)}: FAULTED" for i in range(10)]
    assert lines[11:] == ["- and 3 more"]
    # The attribute side keeps every device.
    assert len(ph.problem_devices(pool)) == 13 and len(ph.pool_problems(pool)) == 14
    # Exactly ten: no "more" line.
    pool = {**pool, "problem_vdevs": _disks(10)}
    _, message = ph.build_notification(
        "tank", "nas", ph.pool_level_problems(pool), ph.device_problems(pool)
    )
    assert "more" not in message


def test_a_change_in_the_devices_updates_the_notification():
    degraded = {**_HEALTHY, "state": "DEGRADED", "problem_vdevs": _disks(1)}
    _, state = _poll({}, [_HEALTHY])
    actions, state = _poll(state, [degraded])
    assert _kinds(actions) == [("create", "tank", ["State: DEGRADED", "sda: FAULTED"])]
    assert actions[0].pool_lines == ["State: DEGRADED"] and actions[0].device_lines == ["sda: FAULTED"]
    actions, state = _poll(state, [{**degraded, "problem_vdevs": _disks(2)}])
    assert _kinds(actions) == [("create", "tank", ["State: DEGRADED", "sda: FAULTED", "sdb: FAULTED"])]
    actions, _ = _poll(state, [{**degraded, "problem_vdevs": _disks(2)}])
    assert actions == []


# --- Missing pools (round 2) ---------------------------------------------------


def test_missing_needs_a_successful_fetch_from_an_advertising_agent():
    listed = pools("gh50-three-pools")
    registered = ["raid10", "rpool", "storage", "tank"]
    assert ph.missing_pools(True, listed, registered) == ["tank"]
    assert ph.missing_pools(True, [], registered) == registered
    assert ph.missing_pools(True, listed, ["rpool"]) == []
    # The pool fetch failed, or the health check did: unknown, not missing.
    assert ph.missing_pools(True, None, registered) is None
    assert ph.missing_pools(False, None, registered) is None
    # Pool status off, or an older agent: it says nothing about imports.
    assert ph.missing_pools(False, [], registered) == []


def test_missing_after_a_successful_fetch_notifies():
    _, state = _poll({}, [_HEALTHY])
    actions, state = _poll(state, [], ["tank"])
    assert _kinds(actions) == [("missing", "tank", ["Not reported by zpool"])]
    assert state == {"tank": [ph.MISSING_REASON]}
    # Once.
    actions, state = _poll(state, [], ["tank"])
    assert actions == []


def test_missing_at_startup_notifies():
    """The failed-import-at-boot case: no baseline silence for a missing pool.
    A pool that is listed but unhealthy at startup still raises nothing."""
    actions, state = _poll({}, pools("degraded-faulted"), ["rpool"])
    assert _kinds(actions) == [("missing", "rpool", ["Not reported by zpool"])]
    assert set(state) == {"tank", "rpool"}


def test_a_failed_fetch_never_makes_a_pool_missing():
    _, state = _poll({}, [_HEALTHY])
    actions, kept = _poll(state, None, None)
    assert actions == [] and kept == state
    # And a missing pool stays as it was through a failed fetch.
    _, state = _poll(state, [], ["tank"])
    actions, kept = _poll(state, None, None)
    assert actions == [] and kept == state


def test_missing_then_back_healthy_dismisses():
    _, state = _poll({}, [], ["tank"])
    actions, state = _poll(state, [_HEALTHY], [])
    assert _kinds(actions) == [("dismiss", "tank", ["Not reported by zpool"])]
    assert state == {"tank": []}


def test_missing_then_back_degraded_updates():
    _, state = _poll({}, [], ["tank"])
    actions, _ = _poll(state, [{**_HEALTHY, "state": "DEGRADED"}], [])
    assert _kinds(actions) == [("create", "tank", ["State: DEGRADED"])]


def test_degraded_then_missing_updates_the_same_notification():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "state": "DEGRADED"}])
    actions, _ = _poll(state, [], ["tank"])
    assert _kinds(actions) == [("missing", "tank", ["Not reported by zpool"])]


def test_a_removed_missing_pool_is_dismissed_and_forgotten():
    """Removing the device takes the pool out of the registry, so it is
    neither listed nor missing: dismissed once, then never mentioned."""
    _, state = _poll({}, [], ["tank"])
    actions, state = _poll(state, [], [])
    assert _kinds(actions) == [("dismiss", "tank", ["Not reported by zpool"])]
    assert state == {}
    actions, _ = _poll(state, [], [])
    assert actions == []


def test_missing_notification_wording():
    title, message = ph.build_missing_notification("storage", "pve-nas")
    assert title == "ZFS pool storage on pve-nas is missing"
    assert message == (
        "The agent on pve-nas no longer reports this pool. It may have been exported, "
        "or failed to import.\n\n"
        'Run "zpool import" on pve-nas to see pools that can be imported.'
    )
    assert (title + message).isascii()


def test_registered_pools_come_from_this_entrys_pool_devices():
    devices = [
        {("smart_sniffer", f"{ENTRY}_agent")},
        {("smart_sniffer", f"{ENTRY}_zpool_rpool")},
        {("smart_sniffer", f"{ENTRY}_zpool_storage"), ("other", "x")},
        {("smart_sniffer", "OTHER_zpool_tank")},
        {("other_domain", f"{ENTRY}_zpool_fake")},
        {("smart_sniffer", f"{ENTRY}_filesystems")},
        {("smart_sniffer", "WD-ABC123")},
    ]
    assert ph.registered_pool_names(devices, ENTRY) == ["rpool", "storage"]


def test_setup_creates_entities_for_registered_pools_absent_from_the_payload():
    data = {"_pools": pools("gh50-three-pools"), "_pools_missing": ["tank"]}
    assert ph.pool_names_for_setup(data, ["tank", "rpool"]) == ["raid10", "rpool", "storage", "tank"]
    # A failed first pool fetch still creates the registered pools' entities.
    assert ph.pool_names_for_setup({"_pools": None}, ["tank"]) == ["tank"]


def test_reading_missing_from_the_payload():
    assert ph.reported_missing({"_pools_missing": ["tank"]}) == ["tank"]
    assert ph.is_missing({"_pools_missing": ["tank"]}, "tank")
    assert not ph.is_missing({"_pools_missing": None}, "tank")
    assert not ph.is_missing(None, "tank")
    assert ph.reported_missing({}) == []


def test_removing_a_pool_device_forgets_it():
    """async_remove_config_entry_device tells the coordinator, which dismisses
    the notification and drops the pool's state."""
    init = (_COMPONENT / "__init__.py").read_text()
    start = init.index("if decision.allowed:")
    allowed = init[start:init.index("return True", start)]
    assert "coordinator.forget_pool(pool)" in allowed
    coord = (_COMPONENT / "coordinator.py").read_text()
    forget = coord[coord.index("def forget_pool"):coord.index("def _handle_pool_notifications")]
    assert "pn_dismiss(self.hass, pool_notification_id(" in forget
    assert "self._prev_pool_reasons.pop(name, None)" in forget


# --- Translations --------------------------------------------------------------


def _entity_keys_in_source() -> dict[str, set[str]]:
    keys = {}
    for platform in ("sensor", "binary_sensor"):
        source = (_COMPONENT / f"{platform}.py").read_text()
        found = set(re.findall(r'super\(\).__init__\(coordinator, pool_name, "(pool_\w+)"\)', source))
        if 'f"pool_{key}"' in source:
            found |= {f"pool_{key}" for key in ph.ERROR_KEYS}
        keys[platform] = found
    return keys


def test_every_pool_entity_has_a_translated_name():
    strings = json.loads((_COMPONENT / "strings.json").read_text())
    for platform, keys in _entity_keys_in_source().items():
        assert keys, platform
        names = strings["entity"][platform]
        assert keys == set(names), platform
        assert all(names[k].get("name") for k in keys), platform


def test_pool_entities_set_no_hard_coded_name():
    """A hard-coded _attr_name would override the translation."""
    for filename in ("pool_entity.py", "sensor.py", "binary_sensor.py"):
        source = (_COMPONENT / filename).read_text()
        for block in re.findall(r"class ZfsPool\w+\(.*?(?=\nclass |\Z)", source, re.S):
            assert "_attr_name" not in block, filename
