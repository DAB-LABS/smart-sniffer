"""Which entities a coordinator payload calls for (D7, devstat plan v3 6.5).

The platform setup used to decide this inline, once, at setup: a pool, a
filesystem or a drive that turned up later, or a drive that was asleep or
unreadable when Home Assistant started, got no entities until the next reload.
The decision now lives here, as plain data (``EntitySpec``), so the platforms
can run it at setup and again after every poll, adding only what is new, and
the tests can check it without Home Assistant.

The rules are the v0.7.0 setup rules, moved unchanged (sensor.py and
binary_sensor.py at 36ebe65), plus Data Written and Data Read, which exist
once the drive has a value for them. Two rules keep a running integration
stable:

  - A drive's class (ATA, NVMe) is pinned the first time it is planned, so a
    payload that later reads differently cannot sprout the other protocol's
    sensors.
  - An unreadable drive gets no specs, so no device is built from an identity
    the agent could not confirm (smart-sniffer-app#7).

Nothing here imports Home Assistant.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .attention import coerce_smart_data, is_dead_wear_attr
from .const import FILESYSTEMS_KEY
from .devstat import data_volume
from .extract import (
    ATA_NAME_MAP,
    ATA_ONLY_KEYS,
    NVME_ONLY_KEYS,
    SENSOR_KEYS,
    SKIP_IF_NOT_PRESENT,
    _extract_attribute,
)
from .pool_health import ERROR_KEYS, pool_identifier, pool_names_for_setup

_LOGGER = logging.getLogger(__name__)

SENSOR = "sensor"
BINARY_SENSOR = "binary_sensor"

# Sensor kinds.
KIND_ATTRIBUTE = "attribute"
KIND_DATA_WRITTEN = "data_written"
KIND_DATA_READ = "data_read"
KIND_ATTENTION = "attention"
KIND_ATTENTION_REASONS = "attention_reasons"
KIND_DIAGNOSTIC_ATTR = "diagnostic_attr"
KIND_FILESYSTEM = "filesystem"
KIND_POOL_STATE = "pool_state"
KIND_POOL_ERROR = "pool_error"
KIND_POOL_LAST_SCRUB = "pool_last_scrub"
KIND_AGENT_VERSION = "agent_version"
KIND_AGENT_LAST_SEEN = "agent_last_seen"
KIND_AGENT_IP = "agent_ip"
KIND_AGENT_PORT = "agent_port"
KIND_AGENT_OS = "agent_os"
KIND_AGENT_POLL_INTERVAL = "agent_scan_interval"

# Binary sensor kinds.
KIND_HEALTH = "health"
KIND_STANDBY = "standby"
KIND_POOL_DATA_ERRORS = "pool_data_errors"
KIND_POOL_PROBLEM = "pool_problem"
KIND_AGENT_STATUS = "agent_status"
KIND_AUTH_ACTIVE = "auth_active"

# The data volume sensors: kind -> the derived key they show.
VOLUME_KINDS: dict[str, str] = {
    KIND_DATA_WRITTEN: "host_writes",
    KIND_DATA_READ: "host_reads",
}

# Kinds that write a state on every poll when the force_update option is on
# (issue #40): the SMART data, attention, and filesystem and pool sensors, as
# in v0.7.0, plus the data volume sensors. Agent metadata is excluded, and the
# binary sensors never had it.
FORCE_UPDATE_KINDS: dict[str, frozenset[str]] = {
    SENSOR: frozenset({
        KIND_ATTRIBUTE,
        KIND_DIAGNOSTIC_ATTR,
        KIND_DATA_WRITTEN,
        KIND_DATA_READ,
        KIND_ATTENTION,
        KIND_ATTENTION_REASONS,
        KIND_FILESYSTEM,
        KIND_POOL_STATE,
        KIND_POOL_ERROR,
    }),
    BINARY_SENSOR: frozenset(),
}

_AGENT_SENSOR_KINDS: tuple[str, ...] = (
    KIND_AGENT_VERSION,
    KIND_AGENT_LAST_SEEN,
    KIND_AGENT_IP,
    KIND_AGENT_PORT,
    KIND_AGENT_OS,
    KIND_AGENT_POLL_INTERVAL,
)


@dataclass(frozen=True)
class EntitySpec:
    """One entity to create: what it is, and the device it belongs to.

    ``subject`` is the drive id, the filesystem id or the pool name. ``key``
    is the curated sensor key or the pool error key; ``attr_id`` and
    ``attr_name`` name a dynamic diagnostic attribute.
    """

    platform: str
    kind: str
    unique_id: str
    device: str
    subject: str | None = None
    key: str | None = None
    attr_id: int | None = None
    attr_name: str | None = None


@dataclass(frozen=True)
class DriveClass:
    """A drive's protocol class as setup reads it."""

    protocol: str
    is_ata: bool
    is_nvme: bool


