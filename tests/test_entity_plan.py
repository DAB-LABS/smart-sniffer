"""The entity plan (D7, plan v3 6.5): v0.7.0's setup, then new entities
without a reload.

The golden is v0.7.0's own setup (36ebe65, sensor.py and binary_sensor.py
under Home Assistant 2024.4.0) on the payload in golden_payloads.py: every
entity's unique id, class, value and attributes, in creation order, with
force_update off and on. test_entities_under_ha.py rebuilds the entities
themselves when Home Assistant is installed.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
from pathlib import Path

import pytest

from custom_components.smart_sniffer import entity_plan as ep
from custom_components.smart_sniffer.devstat import DEVSTAT_KEY, merge_devstat, page_attributes
from custom_components.smart_sniffer.extract import (
    SENSOR_KEYS,
    _decode_raw_value,
    _extract_attribute,
)
from tests.devstat_payloads import agent_drive
from tests.golden_payloads import ENTRY_ID, GOLDEN, REGISTERED_POOLS, golden_payload

_SENSOR_PY = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer" / "sensor.py"

# v0.7.0 entity class -> plan kind.
CLASS_KIND = {
    "SmartSnifferSensor": ep.KIND_ATTRIBUTE,
    "SmartSnifferDiagnosticAttrSensor": ep.KIND_DIAGNOSTIC_ATTR,
    "SmartSnifferAttentionSensor": ep.KIND_ATTENTION,
    "SmartSnifferAttentionReasonsSensor": ep.KIND_ATTENTION_REASONS,
    "SmartSnifferFilesystemSensor": ep.KIND_FILESYSTEM,
    "ZfsPoolStateSensor": ep.KIND_POOL_STATE,
    "ZfsPoolErrorSensor": ep.KIND_POOL_ERROR,
    "ZfsPoolLastScrubSensor": ep.KIND_POOL_LAST_SCRUB,
    "AgentVersionSensor": ep.KIND_AGENT_VERSION,
    "AgentLastSeenSensor": ep.KIND_AGENT_LAST_SEEN,
    "AgentIPSensor": ep.KIND_AGENT_IP,
    "AgentPortSensor": ep.KIND_AGENT_PORT,
    "AgentOSSensor": ep.KIND_AGENT_OS,
    "AgentPollIntervalSensor": ep.KIND_AGENT_POLL_INTERVAL,
    "SmartSnifferHealthSensor": ep.KIND_HEALTH,
    "DriveStandbySensor": ep.KIND_STANDBY,
    "ZfsPoolDataErrorsSensor": ep.KIND_POOL_DATA_ERRORS,
    "ZfsPoolProblemSensor": ep.KIND_POOL_PROBLEM,
    "AgentStatusBinarySensor": ep.KIND_AGENT_STATUS,
    "AuthActiveBinarySensor": ep.KIND_AUTH_ACTIVE,
}


def _golden():
    with (GOLDEN / "v070-entities.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _plan(data, registered=REGISTERED_POOLS, pinned=None):
    pinned = {} if pinned is None else pinned
    return (
        ep.sensor_specs(data, ENTRY_ID, registered, pinned)
        + ep.binary_sensor_specs(data, ENTRY_ID, registered)
    )


# --- v0.7.0 parity ------------------------------------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_the_plan_is_v070s_setup(force):
    rows = _golden()["force_update_on" if force else "force_update_off"]
    data = golden_payload()
    for drive_id, drive in data.items():
        if not drive_id.startswith("_"):
            drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)  # older agent: empty
    specs = _plan(data)
    assert [s.unique_id for s in specs] == [r["unique_id"] for r in rows]
    assert [s.platform for s in specs] == [r["platform"] for r in rows]
    assert [s.kind for s in specs] == [CLASS_KIND[r["class"]] for r in rows]
    assert [ep.wants_force_update(s, force) for s in specs] == [r["force_update"] for r in rows]
    assert [s.device for s in specs] == [r["device_identifiers"][0][1] for r in rows]


def test_the_values_are_v070s():
    """The moved lookups give every value and attribute v0.7.0 showed."""
    data = golden_payload()
    checked = 0
    for row in _golden()["force_update_off"]:
        drive_id = row["device_identifiers"][0][1]
        drive = data.get(drive_id)
        if row["class"] == "SmartSnifferSensor":
            key = row["unique_id"].split(f"{drive_id}_", 1)[1]
            assert _extract_attribute(drive, key) == row["value"], row["unique_id"]
            standby = (
                {"in_standby": True, "data_as_of": drive["last_updated"]}
                if drive.get("in_standby") else {}
            )
            assert {**standby, **page_attributes(drive, key)} == row["attributes"]
            checked += 1
        elif row["class"] == "SmartSnifferDiagnosticAttrSensor":
            attr_id = int(row["unique_id"].rsplit("smart_attr_", 1)[1])
            table = drive["smart_data"]["ata_smart_attributes"]["table"]
            value = next(_decode_raw_value(a.get("raw", {})) for a in table if a.get("id") == attr_id)
            assert value == row["value"], row["unique_id"]
            checked += 1
    assert checked > 200


def test_sensor_keys_follow_the_descriptions():
    """extract.SENSOR_KEYS restates the order of sensor.SENSOR_DESCRIPTIONS."""
    tree = ast.parse(_SENSOR_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "SENSOR_DESCRIPTIONS":
            keys = [
                kw.value.value
                for call in node.value.elts
                for kw in call.keywords
                if kw.arg == "key"
            ]
            assert tuple(keys) == SENSOR_KEYS
            return
    raise AssertionError("SENSOR_DESCRIPTIONS not found")


# --- new entities without a reload -----------------------------------------------------


def _setup(data):
    created = {ep.SENSOR: {}, ep.BINARY_SENSOR: {}}
    pinned: dict = {}
    ep.take_new(ep.sensor_specs(data, ENTRY_ID, REGISTERED_POOLS, pinned), created[ep.SENSOR])
    ep.take_new(ep.binary_sensor_specs(data, ENTRY_ID, REGISTERED_POOLS), created[ep.BINARY_SENSOR])
    return created, pinned


def _poll(data, created, pinned):
    """What the listeners add after a poll."""
    return (
        ep.take_new(ep.sensor_specs(data, ENTRY_ID, (), pinned), created[ep.SENSOR])
        + ep.take_new(ep.binary_sensor_specs(data, ENTRY_ID, ()), created[ep.BINARY_SENSOR])
    )


def test_an_unchanged_payload_adds_nothing():
    data = golden_payload()
    created, pinned = _setup(data)
    assert _poll(data, created, pinned) == []


def test_pool_filesystem_drive_and_data_written_are_added_once():
    data = golden_payload()
    created, pinned = _setup(data)

    data["_pools"].append({"name": "newpool", "state": "ONLINE"})
    data["_filesystems"].append({"id": "fs-new", "mountpoint": "/new", "status": "ok"})
    new_drive = agent_drive("ata_devstat_hgst")
    new_drive["id"] = "fx-new-drive"
    new_drive[DEVSTAT_KEY], _ = merge_devstat(new_drive, None)
    data[new_drive["id"]] = new_drive
    upgraded = data["fx-ata-devstat-wdc-unc"]
    upgraded.update(agent_drive("ata_devstat_wdc_unc"))
    upgraded[DEVSTAT_KEY], _ = merge_devstat(upgraded, None)

    added = _poll(data, created, pinned)
    by_device = {}
    for spec in added:
        by_device.setdefault(spec.device, []).append(spec.kind)
    pool_device = f"{ENTRY_ID}_zpool_newpool"
    assert by_device[pool_device] == [
        "pool_state", "pool_error", "pool_error", "pool_error", "pool_last_scrub",
        "pool_data_errors", "pool_problem",
    ]
    assert by_device[f"{ENTRY_ID}_filesystems"] == ["filesystem"]
    assert by_device["fx-ata-devstat-wdc-unc"] == [
        "data_written", "data_read",
    ]
    assert "data_written" in by_device["fx-new-drive"]
    assert "health" in by_device["fx-new-drive"]
    assert _poll(data, created, pinned) == []  # once


def test_data_written_unique_id_and_absence_without_a_value():
    drive = agent_drive("ata_devstat_hgst")
    drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)
    ids = [s.unique_id for s in ep.sensor_specs({drive["id"]: drive}, "E", (), {})]
    assert "E_fx-ata-devstat-hgst_data_written" in ids
    assert "E_fx-ata-devstat-hgst_data_read" in ids
    scsi = agent_drive("scsi_view", status="not_applicable")
    scsi[DEVSTAT_KEY], _ = merge_devstat(scsi, None)
    kinds = [s.kind for s in ep.sensor_specs({scsi["id"]: scsi}, "E", (), {})]
    assert ep.KIND_DATA_WRITTEN not in kinds and ep.KIND_DATA_READ not in kinds


def test_unreadable_drives_get_no_specs_until_readable():
    data = golden_payload()
    created, pinned = _setup(data)
    assert not [s for s in _plan(data) if s.device == "fx-unreadable"]
    data["fx-unreadable"]["readable"] = True
    added = _poll(data, created, pinned)
    assert {s.device for s in added} == {"fx-unreadable"}


def test_forget_device_lets_a_returning_device_be_built_again():
    data = golden_payload()
    created, pinned = _setup(data)
    ep.forget_device(created, "fx-ata-healthy")
    assert not any(s.device == "fx-ata-healthy" for r in created.values() for s in r.values())
    added = _poll(data, created, pinned)
    assert {s.device for s in added} == {"fx-ata-healthy"}
    assert {s.platform for s in added} == {ep.SENSOR, ep.BINARY_SENSOR}


def test_the_drive_class_is_pinned_at_first_build():
    nvme = agent_drive("nvme_sabrent", status=None)
    data = {"x": nvme}
    pinned: dict = {}
    first = {s.key for s in ep.sensor_specs(data, "E", (), pinned)}
    assert "critical_warning" in first and "reallocated_sector_count" not in first
    flipped = copy.deepcopy(nvme)
    flipped["protocol"] = "ATA"
    later = {s.key for s in ep.sensor_specs({"x": flipped}, "E", (), pinned)}
    assert "reallocated_sector_count" not in later
    fresh = {s.key for s in ep.sensor_specs({"x": flipped}, "E", (), {})}
    assert "reallocated_sector_count" in fresh  # what an unpinned plan would do


def test_guarded_listener_logs_and_returns(caplog):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("bad payload")

    logger = logging.getLogger("tests.entity_plan")
    with caplog.at_level(logging.ERROR, logger="tests.entity_plan"):
        ep.guarded_listener(boom, logger)()
    assert calls == [1]
    assert "adding new entities failed" in caplog.text
    assert "bad payload" in caplog.text


def test_force_update_kinds():
    spec = ep.EntitySpec(ep.SENSOR, ep.KIND_DATA_WRITTEN, "u", "d")
    assert ep.wants_force_update(spec, True) and not ep.wants_force_update(spec, False)
    agent = ep.EntitySpec(ep.SENSOR, ep.KIND_AGENT_VERSION, "u", "d")
    assert not ep.wants_force_update(agent, True)
    health = ep.EntitySpec(ep.BINARY_SENSOR, ep.KIND_HEALTH, "u", "d")
    assert not ep.wants_force_update(health, True)
