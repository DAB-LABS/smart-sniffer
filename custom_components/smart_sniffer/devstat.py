"""Device Statistics: what the agent's two v0.8.0 fields mean (devstat plan v3).

The agent reads the ATA Device Statistics log in a second smartctl call and
publishes it beside ``smart_data``, which stays exactly v0.7.0's output:

  device_statistics  ``status`` present | absent | unavailable |
                     not_applicable | off, a ``reason`` for unavailable and
                     off, and for present ``complete``, ``exit_status``,
                     ``logical_block_size`` and smartctl's raw ``pages``.
  derived            ``host_writes`` / ``host_reads`` as ``bytes`` with a
                     ``source`` (``ata_device_statistics``, ``ata_attribute``
                     with ``attribute_id`` and ``attribute_name``, or
                     ``nvme``), or ``host_writes_omitted`` /
                     ``host_reads_omitted`` with a reason.

A devstat read can fail, time out or come back partial on any poll, so Home
Assistant keeps one held copy of the few readings it uses and merges per
reading (``merge_devstat``): a fresh valid value replaces the held one, and
nothing else ever clears it. Attention, the threshold form and the Data
Written / Read sensors all read the merged result, stored on the drive as
``_devstat``, so they agree.

The held readings, and which announcements have been made, live in one Store
per config entry (``smart_sniffer.devstat.<entry_id>``), removed with the
entry. A drive without a serial gets no hold and no announcement record: its
identity is the device path, and a different drive in that slot must not
inherit its numbers.

An agent older than v0.8.0 sends neither field. Then ``_devstat`` is empty
and every entity, value and reason is what v0.7.0 produced.

Nothing here imports Home Assistant, so the rules can be tested without it.
"""

from __future__ import annotations

from typing import Any

# Merged readings live on the drive payload under this key. The underscore
# keeps it apart from the agent's own fields.
DEVSTAT_KEY = "_devstat"

# What a gap-filled reason carries, inside its parenthesis:
# "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)".
# Only gap-filled reasons carry it, so every other reason string is exactly
# what v0.7.0 wrote, and a held reading gives the same text as a fresh one.
DEVSTAT_REASON_SUFFIX = "from device statistics"

# Appended to the notification raised when a drive's first poll already has
# a gap-filled reason (D11).
ANNOUNCEMENT_LINE = "First reading from this drive's Device Statistics."

STATUS_PRESENT = "present"
STATUS_ABSENT = "absent"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_OFF = "off"

SOURCE_DEVSTAT = "ata_device_statistics"
SOURCE_ATTRIBUTE = "ata_attribute"

# The readings held across polls: name -> (page, offset).
#   unc        P4 0x008  Number of Reported Uncorrectable Errors
#   realloc    P3 0x020  Number of Reallocated Logical Sectors
#   wear_used  P7 0x008  Percentage Used Endurance Indicator
HELD_READINGS: dict[str, tuple[int, int]] = {
    "unc": (4, 0x008),
    "realloc": (3, 0x020),
    "wear_used": (7, 0x008),
}

# Data volumes, held only when they came from the devstat log.
VOLUME_KEYS: tuple[str, ...] = ("host_writes", "host_reads")

# Entity attributes read from this poll's pages only, never held, so an
# attribute that is there is from this poll. sensor key -> [(attr, page, offset)].
PAGE_ATTRIBUTES: dict[str, list[tuple[str, int, int]]] = {
    "temperature": [
        ("lifetime_max", 5, 0x020),
        ("lifetime_min", 5, 0x028),
        ("time_over_limit_minutes", 5, 0x050),
    ],
    "reported_uncorrectable_errors": [("device_statistics_count", 4, 0x008)],
    "reallocated_sector_count": [("device_statistics_logical_sectors", 3, 0x020)],
}

# Gap-fill (D4): reading -> (ATA attribute id whose presence blocks it, or
# None for the wear rule). The labels come from attention.py.
_GAP_BLOCKING_ID: dict[str, int | None] = {
    "unc": 187,
    "realloc": 5,
    "wear_used": None,
}


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def device_statistics(drive_data: dict[str, Any]) -> dict[str, Any] | None:
    """The drive's device_statistics block, or None from an older agent."""
    block = drive_data.get("device_statistics")
    return block if isinstance(block, dict) else None


def devstat_status(drive_data: dict[str, Any]) -> str | None:
    block = device_statistics(drive_data)
    if block is None:
        return None
    status = block.get("status")
    return status if isinstance(status, str) else None


def _pages(drive_data: dict[str, Any]) -> list[dict[str, Any]]:
    block = device_statistics(drive_data)
    if block is None or block.get("status") != STATUS_PRESENT:
        return []
    pages = block.get("pages")
    return [p for p in pages if isinstance(p, dict)] if isinstance(pages, list) else []