def drive_class(drive_data: dict[str, Any]) -> DriveClass:
    protocol = drive_data.get("protocol", "").upper()
    smart_data = coerce_smart_data(drive_data)
    has_ata_attrs = bool(smart_data.get("ata_smart_attributes"))
    return DriveClass(
        protocol=protocol,
        is_ata=protocol in ("ATA", "SATA", "") or has_ata_attrs,
        is_nvme=protocol == "NVME" and not has_ata_attrs,
    )


def pinned_class(
    drive_id: str,
    drive_data: dict[str, Any],
    pinned: dict[str, DriveClass],
) -> DriveClass:
    """The drive's class from its first plan, recording it on first sight."""
    if drive_id not in pinned:
        pinned[drive_id] = drive_class(drive_data)
    return pinned[drive_id]


def _drives(data: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for drive_id, drive_data in data.items():
        if drive_id.startswith("_"):
            continue  # skip internal keys like _filesystems
        if drive_data.get("readable") is False:
            # The agent could not read this drive, so its identity may not be
            # trustworthy. Do not build a device for it. Agents before v0.6.1
            # omit the field entirely, which reads as None here rather than
            # False, so they are unaffected. See smart-sniffer-app#7.
            _LOGGER.debug("Skipping unreadable drive %s", drive_id)
            continue
        yield drive_id, drive_data


def _covered_rows(ata_table: list[dict[str, Any]]) -> set[int]:
    """Table rows a curated sensor already reads, so no diagnostic entity
    duplicates them.

    For each curated sensor, only the single name variant that actually wins
    the lookup in _extract_attribute (which iterates the drive's attribute
    table in order and returns the first match) is suppressed. Other variants
    from the same consolidation list stay eligible to become diagnostic
    entities.

    This matters for multi-variant drives like the Transcend MTS952T (Silicon
    Motion) which reports BOTH attribute 177 (Wear_Leveling_Count) AND 169
    (Remaining_Lifetime_Perc). Without per-drive detection, the global union
    of all variant names would suppress 169 even though only 177 won the
    consolidated sensor.
    """
    # First position only: _extract_attribute returns on the first row whose
    # name matches, so a name appearing twice must resolve to the earlier row.
    # A plain dict comprehension would record the last one.
    attr_position: dict[str, int] = {}
    for i, attr in enumerate(ata_table):
        name = attr.get("name")
        if name and name not in attr_position:
            attr_position[name] = i

    # Suppress by table row, not by name. A drive can report the same
    # attribute name at two different IDs (SK hynix reports Program_Fail_Count
    # at both 175 and 181, #27). Suppressing by name would hide every row
    # sharing that name while the curated sensor only ever consumed one of
    # them, so the others would vanish from both paths with nothing to
    # indicate they existed.
    covered: set[int] = set()
    for key in SENSOR_KEYS:
        candidates = ATA_NAME_MAP.get(key, [])
        if key == "wear_leveling_count":
            # The wear lookup skips rows that are not a gauge (GH #55), so the
            # covered row is the first usable one, not the first by name.
            # Skipped rows stay eligible as diagnostic entities.
            for i, attr in enumerate(ata_table):
                if attr.get("name") in candidates and not is_dead_wear_attr(attr):
                    covered.add(i)
                    break
            continue
        present = [n for n in candidates if n in attr_position]
        if present:
            # Match _extract_attribute's "first in drive-table order" rule so
            # the same variant wins in both paths.
            winner = min(present, key=lambda n: attr_position[n])
            covered.add(attr_position[winner])
    return covered


def _drive_sensor_specs(
    entry_id: str,
    drive_id: str,
    drive_data: dict[str, Any],
    cls: DriveClass,
) -> list[EntitySpec]:
    def spec(kind: str, suffix: str, **extra: Any) -> EntitySpec:
        return EntitySpec(
            SENSOR, kind, f"{entry_id}_{drive_id}_{suffix}", drive_id, drive_id, **extra
        )

    specs: list[EntitySpec] = []

    # --- SMART attribute sensors ---
    for key in SENSOR_KEYS:
        if key in ATA_ONLY_KEYS and not cls.is_ata:
            _LOGGER.debug(
                "Skipping ATA-only sensor '%s' for %s drive %s", key, cls.protocol, drive_id
            )
            continue
        if key in NVME_ONLY_KEYS and not cls.is_nvme:
            _LOGGER.debug(
                "Skipping NVMe-only sensor '%s' for ATA/SATA drive %s", key, drive_id
            )
            continue
        if key in SKIP_IF_NOT_PRESENT and _extract_attribute(drive_data, key) is None:
            _LOGGER.debug(
                "Skipping sensor '%s' for drive %s — not in SMART data", key, drive_id
            )
            continue
        specs.append(spec(KIND_ATTRIBUTE, key, key=key))

    # --- Data Written / Data Read, once the drive has a value (D3) ---
    for kind, volume_key in VOLUME_KINDS.items():
        if data_volume(drive_data, volume_key)[0] is not None:
            specs.append(spec(kind, kind, key=kind))

    # --- Attention Needed and Attention Reasons (one each, always) ---
    specs.append(spec(KIND_ATTENTION, "attention"))
    specs.append(spec(KIND_ATTENTION_REASONS, "attention_reasons"))

    # --- Dynamic diagnostic entities for remaining SMART attributes ---
    # For ATA drives in the smartctl database, expose all named attributes
    # that aren't already covered by a dedicated sensor. Created disabled by
    # default; power users enable what they need.
    smart_data = coerce_smart_data(drive_data)
    ata_table = (smart_data.get("ata_smart_attributes") or {}).get("table", [])
    if cls.is_ata and ata_table:
        covered = _covered_rows(ata_table)
        seen_ids: set[int] = set()
        for row, attr in enumerate(ata_table):
            attr_name = attr.get("name", "")
            attr_id = attr.get("id", 0)
            # Skip unnamed, unknown, already-covered, or duplicate IDs
            if (
                not attr_name
                or attr_name.startswith("Unknown")
                or row in covered
                or attr_id in seen_ids
            ):
                continue
            seen_ids.add(attr_id)
            specs.append(
                spec(
                    KIND_DIAGNOSTIC_ATTR,
                    f"smart_attr_{attr_id}",
                    key=f"smart_attr_{attr_id}",
                    attr_id=attr_id,
                    attr_name=attr_name,
                )
            )
    return specs


def sensor_specs(
    data: dict[str, Any] | None,
    entry_id: str,
    registered_pools: Iterable[str],
    pinned: dict[str, DriveClass],
) -> list[EntitySpec]:
    """Every sensor this payload calls for, in v0.7.0's order."""
    data = data or {}
    specs: list[EntitySpec] = []
    for drive_id, drive_data in _drives(data):
        cls = pinned_class(drive_id, drive_data, pinned)
        specs += _drive_sensor_specs(entry_id, drive_id, drive_data, cls)

    # --- Filesystem usage sensors (one per monitored mountpoint) ---
    for fs_info in data.get(FILESYSTEMS_KEY) or []:
        fs_id = fs_info["id"]
        specs.append(
            EntitySpec(
                SENSOR, KIND_FILESYSTEM, f"{entry_id}_{fs_id}_usage",
                f"{entry_id}_filesystems", fs_id,
            )
        )

    # --- ZFS pool sensors (one device per pool, GH #50) ---
    # Every pool reported now and every pool device already registered, so a
    # pool that failed to import before Home Assistant started still shows.
    for name in pool_names_for_setup(data, registered_pools):
        device = pool_identifier(entry_id, name)
        specs.append(EntitySpec(SENSOR, KIND_POOL_STATE, f"{device}_pool_state", device, name))
        for key in ERROR_KEYS:
            specs.append(
                EntitySpec(SENSOR, KIND_POOL_ERROR, f"{device}_pool_{key}", device, name, key=key)
            )
        specs.append(
            EntitySpec(SENSOR, KIND_POOL_LAST_SCRUB, f"{device}_pool_last_scrub", device, name)
        )

    # --- Agent diagnostic sensors ---
    for kind in _AGENT_SENSOR_KINDS:
        specs.append(EntitySpec(SENSOR, kind, f"{entry_id}_{kind}", f"{entry_id}_agent"))
    return specs


def binary_sensor_specs(
    data: dict[str, Any] | None,
    entry_id: str,
    registered_pools: Iterable[str],
) -> list[EntitySpec]:
    """Every binary sensor this payload calls for, in v0.7.0's order."""
    data = data or {}
    specs: list[EntitySpec] = []
    # Per-drive health + standby sensors.
    for drive_id, _ in _drives(data):
        for kind in (KIND_HEALTH, KIND_STANDBY):
            specs.append(
                EntitySpec(BINARY_SENSOR, kind, f"{entry_id}_{drive_id}_{kind}", drive_id, drive_id)
            )

    # ZFS pool problem sensors (one device per pool, GH #50), for every pool
    # reported now and every pool device already registered.
    for name in pool_names_for_setup(data, registered_pools):
        device = pool_identifier(entry_id, name)
        for kind in (KIND_POOL_DATA_ERRORS, KIND_POOL_PROBLEM):
            specs.append(EntitySpec(BINARY_SENSOR, kind, f"{device}_{kind}", device, name))

    # Agent-level connectivity and auth sensors.
    specs.append(
        EntitySpec(BINARY_SENSOR, KIND_AGENT_STATUS, f"{entry_id}_agent_status", f"{entry_id}_agent")
    )
    specs.append(
        EntitySpec(BINARY_SENSOR, KIND_AUTH_ACTIVE, f"{entry_id}_auth_active", f"{entry_id}_agent")
    )
    return specs


def wants_force_update(spec: EntitySpec, enabled: bool) -> bool:
    """Whether this entity gets force_update, given the entry's option."""
    return enabled and spec.kind in FORCE_UPDATE_KINDS.get(spec.platform, frozenset())


def take_new(
    plan: Iterable[EntitySpec],
    created: dict[str, EntitySpec],
) -> list[EntitySpec]:
    """The specs not created yet, recorded as created.

    Recorded here, before the platform adds them: an add after setup is
    scheduled rather than done at once (entity_platform.py, 2024.4.0), so a
    second poll arriving first must not see them as still missing.
    """
    new: list[EntitySpec] = []
    for spec in plan:
        if spec.unique_id in created:
            continue
        created[spec.unique_id] = spec
        new.append(spec)
    return new


def forget_device(created: dict[str, dict[str, EntitySpec]], device: str) -> None:
    """Drop every record of one device's entities, on every platform, so the
    device is built again if it comes back after the user removed it."""
    for records in created.values():
        for unique_id in [u for u, s in records.items() if s.device == device]:
            del records[unique_id]


def guarded_listener(
    func: Callable[[], None],
    logger: logging.Logger,
) -> Callable[[], None]:
    """A coordinator listener that cannot break the others.

    At 2024.4.0 the coordinator calls its listeners in a plain loop with no
    try/except (update_coordinator.py:165-168), so an exception in one would
    stop every entity after it from updating. This one logs and returns.
    """

    def _guarded() -> None:
        try:
            func()
        except Exception:  # noqa: BLE001 - the point is to contain anything
            logger.exception("SMART Sniffer: adding new entities failed")

    return _guarded
