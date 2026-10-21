"""What the SMART Sniffer panel draws for one machine (v0.9).

Moved from the HA App 0.3.1 proxy (smart_sniffer_agent/web/proxy.py: the
threshold limits, the reason table, drive_view, pool_view, card keys,
attach_filesystems and summarize), so the Control Center's cards read the
same in the integration as they did in the App. The only change is where
the rules come from: the App loaded this integration's attention.py,
devstat.py and pool_health.py from a tarball; here they are imported
directly.

The panel's page does no rule matching of its own: every drive arrives with
the integration's verdict and a "view" that names its rows and their marks,
its filesystems and a card id that stays the same across reads.

Nothing here imports Home Assistant, so it can be tested without it.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import attention as _attention
from . import pool_health as _pool_health
from .devstat import DEVSTAT_KEY, merge_devstat

# ── Card ids ────────────────────────────────────────────────────────────────
#
# A card id is "<entry_id>|<card key>": the card key as the App had it (the
# drive's serial, else "id:<drive id>"; "pool:<name>"; "fs:<filesystem id>"),
# after the config entry's id, which survives restarts and address changes.

CARD_SEP = "|"


def drive_card_key(drive: dict[str, Any], drive_id: str) -> str:
    serial = drive.get("serial") if isinstance(drive, dict) else None
    if isinstance(serial, str) and serial.strip():
        return serial.strip()
    return f"id:{drive_id}"


def card_id(host_key: str, key: str) -> str:
    return f"{host_key}{CARD_SEP}{key}"


def split_card_id(text: str) -> tuple[str, str | None]:
    """(entry id, card key); the key is None for a machine's own card."""
    host_key, sep, key = str(text).partition(CARD_SEP)
    return host_key, (key if sep else None)


# ── Thresholds ──────────────────────────────────────────────────────────────
#
# The integration's per-drive thresholds: per label, "accept up to N" (for the
# NVMe spare, "accept down to N"). Only values that differ from the default
# are stored. threshold_limits() lists a drive's labels with the reading, the
# default, the value in force and what it means for the card.

_LABEL_ROWS = {
    "SSD Wear Percent Used": "wear",
    "NVMe Media Errors": "media_errors",
    "NVMe Spare Warn Below": "available_spare",
}
_COMPARE_WORDS = {"gt": "alert above", "ge": "alert at", "lt": "alert below"}


def label_row_key(label: str) -> str | None:
    """The drive view row key a threshold label sits on."""
    if label in _LABEL_ROWS:
        return _LABEL_ROWS[label]
    found = _REASON_LABELS.get(label)
    return found[0] if found else None


def _row_name(key: str | None, label: str) -> str:
    for row_key, name, *_rest in _ATA_ROWS:
        if row_key == key:
            return name
    for row_key, name in _NVME_ROWS + _SCSI_ROWS:
        if row_key == key:
            return name
    return label


def _trips(value: int, limit: int, compare: str) -> bool:
    if compare == "ge":
        return value >= limit
    if compare == "lt":
        return value < limit
    return value > limit


def threshold_text(limit: int, default: int, compare: str, unit: str) -> str:
    """ "alert above 5, default 0", the note of a row with an override."""
    return f"{_COMPARE_WORDS.get(compare, 'alert above')} {limit}{unit}, default {default}{unit}"


def accept_value(reading: int, compare: str) -> int:
    """The threshold that accepts this reading as normal."""
    return reading + 1 if compare == "ge" else reading


def clean_overrides(raw: Any) -> dict[str, int]:
    """{label: int} from stored data; anything else is dropped."""
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for label, value in raw.items():
            if isinstance(label, str) and label and not isinstance(value, bool):
                try:
                    out[label] = int(value)
                except (TypeError, ValueError):
                    continue
    return out


def threshold_limits(drive: dict[str, Any], overrides: dict[str, int]) -> list[dict[str, Any]]:
    """Every threshold label the drive's protocol has, in the integration's
    form order: {label, slug, reading, default, value, overridden, flagged,
    accepted, compare, unit, row, name}. reading is None for a label the
    drive does not report. flagged: the reading trips the value in force.
    accepted: an override keeps a reading that the default would flag from
    being flagged. The drive must carry its _devstat (evaluate)."""
    attention = _attention
    labels = list(attention.labels_for_drive(drive))
    readings = attention.current_readings(drive)
    labels = list(attention.form_order(labels, readings))
    values = attention.prefill_thresholds(drive, overrides)
    compares = getattr(attention, "_THRESHOLD_COMPARE", {})
    gauges = getattr(attention, "GAUGE_LABELS", frozenset())
    nvme = _smart_data(drive).get("nvme_smart_health_information_log") or {}
    out: list[dict[str, Any]] = []
    for label in labels:
        default = int(attention.default_threshold(label))
        value = int(values.get(label, default))
        compare = compares.get(label, "gt")
        reading = readings.get(label)
        flagged = reading is not None and _trips(reading, value, compare)
        if (flagged and label == "NVMe Spare Warn Below"
                and _int(nvme.get("available_spare_threshold")) is not None
                and reading <= _int(nvme.get("available_spare_threshold"))):
            flagged = False  # the drive's own limit: critical, never a threshold
        overridden = label in overrides and value != default
        accepted = (overridden and reading is not None and not flagged
                    and _trips(reading, default, compare))
        key = label_row_key(label)
        out.append({
            "label": label, "slug": attention.threshold_slug(label), "reading": reading,
            "default": default, "value": value, "overridden": overridden,
            "flagged": flagged, "accepted": accepted, "compare": compare,
            "unit": "%" if label in gauges else "", "row": key, "name": _row_name(key, label),
        })
    return out