def devstat_entry(pages: list[dict[str, Any]], page: int, offset: int) -> int | None:
    """The value of one devstat entry, or None.

    None unless the page is there, the entry is there, smartctl marks it valid,
    and it carries a non-negative integer value. smartctl omits ``value`` on an
    entry the drive did not fill (m01-sdd's timestamp), and an entry without
    the valid flag is the drive saying the number means nothing.
    """
    for candidate in pages:
        if candidate.get("number") != page:
            continue
        for entry in candidate.get("table") or []:
            if not isinstance(entry, dict) or entry.get("offset") != offset:
                continue
            flags = entry.get("flags")
            if not (isinstance(flags, dict) and flags.get("valid") is True):
                return None
            value = entry.get("value")
            return value if _is_count(value) else None
        return None
    return None


def _derived_volume(drive_data: dict[str, Any], key: str) -> dict[str, Any] | None:
    derived = drive_data.get("derived")
    if not isinstance(derived, dict):
        return None
    volume = derived.get(key)
    if not isinstance(volume, dict) or not _is_count(volume.get("bytes")):
        return None
    return volume


def merge_devstat(
    drive_data: dict[str, Any],
    held: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge this poll's devstat readings with the held copy.

    Returns ``(effective, new_held)``. ``effective`` goes on the drive as
    ``_devstat``; ``new_held`` is what to keep for next time.

    Per reading, by ``device_statistics.status``:

      present (complete or partial)  fresh when this poll has a valid value,
                                     else held
      unavailable                    held
      absent, off, not_applicable,   none (the held copy stays in storage,
      or no field (older agent)      unused)

    A held value is replaced only by a fresh valid one, never cleared. A drive
    with an empty serial holds nothing: fresh values only, nothing written.

    Data Written / Read: ``derived`` from any source, else the held devstat
    value (marked held), else nothing.
    """
    status = devstat_status(drive_data)
    has_derived = isinstance(drive_data.get("derived"), dict)
    serial = drive_data.get("serial")
    holds = isinstance(serial, str) and bool(serial)
    previous = dict(held) if (holds and isinstance(held, dict)) else {}

    if status is None and not has_derived:
        return {}, previous

    usable_held = previous if status in (STATUS_PRESENT, STATUS_UNAVAILABLE) else {}
    pages = _pages(drive_data)
    new_held = dict(previous)
    effective: dict[str, Any] = {"status": status, "held": []}

    for name, (page, offset) in HELD_READINGS.items():
        fresh = devstat_entry(pages, page, offset) if status == STATUS_PRESENT else None
        if fresh is not None:
            effective[name] = fresh
            if holds:
                new_held[name] = fresh
        elif _is_count(usable_held.get(name)):
            effective[name] = usable_held[name]
            effective["held"].append(name)
        else:
            effective[name] = None

    for key in VOLUME_KEYS:
        volume = _derived_volume(drive_data, key)
        if volume is not None:
            effective[key] = {**volume, "held": False}
            if (
                holds
                and status == STATUS_PRESENT
                and volume.get("source") == SOURCE_DEVSTAT
            ):
                new_held[key] = volume["bytes"]
        elif _is_count(usable_held.get(key)):
            effective[key] = {
                "bytes": usable_held[key],
                "source": SOURCE_DEVSTAT,
                "held": True,
            }
        else:
            effective[key] = None

    return effective, (new_held if holds else previous)


def effective_devstat(drive_data: dict[str, Any]) -> dict[str, Any]:
    """The merged readings the coordinator stored on this drive, or {}."""
    effective = drive_data.get(DEVSTAT_KEY)
    return effective if isinstance(effective, dict) else {}


def data_volume(drive_data: dict[str, Any], key: str) -> tuple[int | None, dict[str, Any]]:
    """State and attributes of the Data Written (``host_writes``) or Data Read
    (``host_reads``) sensor: bytes, or None for unknown.

    Attributes: ``source``, ``attribute_id`` and ``attribute_name`` for a
    vendor attribute, ``held: True`` when the value is the held one.
    """
    volume = effective_devstat(drive_data).get(key)
    if not isinstance(volume, dict) or not _is_count(volume.get("bytes")):
        return None, {}
    attrs: dict[str, Any] = {"source": volume.get("source")}
    if volume.get("source") == SOURCE_ATTRIBUTE:
        attrs["attribute_id"] = volume.get("attribute_id")
        attrs["attribute_name"] = volume.get("attribute_name")
    if volume.get("held"):
        attrs["held"] = True
    return volume["bytes"], attrs


def page_attributes(drive_data: dict[str, Any], sensor_key: str) -> dict[str, Any]:
    """Devstat attributes for one of the existing sensors, from this poll only."""
    wanted = PAGE_ATTRIBUTES.get(sensor_key)
    if not wanted:
        return {}
    pages = _pages(drive_data)
    if not pages:
        return {}
    attrs: dict[str, Any] = {}
    for name, page, offset in wanted:
        value = devstat_entry(pages, page, offset)
        if value is not None:
            attrs[name] = value
    return attrs


def devstat_gap_readings(
    smart_data: dict[str, Any],
    effective: dict[str, Any],
) -> list[tuple[str, int]]:
    """Readings from Device Statistics that fill a gap in the attribute table.

    Returns ``[(reading, value)]`` in a fixed order (unc, realloc, wear_used):

      unc        only when the table has no attribute id 187
      realloc    only when the table has no attribute id 5
      wear_used  only when no wear attribute is usable (the wear names minus
                 rows that are not a gauge, as attention.py reads them); the
                 value is percent used, no inversion

    Gated on the attribute id, not the name: Samsung's 187 is
    Uncorrectable_Error_Cnt and still blocks P4.
    """
    if not effective:
        return []
    # Imported here: attention.py imports this module at load time.
    from .attention import _ATA_WEAR_NAMES, is_dead_wear_attr  # noqa: PLC0415

    table = (smart_data.get("ata_smart_attributes") or {}).get("table") or []
    ids = {attr.get("id") for attr in table if isinstance(attr, dict)}
    usable_wear = any(
        isinstance(attr, dict)
        and attr.get("name", "") in _ATA_WEAR_NAMES
        and not is_dead_wear_attr(attr)
        for attr in table
    )
    found: list[tuple[str, int]] = []
    for name, blocking in _GAP_BLOCKING_ID.items():
        value = effective.get(name)
        if not _is_count(value):
            continue
        if blocking is not None and blocking in ids:
            continue
        if blocking is None and usable_wear:
            continue
        found.append((name, value))
    return found


def with_devstat_suffix(text: str) -> str:
    """Put the suffix inside a reason's closing parenthesis, or add one."""
    if text.endswith(")"):
        return f"{text[:-1]}; {DEVSTAT_REASON_SUFFIX})"
    return f"{text} ({DEVSTAT_REASON_SUFFIX})"


def is_devstat_reason(reason: str) -> bool:
    return DEVSTAT_REASON_SUFFIX in reason


# ---------------------------------------------------------------------------
# D11: one announcement per drive and reading
# ---------------------------------------------------------------------------

# The reason prefixes of the gap-filled readings, so a reason can be traced
# back to the reading that produced it. Filled by attention.py's wording.
GAP_REASON_PREFIXES: dict[str, str] = {
    "unc": "Reported Uncorrectable Errors:",
    "realloc": "Reallocated Sector Count:",
    "wear_used": "SSD wear at ",
}


def _record(serial: str, reading: str) -> str:
    return f"{serial}|{reading}"


def announcement_records(reasons: list[str], serial: str | None) -> list[str]:
    """Records for the gap-filled reasons among ``reasons``; [] without a serial."""
    if not (isinstance(serial, str) and serial):
        return []
    records: list[str] = []
    for reason in reasons:
        if not is_devstat_reason(reason):
            continue
        for reading, prefix in GAP_REASON_PREFIXES.items():
            if reason.startswith(prefix):
                records.append(_record(serial, reading))
                break
    return records


def baseline_announcement(
    reasons: list[str],
    serial: str | None,
    announced: list[str],
) -> tuple[bool, list[str]]:
    """At a drive's first observation: announce, and what to record.

    Announce when a reason carries the suffix, the drive has a serial, and that
    reading has not been announced for this serial before. Returns
    ``(announce, new_records)``; the records are only the ones not yet kept.
    Reasons only, never the accepted list.
    """
    known = set(announced)
    new = [r for r in announcement_records(reasons, serial) if r not in known]
    return bool(new), list(dict.fromkeys(new))


def forget_serial(announced: list[str], serial: str | None) -> list[str]:
    """``announced`` without the records of one serial."""
    if not (isinstance(serial, str) and serial):
        return list(announced)
    prefix = f"{serial}|"
    return [r for r in announced if not r.startswith(prefix)]


def normalize_store(data: Any) -> dict[str, Any]:
    """The Store payload as this version reads it, whatever was on disk."""
    if not isinstance(data, dict):
        data = {}
    announced = data.get("announced")
    held = data.get("held")
    return {
        "announced": [r for r in announced if isinstance(r, str)]
        if isinstance(announced, list)
        else [],
        "held": {k: dict(v) for k, v in held.items() if isinstance(k, str) and isinstance(v, dict)}
        if isinstance(held, dict)
        else {},
    }
