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
    ]


def test_data_errors_on_an_online_pool():
    pool = one("data-errors")
    assert ph.state_option(pool) == "ONLINE"
    assert ph.data_errors(pool) == 2
    assert ph.pool_problems(pool) == ["Checksum errors: 16", "Data errors: 2"]
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
    assert ph.pool_problems(pool) == ["State: DEGRADED", "Write errors: 37"]


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
    ]


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
    assert "await fetch_pools(health, _get_json) if health is not None else None" in source
    assert "result[POOLS_KEY] = (" in source
    assert "self._handle_pool_notifications(result.get(POOLS_KEY))" in source


# --- Notifications -------------------------------------------------------------


def _poll(previous, pool_list):
    return ph.notification_actions(previous, pool_list)


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
    assert actions == [("create", "tank", ph.pool_problems(one("degraded-faulted")))]
    for _ in range(3):
        actions, state = _poll(state, pools("degraded-faulted"))
        assert actions == []


def test_new_reasons_update_the_same_notification():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "read_errors": 1}])
    actions, _ = _poll(state, [{**_HEALTHY, "read_errors": 1, "state": "DEGRADED"}])
    assert actions == [("create", "tank", ["State: DEGRADED", "Read errors: 1"])]


def test_a_rising_counter_is_a_new_reason():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "checksum_errors": 1}])
    actions, _ = _poll(state, [{**_HEALTHY, "checksum_errors": 5}])
    assert actions == [("create", "tank", ["Checksum errors: 5"])]


def test_recovery_dismisses():
    _, state = _poll({}, [_HEALTHY])
    _, state = _poll(state, [{**_HEALTHY, "data_errors": 2}])
    actions, state = _poll(state, [_HEALTHY])
    assert actions == [("dismiss", "tank", ["Data errors: 2"])]
    assert state == {"tank": []}


def test_a_pool_that_disappears_is_forgotten():
    _, state = _poll({}, [_HEALTHY, {**_HEALTHY, "name": "rpool"}])
    _, state = _poll(state, [_HEALTHY, {**_HEALTHY, "name": "rpool", "state": "DEGRADED"}])
    actions, state = _poll(state, [_HEALTHY])
    assert actions == [("dismiss", "rpool", ["State: DEGRADED"])]
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