def evaluate(drive: dict[str, Any], thresholds: dict[str, int] | None = None,
             rules_version: str | None = None) -> dict[str, Any]:
    """The verdict the integration gives this drive with these threshold
    overrides ({label: int}, none for the defaults). A drive the coordinator
    read already carries its merged Device Statistics readings (_devstat);
    one without them gets this read's own, with nothing held. "limits" is
    every threshold label of the drive as threshold_limits() gives it."""
    attention = _attention
    if DEVSTAT_KEY not in drive:
        drive[DEVSTAT_KEY], _ = merge_devstat(drive, None)
    overrides = dict(thresholds or {})
    state, severity, reasons, accepted = attention.evaluate_attention(drive, overrides or None)
    readings = attention.current_readings(drive)
    reasons_text = attention.compose_reasons_text(state, reasons, accepted)
    return {
        "state": state,
        "severity": severity,
        "reasons": list(reasons),
        "accepted": list(accepted),
        "readings": readings,
        "reasons_text": reasons_text,
        "rules_version": rules_version,
        "limits": threshold_limits(drive, overrides),
    }


def pool_verdict(pool: dict[str, Any]) -> dict[str, Any]:
    """The integration's verdict for one ZFS pool: {"state": "YES" | "NO",
    "problems": [...]}. Pools have no Maybe."""
    problems = list(_pool_health.pool_problems(pool))
    return {"state": "YES" if problems else "NO", "problems": problems}


# ── The drive view ──────────────────────────────────────────────────────────
#
# drive_view() turns a drive payload and its verdict into what the page
# draws: the header word, the stats, one row per watched counter with the
# mark of the reason that names it, the data volumes and the Field report
# size. The page does no rule matching of its own.

HEADER_BY_STATE = {"YES": "attention", "MAYBE": "maybe", "NO": "ok", "UNSUPPORTED": "unknown"}

# Wear attribute names, as attention._ATA_WEAR_NAMES has them (refreshed from
# the module below).
_WEAR_NAMES: set[str] = {
    "Wear_Leveling_Count", "Media_Wearout_Indicator", "SSD_Life_Left",
    "Remaining_Lifetime_Perc", "Percent_Lifetime_Remain", "Perc_Rated_Life_Remain",
    "Percent_Life_Remaining",
}

# ATA rows in display order: key, name, attribute names, attribute id.
_ATA_ROWS: list[tuple[str, str, tuple[str, ...], int | None]] = [
    ("reallocated", "Reallocated sectors", ("Reallocated_Sector_Ct", "Retired_Block_Count"), 5),
    ("pending", "Pending sectors",
     ("Current_Pending_Sector", "Current_Pending_Sector_Ct", "Total_Pending_Sectors"), 197),
    ("offline_unc", "Offline uncorrectable", ("Offline_Uncorrectable", "Total_Offl_Uncorrectabl"), 198),
    ("reported_unc", "Reported uncorrectable", ("Reported_Uncorrect", "Uncorrectable_Error_Cnt"), 187),
    ("cmd_timeout", "Command timeout", ("Command_Timeout",), 188),
    ("realloc_events", "Reallocated events", ("Reallocated_Event_Count",), 196),
    ("spin_retries", "Spin retries", ("Spin_Retry_Count",), 10),
    ("wear", "Wear used", (), None),
]
_NVME_ROWS: list[tuple[str, str]] = [
    ("critical_warning", "Critical warning"),
    ("media_errors", "Media errors"),
    ("available_spare", "Available spare"),
    ("wear", "Wear used"),
]
_SCSI_ROWS: list[tuple[str, str]] = [("grown_defects", "Grown defects")]

# The two rows an Unsupported drive shows as "not known", by protocol.
_MAIN_ROWS = {
    "ata": [("reallocated", "Reallocated sectors"), ("pending", "Pending sectors")],
    "nvme": [("critical_warning", "Critical warning"), ("media_errors", "Media errors")],
    "scsi": [("grown_defects", "Grown defects")],
}

# Device Statistics readings that stand in for a missing attribute (the
# integration's gap-fill): reading -> (page, offset, row key, row name,
# the attribute row it stands in for, the note on that row).
_DEVSTAT_ROWS: dict[str, tuple[int, int, str, str, str, str]] = {
    "unc": (4, 0x008, "reported_unc_devstat", "Reported uncorrectable",
            "reported_unc", "no attribute 187"),
    "realloc": (3, 0x020, "reallocated_devstat", "Reallocated sectors",
                "reallocated", "no attribute 5"),
    "wear_used": (7, 0x008, "wear_devstat", "Wear used",
                  "wear", "no wear attribute"),
}
_DEVSTAT_SUFFIX = "from device statistics"

# Reason label (the text before the colon) -> (row key, mark). Built from the
# rule module's own dictionaries below; this copy is the start.
_REASON_LABELS: dict[str, tuple[str, str]] = {
    "Reallocated Sector Count": ("reallocated", "crit"),
    "Current Pending Sector Count": ("pending", "crit"),
    "Offline Uncorrectable Errors": ("offline_unc", "crit"),
    "Total Offline Uncorrectable": ("offline_unc", "crit"),
    "Reported Uncorrectable Errors": ("reported_unc", "crit"),
    "Uncorrectable Error Count": ("reported_unc", "crit"),
    "Reallocated Event Count": ("realloc_events", "warn"),
    "Spin Retry Count": ("spin_retries", "warn"),
    "Command Timeout": ("cmd_timeout", "warn"),
    "NVMe media errors": ("media_errors", "crit"),
}
# Thresholds named in the expectations, refreshed from the module.
_THRESHOLDS = {"spare_warn_below": 20, "wear_warn_at": 90}


def _build_reason_table(attention: Any) -> None:
    """Refresh the reason table from attention.py's own dictionaries, so a
    counter the integration starts watching gets its row and mark."""
    if attention is None:
        return
    names_to_key = {name: key for key, _, names, _ in _ATA_ROWS for name in names}
    critical = getattr(attention, "_CRITICAL_ATA", None)
    warning = getattr(attention, "_WARNING_ATA", None)
    for table, mark in ((critical, "crit"), (warning, "warn")):
        if not isinstance(table, dict):
            continue
        for name, label in table.items():
            key = names_to_key.get(name) or re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
            _REASON_LABELS[str(label)] = (key, mark)
    media = getattr(attention, "LABEL_NVME_MEDIA_ERRORS", None)
    if isinstance(media, str):
        _REASON_LABELS.setdefault("NVMe media errors", ("media_errors", "crit"))
    wear = getattr(attention, "_ATA_WEAR_NAMES", None)
    if isinstance(wear, (set, frozenset)) and wear:
        _WEAR_NAMES.clear()
        _WEAR_NAMES.update(str(n) for n in wear)
    for key, attr in (("spare_warn_below", "_NVME_SPARE_WARN_BELOW"),
                      ("wear_warn_at", "_ATA_WEAR_WARN_THRESHOLD")):
        value = getattr(attention, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            _THRESHOLDS[key] = value


_build_reason_table(_attention)


def _short_expect(paren: str | None) -> str | None:
    """A reason's parenthesis, made short: "expected 0", "warns above 100"."""
    if not paren:
        return None
    parts = [p.strip() for p in paren.split(";")]
    out: list[str] = []
    for part in parts:
        if part == _DEVSTAT_SUFFIX:
            continue
        match = re.fullmatch(r"threshold (\d+)", part)
        out.append(f"warns above {match.group(1)}" if match else part)
    text = ", ".join(p for p in out if p)
    if _DEVSTAT_SUFFIX in parts:
        text = f"{text}, {_DEVSTAT_SUFFIX}" if text else _DEVSTAT_SUFFIX
    return text or None


def classify_reason(reason: str) -> dict[str, Any] | None:
    """Which row a verdict reason marks: {"key", "mark", "expect", "value"}.
    "value" is the reading the reason states, for a row the drive data does
    not have. None for a reason that matches nothing."""
    text = str(reason)
    devstat = _DEVSTAT_SUFFIX in text
    suffix = "_devstat" if devstat else ""
    tail = f", {_DEVSTAT_SUFFIX}" if devstat else ""
    if text.startswith("SMART overall status"):
        return {"key": "smart_status", "mark": "crit", "expect": None, "value": "FAILED"}
    match = re.match(r"NVMe critical warning flag set \((0x[0-9a-fA-F]+)\)", text)
    if match:
        return {"key": "critical_warning", "mark": "crit", "expect": "expected 0x00",
                "value": match.group(1)}
    match = re.match(r"NVMe available spare \((\d+)%\) at or below drive threshold \((\d+)%\)", text)
    if match:
        return {"key": "available_spare", "mark": "crit",
                "expect": f"at or below the drive's own {match.group(2)}%",
                "value": f"{match.group(1)}%"}
    match = re.match(r"NVMe available spare low: (\d+)% remaining(?: \(accepted down to (\d+)%\))?", text)
    if match:
        expect = (f"accepted down to {match.group(2)}%" if match.group(2)
                  else f"warns below {_THRESHOLDS['spare_warn_below']}%")
        return {"key": "available_spare", "mark": "warn", "expect": expect,
                "value": f"{match.group(1)}%"}
    match = re.match(r"(?:NVMe drive|SSD) wear at (\d+)% of rated life(?:.*?\(accepted (\d+)%\))?", text)
    if match:
        expect = (f"accepted {match.group(2)}%" if match.group(2)
                  else f"warns at {_THRESHOLDS['wear_warn_at']}%")
        return {"key": "wear" + suffix, "mark": "warn", "expect": expect + tail,
                "value": f"{match.group(1)}%"}
    match = re.match(r"(?P<label>[^:()]+): (?P<value>[^(]+?)(?: \((?P<paren>[^)]*)\))?$", text)
    if match:
        found = _REASON_LABELS.get(match.group("label").strip())
        if found:
            key, mark = found
            value = match.group("value").strip()
            if re.fullmatch(r"\d+", value):
                value = f"{int(value):,}"
            return {"key": key + suffix, "mark": mark,
                    "expect": _short_expect(match.group("paren")), "value": value}
    return None


def _smart_data(drive: dict[str, Any]) -> dict[str, Any]:
    raw = drive.get("smart_data") if isinstance(drive, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _fmt(value: int) -> str:
    return f"{value:,}"


def _decode_raw(name: str, raw: Any) -> int | None:
    """An ATA raw value as a count: Command_Timeout's low 16 bits, a packed
    value's leading figure from raw.string (as the integration reads them)."""
    if not isinstance(raw, dict):
        return _int(raw)
    value = _int(raw.get("value"))
    if value is None:
        return None
    if name == "Command_Timeout" and value > 0xFFFF:
        return value & 0xFFFF
    if value > 0xFFFF:
        match = re.match(r"\s*(\d+)", str(raw.get("string") or ""))
        if match and int(match.group(1)) != value:
            return int(match.group(1))
    return value


def _is_dead_wear(attr: dict[str, Any]) -> bool:
    flags = attr.get("flags")
    flags_value = flags.get("value") if isinstance(flags, dict) else None
    fields = (attr.get("value"), attr.get("worst"), attr.get("thresh"), flags_value)
    return all(isinstance(f, int) and not isinstance(f, bool) and f == 0 for f in fields)


def _usable_wear(table: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attr in table:
        if attr.get("name") in _WEAR_NAMES and not _is_dead_wear(attr):
            return attr
    return None


def _nvme_value(log_: dict[str, Any], key: str) -> tuple[bool, int | None]:
    """(present, value) for an NVMe counter; a 128-bit value at or above
    2^64 is present but not readable, as the integration reads it."""
    if f"{key}_le" in log_:
        return True, None
    raw = log_.get(f"{key}_s", log_.get(key))
    if raw is None:
        return key in log_, None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return True, None
    if value < 0 or value >= 1 << 64:
        return True, None
    return True, value


def _devstat_entry(pages: Any, page: int, offset: int) -> int | None:
    if not isinstance(pages, list):
        return None
    for candidate in pages:
        if not isinstance(candidate, dict) or candidate.get("number") != page:
            continue
        for entry in candidate.get("table") or []:
            if not isinstance(entry, dict) or entry.get("offset") != offset:
                continue
            flags = entry.get("flags")
            if not (isinstance(flags, dict) and flags.get("valid") is True):
                return None
            return _int(entry.get("value"))
    return None


def _devstat_readings(drive: dict[str, Any]) -> dict[str, int]:
    """The three Device Statistics readings: the merged ones the rules put on
    the drive (_devstat), else this read's pages."""
    merged = drive.get(DEVSTAT_KEY)
    out: dict[str, int] = {}
    if isinstance(merged, dict) and merged:
        for name in _DEVSTAT_ROWS:
            value = _int(merged.get(name))
            if value is not None and value >= 0:
                out[name] = value
        return out
    block = drive.get("device_statistics")
    if not isinstance(block, dict) or block.get("status") != "present":
        return out
    for name, (page, offset, *_rest) in _DEVSTAT_ROWS.items():
        value = _devstat_entry(block.get("pages"), page, offset)
        if value is not None and value >= 0:
            out[name] = value
    return out


def _protocol(drive: dict[str, Any], smart: dict[str, Any]) -> str | None:
    if smart.get("nvme_smart_health_information_log"):
        return "nvme"
    if (smart.get("ata_smart_attributes") or {}).get("table"):
        return "ata"
    proto = str(drive.get("protocol") or (smart.get("device") or {}).get("protocol") or "").lower()
    if "nvme" in proto:
        return "nvme"
    if "scsi" in proto or "sas" in proto or "scsi_grown_defect_list" in smart:
        return "scsi"
    if "ata" in proto or "sat" in proto:
        return "ata"
    return None


def _row(key: str, name: str, value: str, mark: str | None = None,
         expect: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {"key": key, "name": name, "value": value, "mark": mark, "expect": expect, "note": note}


def _ghost(key: str, name: str, note: str | None = None) -> dict[str, Any]:
    return _row(key, name, "not known", "unk", None, note)


# The attribute id whose presence blocks a gap-fill reading.
_DEVSTAT_BLOCKING = {"unc": 187, "realloc": 5, "wear_used": None}


def _data_rows(drive: dict[str, Any], smart: dict[str, Any], protocol: str | None) -> list[dict[str, Any]]:
    """Rows for the counters the drive reports, unmarked, in display order."""
    rows: list[dict[str, Any]] = []
    if protocol == "nvme":
        log_ = smart.get("nvme_smart_health_information_log") or {}
        for key, name in _NVME_ROWS:
            if key == "critical_warning":
                value = _int(log_.get("critical_warning"))
                if value is not None:
                    rows.append(_row(key, name, f"0x{value:02x}"))
            elif key == "media_errors":
                present, value = _nvme_value(log_, "media_errors")
                if present:
                    rows.append(_row(key, name, _fmt(value)) if value is not None
                                else _ghost(key, name, "not readable"))
            elif key == "available_spare":
                value = _int(log_.get("available_spare"))
                if value is not None:
                    rows.append(_row(key, name, f"{value}%"))
            elif key == "wear":
                value = _int(log_.get("percentage_used"))
                if value is not None:
                    rows.append(_row(key, name, f"{value}%"))
        return rows
    if protocol == "scsi":
        value = _int(smart.get("scsi_grown_defect_list"))
        if value is not None:
            rows.append(_row("grown_defects", "Grown defects", _fmt(value)))
        return rows
    if protocol != "ata":
        return rows

    table = [a for a in (smart.get("ata_smart_attributes") or {}).get("table") or []
             if isinstance(a, dict)]
    ids = {a.get("id") for a in table}
    gaps = {}
    readings = _devstat_readings(drive)
    for reading, (_page, _offset, dkey, dname, base, note) in _DEVSTAT_ROWS.items():
        value = readings.get(reading)
        if value is None:
            continue
        if reading == "wear_used":
            if _usable_wear(table) is not None:
                continue
        elif _DEVSTAT_BLOCKING[reading] in ids:
            continue
        gaps[base] = (dkey, dname, note, value, reading)

    for key, name, names, _attr_id in _ATA_ROWS:
        if key == "wear":
            attr = _usable_wear(table)
            value = _int(attr.get("value")) if attr else None
            if value is not None:
                rows.append(_row(key, name, f"{max(0, 100 - value)}%"))
            elif key in gaps:
                rows.append(_ghost(key, name, gaps[key][2]))
        else:
            attr = next((a for a in table if a.get("name") in names), None)
            value = _decode_raw(str(attr.get("name")), attr.get("raw")) if attr else None
            if value is not None:
                rows.append(_row(key, name, _fmt(value)))
            elif key in gaps:
                rows.append(_ghost(key, name, gaps[key][2]))
        if key in gaps:
            dkey, dname, _note, value, reading = gaps[key]
            text = f"{value}%" if reading == "wear_used" else _fmt(value)
            rows.append(_row(dkey, dname, text))
    return rows


def _stats(smart: dict[str, Any]) -> dict[str, int | None]:
    log_ = smart.get("nvme_smart_health_information_log") or {}
    temp = _int((smart.get("temperature") or {}).get("current"))
    hours = _int((smart.get("power_on_time") or {}).get("hours"))
    cycles = _int(smart.get("power_cycle_count"))
    if temp is None:
        temp = _int(log_.get("temperature"))
    if hours is None:
        hours = _int(log_.get("power_on_hours"))
    if cycles is None:
        cycles = _int(log_.get("power_cycles"))
    # Read from the ATA table first by the integration; a payload with no
    # top-level fields still has them there.
    table = (smart.get("ata_smart_attributes") or {}).get("table") or []
    by_id = {a.get("id"): a for a in table if isinstance(a, dict)}

    def _raw(*ids: int) -> int | None:
        for attr_id in ids:
            raw = (by_id.get(attr_id) or {}).get("raw") or {}
            match = re.match(r"\s*(\d+)", str(raw.get("string", "")))
            if match:
                return int(match.group(1))
        return None

    if temp is None:
        temp = _raw(194, 190)
    if hours is None:
        hours = _raw(9)
    if cycles is None:
        cycles = _raw(12)
    return {"temp_c": temp, "power_on_hours": hours, "power_cycles": cycles}


_VOLUME_SOURCES = {"ata_attribute": "attribute", "nvme": "nvme", "ata_device_statistics": "devstat"}


def _volumes(drive: dict[str, Any]) -> dict[str, Any]:
    derived = drive.get("derived")
    out: dict[str, Any] = {}
    for word, key in (("written", "host_writes"), ("read", "host_reads")):
        if not isinstance(derived, dict):
            out[word] = {"omitted": "not_reported"}
            continue
        volume = derived.get(key)
        if isinstance(volume, dict) and _int(volume.get("bytes")) is not None:
            entry: dict[str, Any] = {
                "bytes": _int(volume.get("bytes")),
                "source": _VOLUME_SOURCES.get(str(volume.get("source")), volume.get("source")),
            }
            if volume.get("attribute_id") is not None:
                entry["attribute_id"] = volume.get("attribute_id")
            out[word] = entry
        else:
            out[word] = {"omitted": derived.get(f"{key}_omitted") or "no_source"}
    return out


def _report(smart: dict[str, Any], protocol: str | None) -> dict[str, Any]:
    if protocol == "ata":
        table = (smart.get("ata_smart_attributes") or {}).get("table") or []
        return {"kind": "ata", "count": len(table)}
    if protocol == "nvme":
        return {"kind": "nvme", "count": len(smart.get("nvme_smart_health_information_log") or {})}
    if protocol == "scsi" and smart:
        keys = [k for k in smart if k.startswith("scsi_")]
        return {"kind": "scsi", "count": len(keys)}
    return {"kind": None, "count": 0}


def drive_view(drive: dict[str, Any], verdict: dict[str, Any] | None) -> dict[str, Any]:
    """What the page draws for one drive. Pure: reads only its arguments and
    the reason table. A verdict of None gives the header "unknown" and no
    marks."""
    drive = drive if isinstance(drive, dict) else {}
    smart = _smart_data(drive)
    protocol = _protocol(drive, smart)
    state = (verdict or {}).get("state")
    header = HEADER_BY_STATE.get(str(state), "unknown")
    status = smart.get("smart_status")
    health_failed = isinstance(status, dict) and status.get("passed") is False

    if state == "UNSUPPORTED":
        rows = [_ghost(key, name) for key, name in _MAIN_ROWS.get(protocol or "ata", _MAIN_ROWS["ata"])]
    else:
        rows = _data_rows(drive, smart, protocol)
    by_key = {row["key"]: row for row in rows}
    severity = (verdict or {}).get("severity")
    for reason in (verdict or {}).get("reasons") or []:
        found = classify_reason(reason)
        if found is None:
            rows.append(_row("other", str(reason), "",
                             "crit" if severity == "critical" else "warn"))
            continue
        if found["key"] == "smart_status":
            continue  # the header's Health FAILED says it
        row = by_key.get(found["key"])
        if row is None:
            name = next((n for k, n, *_ in _ATA_ROWS if k == found["key"]), None) or next(
                (n for k, n in _NVME_ROWS + _SCSI_ROWS if k == found["key"]), None)
            if name is None:
                dev = next((d for d in _DEVSTAT_ROWS.values() if d[2] == found["key"]), None)
                name = dev[3] if dev else found["key"]
            row = _row(found["key"], name, found["value"])
            rows.append(row)
            by_key[row["key"]] = row
        if row["mark"] != "crit":  # a critical mark is never weakened
            row["mark"] = found["mark"]
        row["expect"] = found["expect"]

    # Thresholds: a row with an override says it in its note, and a reading
    # the override keeps from being flagged is marked accepted.
    for limit in (verdict or {}).get("limits") or []:
        if not (limit.get("overridden") or limit.get("accepted")) or not limit.get("row"):
            continue
        for key in (limit["row"], limit["row"] + "_devstat"):
            row = by_key.get(key)
            if row is None or row["mark"] == "unk":
                continue
            if limit.get("overridden"):
                row["limit"] = {"value": limit["value"], "default": limit["default"],
                                "text": threshold_text(limit["value"], limit["default"],
                                                       limit["compare"], limit["unit"])}
            if limit.get("accepted") and row["mark"] not in ("crit", "warn"):
                row["accepted"] = True

    block = drive.get("device_statistics")
    devstat = block.get("status") if isinstance(block, dict) else None
    return {
        "header": header,
        "health_failed": health_failed,
        "protocol": protocol,
        "stats": _stats(smart),
        "rows": rows,
        "volumes": _volumes(drive),
        "devstat": devstat,
        "devstat_reason": block.get("reason") if isinstance(block, dict) else None,
        "report": _report(smart, protocol),
        "can_save": state != "UNSUPPORTED" and bool(smart),
    }


_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten", "eleven", "twelve"]


def _layout(vdev: Any, devices: list[dict[str, Any]]) -> str | None:
    if not isinstance(vdev, dict):
        return None
    kind = str(vdev.get("type") or "").lower()
    count = len(devices)
    word = _NUMBER_WORDS[count] if count < len(_NUMBER_WORDS) else str(count)
    if kind in ("disk", "file") or (not kind and count == 1):
        return "single disk"
    if kind in ("mirror", "raidz", "raidz1", "raidz2", "raidz3", "draid"):
        return f"{word}-disk {kind}"
    return f"{kind or 'stripe'} of {word} disks"


def pool_view(pool: dict[str, Any], verdict: dict[str, Any] | None,
              lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the page draws for one pool. The layout and every disk come only
    from a replica's lab block; a real agent lists only problem vdevs."""
    pool = pool if isinstance(pool, dict) else {}
    lab = lab if isinstance(lab, dict) else {}
    vanished = bool(lab.get("vanished"))
    devices = [d for d in lab.get("devices") or [] if isinstance(d, dict)]
    disks = None
    if devices:
        disks = []
        for dev in devices:
            errors = [_int(dev.get(k)) or 0 for k in ("read", "write", "cksum")]
            bad = dev.get("state") != "ONLINE" or any(errors)
            disks.append({"name": dev.get("name"), "state": dev.get("state"),
                          "read": errors[0], "write": errors[1], "cksum": errors[2],
                          "mark": "crit" if bad else None})
    state = (verdict or {}).get("state")
    header = "unknown" if vanished or state is None else ("attention" if state == "YES" else "ok")
    return {
        "header": header,
        "layout": _layout(lab.get("vdev"), devices) if devices else None,
        "state": pool.get("state"),
        "disks": disks,
        "vanished": vanished,
    }


# ── Filesystems and the machine's cards ─────────────────────────────────────


def _part_of(device: str, drive_path: str) -> bool:
    """True when a filesystem's block device is on this drive: the drive's
    path itself, or it followed by a partition number (/dev/sda1,
    /dev/nvme0n1p2, /dev/mmcblk0p1)."""
    if not drive_path or not device.startswith(drive_path):
        return False
    rest = device[len(drive_path):]
    return re.fullmatch(r"(?:n\d+)?(?:p?\d+)?", rest) is not None


def attach_filesystems(host_key: str, items: list[dict[str, Any]],
                       filesystems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put each filesystem on the card it lives on, as item["filesystems"]:
    on the drive the agent names in drive_id (agent 0.8.3 on macOS, where an
    APFS volume's device says nothing about its drive), else a block
    device's on the drive whose path starts it, a ZFS dataset on its pool's
    card. Returns the rest as FILESYSTEM cards."""
    for item in items:
        item["filesystems"] = []
    drives = [i for i in items if i.get("type") == "drive"]
    by_id = {str(d.get("id")): d for d in drives}
    pools = {str((i.get("pool") or {}).get("name")): i for i in items if i.get("type") == "pool"}
    leftover: list[dict[str, Any]] = []
    for fs in filesystems:
        device = str(fs.get("device") or "")
        drive_id = fs.get("drive_id")
        owner = None
        if isinstance(drive_id, str) and drive_id in by_id:
            owner = by_id[drive_id]
        elif str(fs.get("fstype") or "").lower() == "zfs":
            owner = pools.get(device.split("/", 1)[0])
        else:
            matches = [d for d in drives
                       if _part_of(device, str((d.get("drive") or {}).get("device_path") or ""))]
            if matches:
                owner = max(matches, key=lambda d: len(str(d["drive"].get("device_path"))))
        if owner is not None:
            owner["filesystems"].append(fs)
            continue
        fs_id = str(fs.get("id") or fs.get("mountpoint") or len(leftover))
        leftover.append({"type": "filesystem", "id": fs_id,
                         "card_id": card_id(host_key, f"fs:{fs_id}"), "filesystem": fs})
    return leftover


def card_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Every card of a machine's read: its items, then its FILESYSTEM cards."""
    return [c for c in (response.get("items") or []) + (response.get("filesystem_cards") or [])
            if isinstance(c, dict) and c.get("card_id")]


def summarize(items: list[dict[str, Any]], ok: bool) -> dict[str, Any]:
    """The machine's header and the counts behind it."""
    drives = [i for i in items if i.get("type") == "drive"]
    pools = [i for i in items if i.get("type") == "pool"]
    states = [((i.get("verdict") or {}).get("state")) for i in drives]
    pool_states = [((i.get("verdict") or {}).get("state")) for i in pools
                   if not (i.get("view") or {}).get("vanished")]
    attention = states.count("YES") + pool_states.count("YES")
    maybe = states.count("MAYBE")
    unsupported = states.count("UNSUPPORTED")
    judged = any(s in ("YES", "MAYBE", "NO") for s in states + pool_states)
    if not ok:
        header = "unknown"
    elif attention:
        header = "attention"
    elif maybe:
        header = "maybe"
    elif judged:
        header = "ok"
    else:
        header = "unknown"
    return {"header": header, "attention": attention, "maybe": maybe,
            "unsupported": unsupported, "drives": len(drives), "pools": len(pools)}


def without_removed(response: dict[str, Any], removed: set[str]) -> dict[str, Any]:
    """A machine's read without the cards removed with the trash; the summary
    counted again over what is left."""
    if not removed:
        return response
    items = [i for i in response.get("items") or [] if i.get("card_id") not in removed]
    fs_cards = [c for c in response.get("filesystem_cards") or []
                if c.get("card_id") not in removed]
    return {**response, "items": items, "filesystem_cards": fs_cards,
            "summary": summarize(items, response.get("status") == "ok")}


def build_items(host_key: str, drives: dict[str, dict[str, Any]],
                thresholds_for: Any, rules_version: str | None,
                pools: list[dict[str, Any]] | None,
                filesystems: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]],
                                                                   list[dict[str, Any]]]:
    """(items, filesystem cards) of one machine from what its coordinator
    read: every drive and pool judged, every card given its id.
    ``thresholds_for(drive_id)`` gives the drive's overrides. The coordinator's
    data is never changed: each drive is judged on a copy."""
    items: list[dict[str, Any]] = []
    for drive_id, payload in drives.items():
        payload = payload if isinstance(payload, dict) else {}
        # The drive as the agent sent it; the merged Device Statistics stay
        # on the working copy the rules and the view read.
        drive = {k: v for k, v in payload.items() if k != DEVSTAT_KEY}
        if "smart_data" not in drive:
            # A drive whose read failed is kept as its summary, unjudged.
            items.append({"type": "drive", "id": str(drive_id), "drive": drive, "verdict": None,
                          "view": None, "lab": None, "error": "not_answering"})
            continue
        work = copy.copy(payload)
        verdict = evaluate(work, clean_overrides(thresholds_for(drive_id)), rules_version)
        items.append({"type": "drive", "id": str(drive_id), "drive": drive,
                      "verdict": verdict, "view": drive_view(work, verdict),
                      "lab": None, "thresholds_source": "integration"})
    for pool in pools or []:
        if not isinstance(pool, dict) or not pool.get("name"):
            continue
        verdict = pool_verdict(pool)
        items.append({"type": "pool", "id": f"pool.{pool.get('name')}", "pool": pool,
                      "verdict": verdict, "view": pool_view(pool, verdict, None), "lab": None})
    for item in items:
        key = (f"pool:{item['pool'].get('name')}" if item["type"] == "pool"
               else drive_card_key(item["drive"], item["id"]))
        item["card_id"] = card_id(host_key, key)
    fs_cards = attach_filesystems(host_key, items,
                                  [f for f in filesystems or [] if isinstance(f, dict)])
    return items, fs_cards


def mark_stale(response: dict[str, Any], status: str) -> dict[str, Any]:
    """A last good read shown again while its machine does not answer as it
    should: every card marked stale, the status said."""
    items = [{**item, "stale": True} for item in response.get("items") or []]
    fs_cards = [{**c, "stale": True} for c in response.get("filesystem_cards") or []]
    return {**response, "status": status, "items": items, "filesystem_cards": fs_cards,
            "summary": summarize(items, False), "stale": True}


# ── Trash: what it can do in Home Assistant ─────────────────────────────────
#
# What the trash can do in Home Assistant besides removing a card from the
# page, as the integration's device_removal.py rules allow it:
#   "entry"    the machine's last card: its whole config entry is deleted,
#              which takes every device and entity of it;
#   "delete"   the agent no longer reports the drive or pool, so the
#              integration lets its device be deleted;
#   "disable"  the agent still reports it (or is not reporting at all, when
#              the integration deletes nothing): the device is disabled,
#              which stops its entities and keeps their history.

HA_REMOVAL_MODES = ("delete", "disable", "entry")


def ha_removal_mode(item_type: Any, card_key: str, target: dict[str, Any] | None,
                    response: Any, last: bool) -> str | None:
    """The Home Assistant removal for one card, or None when the card has no
    device in Home Assistant.

    ``response`` is the machine's last read, cards removed with the trash
    included: what the agent reports. ``last`` is whether this card is the
    machine's last one on the page."""
    if target is None or item_type not in ("drive", "pool"):
        return None
    device = (target.get("devices") or {}).get(card_key)
    if device is None:
        return None
    if last:
        return "entry"
    if not isinstance(response, dict) or response.get("status") != "ok" or response.get("stale"):
        return "disable"
    items = [i for i in response.get("items") or [] if isinstance(i, dict)]
    if item_type == "pool":
        if response.get("pools_status") != "ok":
            return "disable"
        names = {str((i.get("pool") or {}).get("name")) for i in items if i.get("type") == "pool"}
        return "disable" if card_key[len("pool:"):] in names else "delete"
    identifier = (target.get("identifiers") or {}).get(card_key)
    if identifier is None:
        return "disable"  # the device's drive id is not known: never offer a delete
    reported = {str(i.get("id")) for i in items if i.get("type") == "drive"}
    return "disable" if identifier in reported else "delete"


def ha_removals(host_key: str, target: dict[str, Any] | None, response: dict[str, Any],
                removed: set[str]) -> dict[str, str]:
    """Card id -> what the trash can do in Home Assistant, for every card of
    one machine shown on the page. Hidden cards count as shown."""
    out: dict[str, str] = {}
    if target is None:
        return out
    cards = card_list(without_removed(response, removed))
    for card in cards:
        card_key = split_card_id(card["card_id"])[1] or ""
        mode = ha_removal_mode(card.get("type"), card_key, target, response,
                               len(cards) == 1)
        if mode is not None:
            out[card["card_id"]] = mode
    return out
