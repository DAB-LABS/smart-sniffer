#!/usr/bin/env python3
"""SMART Sniffer Mock Agent: a fake smartha-agent for integration testing.

Serves the same REST API as the real Go agent at v0.8.0 (/api/health,
/api/drives, /api/drives/{id} with device_statistics and derived, and
/api/pools) with fully controllable fake data. Every drive and pool comes
from a replica file (tools/replicas/*.json): add one, apply a scenario or
change its readings in real time, and watch the Home Assistant integration
react. A built-in web dashboard does the same.

The control routes (add, remove, edit) also live under /api/, so the SMART
Sniffer app can reach them through its proxy, which rewrites /mock/... to
/api/... on the way in. The dashboard calls the same routes as /mock/...;
both spellings work.

Usage:
    python3 mock-agent.py                       # port 9099, no auth
    python3 mock-agent.py --port 9100           # custom port
    python3 mock-agent.py --token mysecret      # enable bearer auth
    python3 mock-agent.py --no-mdns             # disable mDNS advertisement
    python3 mock-agent.py --data-dir /data      # keep drives and pools across restarts
    python3 mock-agent.py --preload sata_hdd,nvme,zfs_pool
    python3 mock-agent.py --replicas /opt/replicas --user-replicas /share/smart-sniffer/replicas

Requirements: Python 3.9+, stdlib only. The zeroconf package is optional and
only used for the mDNS advertisement.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import unquote, urlparse

# ── Version ──────────────────────────────────────────────────────────────────
# The agent API this mock follows, with a suffix so nobody mistakes it for a
# real agent in Home Assistant's device info.
VERSION = "0.8.0-mock"

# Set when the server is built; /api/health reports uptime from it.
STARTED_AT = time.time()

# ── Pool records ─────────────────────────────────────────────────────────────
# A pool is not a drive: it lives in its own collection and is served on
# /api/pools in the agent's PoolInfo shape. The mock keeps the full device
# list so it can build problem_vdevs and the error totals the way the agent
# does from zpool status.


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# What zpool prints for a pool with a faulted disk. Used as the status and
# action text when a pool leaves ONLINE and the caller gave none.
_FAULTED_STATUS = ("One or more devices are faulted in response to persistent errors. "
                   "Sufficient replicas exist for the pool to continue functioning in a degraded state.")
_FAULTED_ACTION = "Replace the faulted device, or use 'zpool clear' to mark the device repaired."

_DEVICE_STATES = ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED")

# ── Device Statistics ────────────────────────────────────────────────────────

DEVSTAT_STATUSES = ("present", "absent", "unavailable", "not_applicable", "off")
DEVSTAT_REASONS: dict[str, tuple[str, ...]] = {
    "unavailable": ("failed", "timeout", "standby", "mismatch", "stopped"),
    "off": ("config", "os"),
}

# Editable Device Statistics counts: name -> (page, offset, entry name, size).
# These are the three readings the integration holds and gap-fills from.
DEVSTAT_COUNTS: dict[str, tuple[int, int, str, int]] = {
    "reported_uncorrectable":      (4, 0x008, "Number of Reported Uncorrectable Errors", 4),
    "reallocated_logical_sectors": (3, 0x020, "Number of Reallocated Logical Sectors", 4),
    "percentage_used_endurance":   (7, 0x008, "Percentage Used Endurance Indicator", 1),
}

_PAGE_NAMES = {
    1: "General Statistics",
    3: "Rotating Media Statistics",
    4: "General Errors Statistics",
    5: "Temperature Statistics",
    7: "Solid State Device Statistics",
}


def _devstat_entry(offset: int, name: str, size: int, value: int) -> dict:
    return {
        "offset": offset, "name": name, "size": size, "value": value,
        "flags": {"value": 192, "string": "V--- ", "valid": True, "normalized": False,
                  "supports_dsn": False, "monitored_condition_met": False},
    }


def _devstat_page(number: int, table: list[dict]) -> dict:
    return {"number": number, "name": _PAGE_NAMES.get(number, ""), "revision": 1, "table": table}


def _find_entry(pages: list[dict], page: int, offset: int) -> dict | None:
    for p in pages:
        if p.get("number") == page:
            for e in p.get("table") or []:
                if e.get("offset") == offset:
                    return e
    return None


def _set_entry(pages: list[dict], page: int, offset: int, name: str, size: int, value: int) -> None:
    """Set one entry's value, creating the page or the entry if needed."""
    entry = _find_entry(pages, page, offset)
    if entry is not None:
        entry["value"] = value
        return
    for p in pages:
        if p.get("number") == page:
            p.setdefault("table", []).append(_devstat_entry(offset, name, size, value))
            p["table"].sort(key=lambda e: e.get("offset", 0))
            return
    pages.append(_devstat_page(page, [_devstat_entry(offset, name, size, value)]))
    pages.sort(key=lambda p: p.get("number", 0))


def _natural_devstat(drive: dict) -> dict:
    natural = drive.get("_devstat")
    if not isinstance(natural, dict):
        natural = _fallback_devstat(drive)
        drive["_devstat"] = natural
    return natural


def devstat_block(drive: dict) -> dict:
    """The device_statistics field of /api/drives/{id}, as the agent sends it."""
    natural = _natural_devstat(drive)
    override = drive.get("_devstat_override")
    current = override if isinstance(override, dict) else natural
    status = current.get("status")
    if status == "present":
        complete = bool(natural.get("complete", True))
        return {
            "status": "present",
            "complete": complete,
            "exit_status": 0 if complete else 4,
            "logical_block_size": natural.get("logical_block_size", 512),
            "pages": copy.deepcopy(natural.get("pages") or []),
        }
    block: dict[str, Any] = {"status": status}
    if current.get("reason"):
        block["reason"] = current["reason"]
    return block


# ── Data Written / Data Read (the agent's derivation, devstat.go) ───────────

_MISSING, _OK, _INVALID, _OVERFLOW = "missing", "ok", "invalid", "overflow"
_U64 = 1 << 64
NVME_DATA_UNIT = 512_000
_RATE_BOUND = 1.2e9          # bytes per second
_MIN_POH_FOR_BOUND = 24
_KIOXIA_EXCERIA = re.compile(r"^KIOXIA-EXCERIA SATA SSD")

_WRITE_SPEC = {
    "offset": 0x018,  # Logical Sectors Written
    "named": {"Host_Writes_32MiB": 32 << 20, "Host_Writes_GiB": 1 << 30,
              "Lifetime_Writes_GiB": 1 << 30, "Total_Writes_GiB": 1 << 30},
    "sectors": "Total_LBAs_Written",
    "preferred": 241,
    "nvme": "data_units_written",
}
_READ_SPEC = {
    "offset": 0x028,  # Logical Sectors Read
    "named": {"Host_Reads_32MiB": 32 << 20, "Host_Reads_GiB": 1 << 30,
              "Lifetime_Reads_GiB": 1 << 30, "Total_Reads_GiB": 1 << 30},
    "sectors": "Total_LBAs_Read",
    "preferred": 242,
    "nvme": "data_units_read",
}
_SPECS = {"host_writes": _WRITE_SPEC, "host_reads": _READ_SPEC}


def _parse_uint(raw: Any) -> tuple[int, str]:
    """A JSON number, or a string holding one, as a non-negative int below 2^64."""
    if raw is None:
        return 0, _MISSING
    if isinstance(raw, bool):
        return 0, _INVALID
    if isinstance(raw, str):
        text = raw.strip()
        if not text.isdigit():
            return 0, _INVALID
        value = int(text)
    elif isinstance(raw, int):
        if raw < 0:
            return 0, _INVALID
        value = raw
    else:
        return 0, _INVALID   # a float never parses as an unsigned integer
    if value >= _U64:
        return 0, _OVERFLOW
    return value, _OK


def _num_field(obj: Any, key: str) -> tuple[int, str]:
    """obj[key] as a uint64, preferring smartctl's obj[key + "_s"] string form."""
    if not isinstance(obj, dict):
        return 0, _MISSING
    if f"{key}_s" in obj:
        value, status = _parse_uint(obj[f"{key}_s"])
        if status != _MISSING:
            return value, status
    return _parse_uint(obj.get(key))


def _attr_table(smart: dict) -> list[dict]:
    table = (smart.get("ata_smart_attributes") or {}).get("table") or []
    return [a for a in table if isinstance(a, dict)]


def _power_on_hours(smart: dict) -> int | None:
    value, status = _num_field(smart.get("power_on_time"), "hours")
    if status == _OK:
        return value
    # The mock's simpler presets carry power-on hours only as attribute 9.
    for attr in _attr_table(smart):
        if attr.get("id") == 9:
            value, status = _num_field(attr.get("raw"), "value")
            if status == _OK:
                return value
    return None


def _devstat_volume(smart: dict, ds: dict, spec: dict) -> tuple[dict | None, str]:
    pages = ds.get("pages")
    if not isinstance(pages, list):
        return None, "entry_invalid"
    entry = None
    for p in pages:
        if isinstance(p, dict) and p.get("number") == 1:
            for e in p.get("table") or []:
                if isinstance(e, dict) and _parse_uint(e.get("offset")) == (spec["offset"], _OK):
                    entry = e
    if entry is None:
        return None, "entry_invalid"
    flags = entry.get("flags")
    if not (isinstance(flags, dict) and flags.get("valid") is True):
        return None, "entry_invalid"
    value, status = _num_field(entry, "value")
    if status in (_MISSING, _INVALID):
        return None, "entry_invalid"
    if status == _OVERFLOW:
        return None, "overflow"
    lbs = ds.get("logical_block_size") or 0
    if not lbs:
        lbs, _ = _parse_uint(smart.get("logical_block_size"))
    if not (512 <= lbs <= 65536 and lbs & (lbs - 1) == 0):
        return None, "block_size"
    if value * lbs >= _U64:
        return None, "overflow"
    return {"bytes": value * lbs, "source": "ata_device_statistics"}, ""


def _vendor_volume(smart: dict, spec: dict) -> tuple[dict | None, str]:
    table = _attr_table(smart)
    cands = []
    for attr in table:
        name = attr.get("name")
        if name in spec["named"]:
            cands.append({"id": attr.get("id"), "name": name, "unit": spec["named"][name], "named": True})
        elif name == spec["sectors"]:
            cands.append({"id": attr.get("id"), "name": name, "unit": 512, "named": False})
    if not cands:
        return None, "no_source"
    if _KIOXIA_EXCERIA.match(str(smart.get("model_name") or "")):
        cands = [c for c in cands if not c["name"].startswith("Lifetime_")]
        if not cands:
            return None, "excluded_model"
    block, _ = _parse_uint(smart.get("logical_block_size"))
    sector_reason = ""
    if block != 512:
        sector_reason = "block_size"
    elif smart.get("in_smartctl_database") is not True:
        sector_reason = "not_in_database"
    if sector_reason:
        cands = [c for c in cands if c["named"]]
        if not cands:
            return None, sector_reason
    by_id = {a.get("id"): a for a in table}
    valid = []
    for c in cands:
        value, status = _num_field((by_id.get(c["id"]) or {}).get("raw"), "value")
        if status != _OK or value >= 1 << 48:
            continue
        valid.append({**c, "raw": value})
    if not valid:
        return None, "entry_invalid"
    named = [c for c in valid if c["named"]]
    group = named or valid
    chosen = min(group, key=lambda c: c["id"])
    for c in group:
        if c["id"] == spec["preferred"]:
            chosen = c
            break
    chosen_bytes = chosen["raw"] * chosen["unit"]
    if chosen_bytes >= _U64:
        return None, "overflow"
    for c in group:
        if c["id"] == chosen["id"]:
            continue
        other = c["raw"] * c["unit"]
        high, low = max(other, chosen_bytes), min(other, chosen_bytes)
        if other >= _U64 or (high - low) > 0.01 * high:
            return None, "candidates_disagree"
    poh = _power_on_hours(smart)
    if poh is None or poh < _MIN_POH_FOR_BOUND:
        return None, "power_on_hours_unknown"
    if chosen_bytes > poh * 3600 * _RATE_BOUND:
        return None, "rate_bound"
    return {"bytes": chosen_bytes, "source": "ata_attribute",
            "attribute_id": chosen["id"], "attribute_name": chosen["name"]}, ""


def _nvme_volume(smart: dict, key: str) -> tuple[dict | None, str]:
    log = smart.get("nvme_smart_health_information_log")
    if not isinstance(log, dict):
        return None, "no_source"
    value, status = _num_field(log, key)
    if status == _MISSING:
        return None, "no_source"
    if status == _INVALID:
        return None, "entry_invalid"
    if status == _OVERFLOW or value * NVME_DATA_UNIT >= _U64:
        return None, "overflow"
    return {"bytes": value * NVME_DATA_UNIT, "source": "nvme"}, ""


def _protocol(drive: dict) -> str:
    device = (drive.get("smart_data") or {}).get("device")
    if isinstance(device, dict) and device.get("protocol"):
        return str(device["protocol"])
    return str(drive.get("protocol") or "")


def derive_volumes(drive: dict, ds: dict) -> dict:
    """The derived field of /api/drives/{id}, computed the way the agent does.

    NVMe: data units x 512000. ATA with Device Statistics present: Logical
    Sectors Written / Read x logical block size. ATA with the log absent or
    off: an allowlisted vendor attribute. ATA with it unavailable: omitted.
    Anything else: omitted, no source.
    """
    smart = drive.get("smart_data") or {}
    protocol = _protocol(drive).upper()
    derived: dict[str, Any] = {}
    for key, spec in _SPECS.items():
        if protocol == "ATA":
            status = ds.get("status")
            if status == "present":
                volume, reason = _devstat_volume(smart, ds, spec)
            elif status == "unavailable":
                volume, reason = None, "device_statistics_unavailable"
            elif status in ("absent", "off"):
                volume, reason = _vendor_volume(smart, spec)
            else:
                volume, reason = None, "no_source"
        elif protocol == "NVME":
            volume, reason = _nvme_volume(smart, spec["nvme"])
        else:
            volume, reason = None, "no_source"
        if volume is not None:
            derived[key] = volume
        else:
            derived[f"{key}_omitted"] = reason
    return derived


# ── Pool payload (the agent's PoolInfo, zpool_status.go) ─────────────────────


def _mirror_state(devices: list[dict]) -> str:
    states = [d.get("state") for d in devices]
    if all(s == "ONLINE" for s in states):
        return "ONLINE"
    if all(s != "ONLINE" for s in states):
        return "UNAVAIL"
    return "DEGRADED"


def _pool_state_from_devices(devices: list[dict]) -> str:
    return {"ONLINE": "ONLINE", "DEGRADED": "DEGRADED"}.get(_mirror_state(devices), "FAULTED")


def pool_payload(rec: dict) -> dict:
    """One pool as GET /api/pools lists it."""
    devices = rec.get("devices") or []
    vdev = rec.get("vdev") or {"name": "mirror-0", "type": "mirror"}
    problems: list[dict] = []
    vdev_state = _mirror_state(devices)
    if vdev_state != "ONLINE":
        problems.append({"name": vdev["name"], "type": vdev["type"], "state": vdev_state,
                         "read_errors": 0, "write_errors": 0, "checksum_errors": 0})
    for d in devices:
        if d["state"] != "ONLINE" or d["read"] or d["write"] or d["cksum"]:
            problems.append({"name": d["name"], "type": "disk", "state": d["state"],
                             "read_errors": d["read"], "write_errors": d["write"],
                             "checksum_errors": d["cksum"]})
    return {
        "name": rec["name"],
        "state": rec["state"],
        "status": rec.get("status"),
        "action": rec.get("action"),
        "read_errors": sum(d["read"] for d in devices),
        "write_errors": sum(d["write"] for d in devices),
        "checksum_errors": sum(d["cksum"] for d in devices),
        "data_errors": rec.get("data_errors"),
        "scan_function": rec.get("scan_function"),
        "scan_state": rec.get("scan_state"),
        "scrub_in_progress": bool(rec.get("scrub_in_progress")),
        "last_scrub_end": rec.get("last_scrub_end"),
        "last_scrub_repaired": rec.get("last_scrub_repaired"),
        "last_scrub_errors": rec.get("last_scrub_errors"),
        "problem_vdevs": problems,
    }


# ── Request validation helpers ──────────────────────────────────────────────


class BadRequest(Exception):
    """A request the mock refuses, with the HTTP status to answer."""

    def __init__(self, error: str, code: int = 400, **extra: Any) -> None:
        super().__init__(error)
        self.code = code
        self.extra = extra


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BadRequest(f"{what} must be a non-negative integer")
    return value


def _opt_count(value: Any, what: str) -> int | None:
    return None if value is None else _count(value, what)


def _opt_text(value: Any, what: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise BadRequest(f"{what} must be a string or null")
    return value


def _check_keys(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise BadRequest(f"unknown keys: {', '.join(unknown)}", allowed=sorted(allowed))


def _parse_errors_line(value: Any) -> int | None:
    """The pool's "errors:" line as data_errors: an int, the zpool text, or null."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("no known data errors"):
            return 0
        match = re.match(r"^(\d+)\s+data errors?", text)
        if match:
            return int(match.group(1))
        raise BadRequest('errors must be "No known data errors", "N data errors, ...", a count or null')
    return _count(value, "errors")


# ── Replica files ────────────────────────────────────────────────────────────
# Every fake drive and pool comes from a replica file: one JSON document per
# replica, in the shape the agent serves (meta, smart, devstat for a drive;
# pool for a pool), with a name, a kind and a description. The files live in
# read-only folders (--replicas, shipped with the app) and one read-write
# folder (--user-replicas, where uploads and saved drives go). See
# docs/mock-agent.md for the format.

REPLICA_FORMAT = "smart-sniffer-replica"
REPLICA_VERSION = 1
REPLICA_KINDS = ("ata", "ata_devstat", "nvme", "scsi", "unsupported", "zfs_pool")
REPLICA_ORIGINS = ("shipped", "saved", "uploaded")
MAX_REPLICA_BYTES = 1024 * 1024          # 1 MB per file
SCENARIOS_FILE = "scenarios.json"

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPLICA_DIRS = [os.path.join(_HERE, "replicas"), os.path.join(_HERE, "replicas", "extra")]

# What the old preset keys were called, for the "presets" list in /api/state.
_PRESET_LABELS = {
    "sata_hdd":         "SATA HDD (Seagate Barracuda 2TB)",
    "sata_hdd_devstat": "SATA HDD with Device Statistics (WDC Ultrastar 14TB)",
    "sata_ssd":         "SATA SSD (Samsung 870 EVO 500GB)",
    "nvme":             "NVMe SSD (Samsung 980 PRO 1TB)",
    "nvme_usb":         "NVMe USB-C Enclosure (Sabrent)",
    "usb_blocked":      "USB External, SMART Blocked (WD Elements)",
    "virtual_disk":     "Virtual Disk (QEMU VirtIO)",
    "sas_enterprise":   "Enterprise SAS (Seagate Exos 10E2400)",
    "zfs_pool":         "ZFS pool (two-disk mirror)",
}

# Device Statistics of old presets that have no file in the loaded folders,
# for drives saved by an older mock. Everything else falls back by protocol.
_LEGACY_DEVSTAT = {"usb_blocked": {"status": "unavailable", "reason": "failed"}}

_POOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_FILE_SLUG_RE = re.compile(r"[^a-z0-9]+")


class InvalidReplica(ValueError):
    """A document that is not a valid replica file."""


def _file_slug(text: str) -> str:
    slug = _FILE_SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:80].strip("-") or "replica"


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise InvalidReplica(message)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_scenarios(options: Any) -> None:
    _need(isinstance(options, list), "scenario.options must be a list")
    for opt in options:
        _need(isinstance(opt, dict) and isinstance(opt.get("id"), str) and opt["id"]
              and isinstance(opt.get("label"), str) and isinstance(opt.get("steps"), list),
              "each scenario option needs an id, a label and a list of steps")
        for step in opt["steps"]:
            _need(isinstance(step, dict) and (
                (isinstance(step.get("patch"), str) and isinstance(step.get("body", {}), dict))
                or step.get("post") in ("vanish", "restore")),
                f"scenario {opt['id']}: each step is a patch with a body, or a post of vanish or restore")


def validate_replica(doc: Any) -> dict:
    """Check a replica document and return it. Raises InvalidReplica."""
    _need(isinstance(doc, dict), "not a replica file: the top level must be a JSON object")
    _need(doc.get("format") == REPLICA_FORMAT, f'not a replica file: format must be "{REPLICA_FORMAT}"')
    _need(doc.get("version") == REPLICA_VERSION, f"unsupported replica version: {doc.get('version')!r}")
    _need(isinstance(doc.get("name"), str) and doc["name"].strip() != "", "name must be a non-empty string")
    _need(doc.get("kind") in REPLICA_KINDS, f"kind must be one of {', '.join(REPLICA_KINDS)}")
    _need(isinstance(doc.get("description"), str), "description must be a string")
    origin = doc.get("origin")
    _need(isinstance(origin, dict) and origin.get("type") in REPLICA_ORIGINS,
          f"origin.type must be one of {', '.join(REPLICA_ORIGINS)}")
    _need(doc.get("order") is None or _is_count(doc["order"]), "order must be a whole number")
    _need(doc.get("preset") is None or isinstance(doc["preset"], str), "preset must be a string")
    _need(doc.get("verdict") is None or isinstance(doc["verdict"], dict), "verdict must be an object")
    scenario = doc.get("scenario")
    if scenario is not None:
        _need(isinstance(scenario, dict), "scenario must be an object")
        _need(scenario.get("current") is None or isinstance(scenario["current"], str),
              "scenario.current must be a string")
        if "options" in scenario:
            _check_scenarios(scenario["options"])
    if doc["kind"] == "zfs_pool":
        pool = doc.get("pool")
        _need(isinstance(pool, dict), "a zfs_pool replica needs a pool object")
        _need(isinstance(pool.get("name"), str) and bool(_POOL_NAME_RE.match(pool["name"])),
              "pool.name must be a valid pool name")
        _need(isinstance(pool.get("state"), str) and pool["state"].strip() != "", "pool.state must be a string")
        vdev = pool.get("vdev")
        _need(isinstance(vdev, dict) and isinstance(vdev.get("name"), str) and isinstance(vdev.get("type"), str),
              "pool.vdev needs a name and a type")
        devices = pool.get("devices")
        _need(isinstance(devices, list) and devices != [], "pool.devices must be a list of disks")
        names = set()
        for dev in devices:
            _need(isinstance(dev, dict) and isinstance(dev.get("name"), str) and dev["name"] != "",
                  "each pool device needs a name")
            _need(dev["name"] not in names, f"pool device {dev['name']} is listed twice")
            names.add(dev["name"])
            _need(dev.get("state") in _DEVICE_STATES, f"pool device state must be one of {', '.join(_DEVICE_STATES)}")
            _need(all(_is_count(dev.get(k)) for k in ("read", "write", "cksum")),
                  "pool device read, write and cksum must be whole numbers")
    else:
        meta = doc.get("meta")
        _need(isinstance(meta, dict), "a drive replica needs a meta object")
        for key in ("model", "serial", "protocol", "device_path"):
            _need(isinstance(meta.get(key), str), f"meta.{key} must be a string")
        _need(meta["serial"].strip() != "", "meta.serial must not be empty")
        _need(isinstance(doc.get("smart"), dict), "smart must be an object")
        devstat = doc.get("devstat")
        _need(isinstance(devstat, dict) and devstat.get("status") in DEVSTAT_STATUSES,
              f"devstat.status must be one of {', '.join(DEVSTAT_STATUSES)}")
        if devstat["status"] == "present":
            _need(isinstance(devstat.get("pages"), list), "devstat with status present needs pages")
    return doc


def _replica_entry(rec: dict) -> dict:
    """One file as GET /api/replicas lists it."""
    if rec.get("doc") is None:
        return {"id": rec["id"], "source": rec["source"], "valid": False, "error": rec["error"]}
    doc = rec["doc"]
    return {"id": rec["id"], "name": doc["name"], "kind": doc["kind"], "description": doc["description"],
            "source": rec["source"], "order": doc.get("order") if rec["source"] == "shipped" else None,
            "valid": True}


def drive_kind(smart: dict, devstat: dict, protocol: str) -> str:
    """The replica kind a drive's data calls for; unsupported when it has no SMART data."""
    if isinstance(smart.get("nvme_smart_health_information_log"), dict):
        return "nvme"
    if _attr_table(smart):
        return "ata_devstat" if devstat.get("status") == "present" else "ata"
    scsi = any(k in smart for k in ("scsi_grown_defect_list", "scsi_error_counter_log"))
    if scsi or (protocol.upper() == "SCSI" and smart.get("smart_status")):
        return "scsi"
    return "unsupported"


class ReplicaLibrary:
    """The replica files on disk, re-read when they change (by mtime)."""

    def __init__(self, shipped_dirs: list[str], user_dir: str | None) -> None:
        self.lock = threading.Lock()
        self.shipped_dirs = list(shipped_dirs)
        self.user_dir = user_dir
        self._cache: dict[str, tuple[int, int, dict]] = {}
        self.files: list[dict] = []          # {"id", "source", "path", "doc" or None, "error"}
        self.scenarios: dict[str, list] = {}
        self._scenarios_stamp: tuple | None = None
        self.refresh()

    # ── Reading ──

    def _read(self, path: str, source: str, stem: str) -> dict:
        st = os.stat(path)
        cached = self._cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        rec: dict[str, Any] = {"id": f"{source}.{stem}", "source": source, "path": path,
                               "doc": None, "error": None}
        try:
            if st.st_size > MAX_REPLICA_BYTES:
                raise InvalidReplica("larger than 1 MB")
            with open(path, encoding="utf-8") as f:
                try:
                    doc = json.load(f)
                except ValueError:
                    raise InvalidReplica("not valid JSON") from None
            rec["doc"] = validate_replica(doc)
        except InvalidReplica as err:
            rec["error"] = str(err)
        except OSError as err:
            rec["error"] = f"cannot read the file: {err.strerror or err}"
        self._cache[path] = (st.st_mtime_ns, st.st_size, rec)
        return rec

    def _scan(self, folder: str, source: str) -> list[dict]:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return []
        out = []
        for name in names:
            path = os.path.join(folder, name)
            if not name.endswith(".json") or name == SCENARIOS_FILE or name.startswith(".") \
                    or not os.path.isfile(path):
                continue
            out.append(self._read(path, source, name[:-len(".json")]))
        return out

    def _load_scenarios(self) -> None:
        candidates = [os.path.join(d, SCENARIOS_FILE) for d in self.shipped_dirs]
        candidates.append(os.path.join(DEFAULT_REPLICA_DIRS[0], SCENARIOS_FILE))
        for path in candidates:
            try:
                st = os.stat(path)
            except OSError:
                continue
            stamp = (path, st.st_mtime_ns, st.st_size)
            if stamp == self._scenarios_stamp:
                return
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise InvalidReplica("the top level must be an object of kinds")
                for kind, options in data.items():
                    _need(kind in REPLICA_KINDS, f"unknown kind {kind}")
                    _check_scenarios(options)
            except (OSError, ValueError) as err:
                print(f"[mock] Ignoring {path}: {err}")
                continue
            self.scenarios = data
            self._scenarios_stamp = stamp
            return
        self.scenarios, self._scenarios_stamp = {}, None

    def refresh(self) -> None:
        """List the folders again and parse any file that changed."""
        with self.lock:
            files: list[dict] = []
            seen: set[str] = set()
            for folder in self.shipped_dirs:
                for rec in self._scan(folder, "shipped"):
                    if rec["id"] in seen:
                        rec = {**rec, "doc": None, "error": f"another folder already has {rec['id']}"}
                    seen.add(rec["id"])
                    files.append(rec)
            if self.user_dir:
                files.extend(self._scan(self.user_dir, "user"))

            def sort_key(rec: dict) -> tuple:
                doc = rec.get("doc") or {}
                order = doc.get("order") if rec["source"] == "shipped" else None
                return (rec["source"] != "shipped", order is None, order or 0,
                        str(doc.get("name", "")).lower(), rec["id"])

            self.files = sorted(files, key=sort_key)
            live = {rec["path"] for rec in files}
            self._cache = {p: v for p, v in self._cache.items() if p in live}
            self._load_scenarios()

    def folder_status(self) -> dict:
        path = self.user_dir
        if not path or not os.path.isdir(path):
            return {"path": path, "status": "missing"}
        probe = os.path.join(path, f".write-test-{uuid.uuid4().hex[:8]}")
        try:
            with open(probe, "w") as f:
                f.write("")
            os.remove(probe)
        except OSError:
            return {"path": path, "status": "read_only"}
        return {"path": path, "status": "ok"}

    def listing(self) -> dict:
        self.refresh()
        with self.lock:
            files = [_replica_entry(rec) for rec in self.files]
        return {"agent": "ok", "folder": self.folder_status(), "files": files}

    def find(self, file_id: str) -> dict | None:
        """The file record for an id (re-reading the folders once if unknown)."""
        for attempt in (0, 1):
            with self.lock:
                for rec in self.files:
                    if rec["id"] == file_id:
                        return rec
            if attempt == 0:
                self.refresh()
        return None

    def by_preset(self, key: str) -> dict | None:
        """The first valid file that names this old preset key."""
        with self.lock:
            for rec in self.files:
                if rec.get("doc") and rec["doc"].get("preset") == key:
                    return rec
        return None

    def preset_keys(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        with self.lock:
            for rec in self.files:
                doc = rec.get("doc")
                if doc and doc.get("preset") and doc["preset"] not in out:
                    out[doc["preset"]] = {
                        "label": _PRESET_LABELS.get(doc["preset"], doc["name"]),
                        "kind": "pool" if doc["kind"] == "zfs_pool" else "drive",
                    }
        return out

    def scenario_options(self, kind: str, extra: list | None) -> list[dict]:
        """Scenarios for a kind: the file's own first, then the library's."""
        out, seen = [], set()
        with self.lock:
            library = list(self.scenarios.get(kind) or [])
        for opt in list(extra or []) + library:
            if opt["id"] not in seen:
                seen.add(opt["id"])
                out.append(opt)
        return out


# Replaced in build_server().
library = ReplicaLibrary([], None)


# ── Drive and pool store ─────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _make_slug(serial: str) -> str:
    slug = _SLUG_RE.sub("-", serial.lower()).strip("-")
    return slug or "unknown"


def _now_iso() -> str:
    return _rfc3339(time.time())


def _parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _natural_from_file(devstat: dict) -> dict:
    """The stored Device Statistics state from a file's devstat part."""
    natural = {k: copy.deepcopy(devstat[k]) for k in ("status", "reason") if k in devstat}
    if devstat.get("status") == "present":
        natural["complete"] = bool(devstat.get("complete", True))
        natural["logical_block_size"] = devstat.get("logical_block_size", 512)
        natural["pages"] = copy.deepcopy(devstat.get("pages") or [])
    return natural


def _replica_fields(rec: dict, file_rec: dict) -> None:
    """The private keys a live drive or pool keeps about the file it came from."""
    doc = file_rec["doc"]
    scenario = doc.get("scenario") or {}
    rec["_replica"] = file_rec["id"]
    rec["_scenario"] = scenario.get("current")
    rec["_kind"] = doc["kind"]
    rec["_name"] = doc["name"]
    rec["_description"] = doc["description"]
    rec["_origin"] = copy.deepcopy(doc["origin"])
    rec["_scenarios_extra"] = copy.deepcopy(scenario.get("options") or [])
    if doc.get("preset"):
        rec["_preset"] = doc["preset"]


def drive_from_file(file_rec: dict) -> dict:
    """A live drive record (no id yet) from a drive replica file."""
    doc = file_rec["doc"]
    meta = doc["meta"]
    drive: dict[str, Any] = {
        "id": "",
        "device_path": meta["device_path"],
        "model": meta["model"],
        "serial": meta["serial"],
        "protocol": meta["protocol"],
        "smart_data": copy.deepcopy(doc["smart"]),
        "_devstat": _natural_from_file(doc["devstat"]),
    }
    _replica_fields(drive, file_rec)
    return drive


def pool_from_file(file_rec: dict) -> dict:
    """A live pool record from a pool replica file.

    A shipped file's last scrub is read as an age: the time between the
    file's verdict.at and last_scrub_end is kept, counted back from now.
    """
    doc = file_rec["doc"]
    rec = copy.deepcopy(doc["pool"])
    rec.setdefault("status", None)
    rec.setdefault("action", None)
    if file_rec["source"] == "shipped":
        end = _parse_time(rec.get("last_scrub_end"))
        at = _parse_time((doc.get("verdict") or {}).get("at"))
        if end is not None and at is not None:
            shifted = datetime.now(timezone.utc).replace(microsecond=0) - (at - end)
            rec["last_scrub_end"] = shifted.strftime("%Y-%m-%dT%H:%M:%SZ")
    _replica_fields(rec, file_rec)
    rec["vanished"] = rec["_scenario"] == "vanished"
    return rec


def _resolve_preset(key: str) -> dict | None:
    """The first file naming an old preset key, as a live record with its
    healthy scenario applied (when its kind has one)."""
    file_rec = library.by_preset(key)
    if file_rec is None:
        return None
    is_pool = file_rec["doc"]["kind"] == "zfs_pool"
    rec = pool_from_file(file_rec) if is_pool else drive_from_file(file_rec)
    healthy = _find_scenario(rec, "healthy")
    if healthy is not None:
        rec = _run_scenario(rec, healthy, is_pool)
    return rec


def _fallback_devstat(drive: dict) -> dict:
    """Device Statistics state for a drive saved by an older mock."""
    preset = drive.get("_preset")
    resolved = _resolve_preset(preset) if isinstance(preset, str) else None
    if resolved is not None and "_devstat" in resolved:
        return resolved["_devstat"]
    if preset in _LEGACY_DEVSTAT:
        return copy.deepcopy(_LEGACY_DEVSTAT[preset])
    if (drive.get("protocol") or "").upper() != "ATA":
        return {"status": "not_applicable"}
    return {"status": "absent"}


def _upgrade_drive(drive: dict) -> None:
    """Bring a drive saved by an older mock up to its file's current shape.

    Only adds what is missing (attribute rows, NVMe log keys, top-level keys
    and the Device Statistics state); a value the user already set is never
    changed. The file is the first one naming the drive's old preset key.
    """
    if "_devstat" in drive or not isinstance(drive.get("_preset"), str):
        return
    resolved = _resolve_preset(drive["_preset"])
    if resolved is None or "smart_data" not in resolved:
        return
    smart, devstat = resolved["smart_data"], resolved["_devstat"]
    have = drive.setdefault("smart_data", {})
    for key, value in smart.items():
        if key == "ata_smart_attributes" and isinstance(have.get(key), dict):
            table = have[key].setdefault("table", [])
            ids = {a.get("id") for a in table if isinstance(a, dict)}
            table.extend(a for a in value.get("table", []) if a.get("id") not in ids)
        elif key == "nvme_smart_health_information_log" and isinstance(have.get(key), dict):
            for log_key, log_value in value.items():
                have[key].setdefault(log_key, log_value)
        elif key in ("serial_number", "model_name"):
            continue   # the saved drive keeps its own identity
        elif have:
            # An empty smart_data stays empty: that is the preset's point.
            have.setdefault(key, value)
    drive["_devstat"] = devstat


def _map_saved_record(rec: dict) -> None:
    """Give a drive or pool saved by an older mock its replica fields."""
    if "_replica" in rec:
        return
    file_rec = library.by_preset(rec["_preset"]) if isinstance(rec.get("_preset"), str) else None
    if file_rec is not None:
        _replica_fields(rec, file_rec)
        rec["_scenario"] = None     # its values may have been edited since
    else:
        rec["_replica"] = None
        rec["_scenario"] = None


def _record_kind(rec: dict) -> str:
    if rec.get("_kind") in REPLICA_KINDS:
        return rec["_kind"]
    if "devices" in rec and "smart_data" not in rec:
        return "zfs_pool"
    return drive_kind(rec.get("smart_data") or {}, _natural_devstat(rec), _protocol(rec))


def _find_scenario(rec: dict, scenario_id: str) -> dict | None:
    for opt in library.scenario_options(_record_kind(rec), rec.get("_scenarios_extra")):
        if opt["id"] == scenario_id:
            return opt
    return None


def _scenario_block(rec: dict) -> dict:
    options = library.scenario_options(_record_kind(rec), rec.get("_scenarios_extra"))
    return {"current": rec.get("_scenario"), "options": [{"id": o["id"], "label": o["label"]} for o in options]}


def _replica_lab(rec: dict) -> dict:
    """The lab fields every live drive and pool carries in /api/state."""
    default_name = f"ZFS pool {rec['name']}" if "devices" in rec and "smart_data" not in rec else rec.get("model")
    return {
        "replica": rec.get("_replica"),
        "name": rec.get("_name") or default_name,
        "kind": _record_kind(rec),
        "description": rec.get("_description") or "",
        "scenario": _scenario_block(rec),
    }


# ── Edits (the PATCH routes and the scenario steps share these) ─────────────


def _apply_smart(drive: dict, updates: dict[str, Any]) -> None:
    """Targeted updates to a drive's smart_data.

    ATA: {"Reallocated_Sector_Ct": 5, ...} sets an attribute's raw value by
    name. NVMe: {"critical_warning": 1, ...} sets a health log key that
    exists. "smart_passed": bool sets smart_status.passed.
    """
    updates = dict(updates)
    smart = drive["smart_data"]
    if "smart_passed" in updates:
        smart.setdefault("smart_status", {})["passed"] = updates.pop("smart_passed")
    nvme_log = smart.get("nvme_smart_health_information_log")
    if nvme_log is not None:
        for key, val in updates.items():
            if key in nvme_log:
                nvme_log[key] = val
    for attr in _attr_table(smart):
        if attr.get("name") in updates:
            value = updates[attr["name"]]
            attr["raw"] = {**(attr.get("raw") or {}), "value": value, "string": str(value)}


def _apply_devstat(drive: dict, body: dict[str, Any]) -> None:
    """PATCH /api/drives/{id}/devstat on one drive record. Validates first."""
    _check_keys(body, set(DEVSTAT_COUNTS) | {"status", "reason", "complete"})
    natural = _natural_devstat(drive)
    has_pages = isinstance(natural.get("pages"), list)

    override = None
    clear_override = False
    if "status" in body:
        status = body["status"]
        reason = body.get("reason")
        if status in (None, "default"):
            clear_override = True
        elif status not in DEVSTAT_STATUSES:
            raise BadRequest(f"status must be one of {', '.join(DEVSTAT_STATUSES)} or default")
        elif status == "present" and not has_pages:
            raise BadRequest("this drive has no Device Statistics pages to present", 409)
        elif status in DEVSTAT_REASONS:
            reasons = DEVSTAT_REASONS[status]
            reason = reasons[0] if reason is None else reason
            if reason not in reasons:
                raise BadRequest(f"reason for {status} must be one of {', '.join(reasons)}")
            override = {"status": status, "reason": reason}
        elif reason is not None:
            raise BadRequest(f"status {status} takes no reason")
        else:
            override = {"status": status}
    elif "reason" in body:
        raise BadRequest("reason needs a status")
    counts = {k: _count(body[k], k) for k in DEVSTAT_COUNTS if k in body}
    if counts and not has_pages:
        raise BadRequest("this drive has no Device Statistics pages to edit", 409)
    if "complete" in body and not isinstance(body["complete"], bool):
        raise BadRequest("complete must be true or false")

    if clear_override:
        drive.pop("_devstat_override", None)
    elif override is not None:
        same = (override["status"] == natural.get("status")
                and override.get("reason") == natural.get("reason"))
        if same:
            drive.pop("_devstat_override", None)
        else:
            drive["_devstat_override"] = override
    for key, value in counts.items():
        page, offset, name, size = DEVSTAT_COUNTS[key]
        _set_entry(natural["pages"], page, offset, name, size, value)
    if "complete" in body:
        natural["complete"] = body["complete"]


def _apply_derived(drive: dict, body: dict[str, Any]) -> None:
    """PATCH /api/drives/{id}/derived on one drive record: Data Written / Read in TB.

    Writes through to whatever the volume is computed from (the NVMe data
    units, Device Statistics Logical Sectors, or the vendor attribute), so
    the source stays what it was and every view of the drive agrees.
    """
    allowed = {"host_writes_tb", "host_reads_tb", "host_writes_bytes", "host_reads_bytes"}
    _check_keys(body, allowed)
    wanted: dict[str, int] = {}
    for key in ("host_writes", "host_reads"):
        for suffix, scale in (("_tb", 10 ** 12), ("_bytes", 1)):
            raw = body.get(key + suffix)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                    or not math.isfinite(raw) or raw < 0:
                raise BadRequest(f"{key}{suffix} must be a non-negative number")
            wanted[key] = int(round(raw * scale))
    if not wanted:
        raise BadRequest("give host_writes_tb or host_reads_tb")
    ds = devstat_block(drive)
    current = derive_volumes(drive, ds)
    for key in wanted:
        if key not in current:
            raise BadRequest(f"this drive has no source for {key}", 409,
                             omitted=current.get(f"{key}_omitted"))
    smart = drive["smart_data"]
    for key, target in wanted.items():
        volume = current[key]
        spec = _SPECS[key]
        if volume["source"] == "nvme":
            log = smart["nvme_smart_health_information_log"]
            log.pop(spec["nvme"] + "_s", None)
            log[spec["nvme"]] = int(round(target / NVME_DATA_UNIT))
        elif volume["source"] == "ata_device_statistics":
            natural = _natural_devstat(drive)
            lbs = ds.get("logical_block_size") or 512
            entry = _find_entry(natural["pages"], 1, spec["offset"])
            entry.pop("value_s", None)
            entry["value"] = int(round(target / lbs))
        else:
            unit = spec["named"].get(volume["attribute_name"], 512)
            for attr in _attr_table(smart):
                if attr.get("id") == volume["attribute_id"]:
                    raw = int(round(target / unit))
                    attr["raw"] = {"value": raw, "string": str(raw)}


def _apply_pool(rec: dict, body: dict[str, Any]) -> dict:
    """PATCH /api/pools/{name}: the changed pool record. Validates the whole body first."""
    scrub_keys = {"scan_function", "scan_state", "last_scrub_end"}
    scrub_counts = {"last_scrub_repaired", "last_scrub_errors"}
    _check_keys(body, {"state", "devices", "errors", "data_errors", "status", "action",
                       "scrub_in_progress"} | scrub_keys | scrub_counts)
    new = copy.deepcopy(rec)
    before = new["state"]

    if "devices" in body:
        if not isinstance(body["devices"], list):
            raise BadRequest("devices must be a list")
        by_name = {d["name"]: d for d in new["devices"]}
        for upd in body["devices"]:
            if not isinstance(upd, dict) or upd.get("name") not in by_name:
                raise BadRequest(f"devices: unknown device; this pool has {', '.join(by_name)}")
            _check_keys(upd, {"name", "state", "read", "write", "cksum",
                              "read_errors", "write_errors", "checksum_errors"})
            dev = by_name[upd["name"]]
            if "state" in upd:
                if upd["state"] not in _DEVICE_STATES:
                    raise BadRequest(f"device state must be one of {', '.join(_DEVICE_STATES)}")
                dev["state"] = upd["state"]
            for short, long in (("read", "read_errors"), ("write", "write_errors"),
                                ("cksum", "checksum_errors")):
                for key in (short, long):
                    if key in upd:
                        dev[short] = _count(upd[key], key)

    if "state" in body:
        state = body["state"]
        if not isinstance(state, str) or not state.strip():
            raise BadRequest("state must be a pool state such as ONLINE or DEGRADED")
        state = state.strip().upper()
        if state == "MISSING":
            raise BadRequest("MISSING is decided by the integration; use POST /api/pools/{name}/vanish")
        new["state"] = state
    elif "devices" in body:
        new["state"] = _pool_state_from_devices(new["devices"])

    if "status" in body or "action" in body:
        if "status" in body:
            new["status"] = _opt_text(body["status"], "status")
        if "action" in body:
            new["action"] = _opt_text(body["action"], "action")
    elif new["state"] != before:
        faulted = new["state"] != "ONLINE"
        new["status"] = _FAULTED_STATUS if faulted else None
        new["action"] = _FAULTED_ACTION if faulted else None

    if "errors" in body:
        new["data_errors"] = _parse_errors_line(body["errors"])
    if "data_errors" in body:
        new["data_errors"] = _opt_count(body["data_errors"], "data_errors")
    for key in scrub_keys:
        if key in body:
            new[key] = _opt_text(body[key], key)
    for key in scrub_counts:
        if key in body:
            new[key] = _opt_count(body[key], key)
    if "scrub_in_progress" in body:
        if not isinstance(body["scrub_in_progress"], bool):
            raise BadRequest("scrub_in_progress must be true or false")
        new["scrub_in_progress"] = body["scrub_in_progress"]
    return new


def _expand_devices(body: dict, rec: dict) -> dict:
    """Scenario bodies may name pool disks as "*" (every disk) or "#N" (the Nth, from 0)."""
    if not isinstance(body.get("devices"), list):
        return body
    names = [d["name"] for d in rec.get("devices") or []]
    out = []
    for upd in body["devices"]:
        name = upd.get("name") if isinstance(upd, dict) else None
        if name == "*":
            targets = names
        elif isinstance(name, str) and re.fullmatch(r"#\d+", name):
            index = int(name[1:])
            if index >= len(names):
                raise BadRequest(f"this pool has no disk {name}")
            targets = [names[index]]
        else:
            out.append(upd)
            continue
        out.extend({**upd, "name": target} for target in targets)
    return {**body, "devices": out}


def _run_scenario(rec: dict, scenario: dict, is_pool: bool) -> dict:
    """A copy of rec with the scenario's steps applied in order."""
    work = copy.deepcopy(rec)
    steps = scenario.get("steps") or []
    if is_pool and not any(step.get("post") == "vanish" for step in steps):
        work["vanished"] = False
    for step in steps:
        if "post" in step:
            if not is_pool or step["post"] not in ("vanish", "restore"):
                raise BadRequest(f"scenario {scenario['id']}: cannot post {step['post']} here", 422)
            work["vanished"] = step["post"] == "vanish"
            continue
        target, body = step.get("patch"), step.get("body") or {}
        if is_pool and target == "pool":
            work = _apply_pool(work, _expand_devices(body, work))
        elif not is_pool and target == "smart":
            _apply_smart(work, body)
        elif not is_pool and target == "devstat":
            _apply_devstat(work, body)
        elif not is_pool and target == "derived":
            _apply_derived(work, body)
        else:
            raise BadRequest(f"scenario {scenario['id']}: cannot patch {target} here", 422)
    work["_scenario"] = scenario["id"]
    return work


class DriveStore:
    """Thread-safe store of fake drives and pools with optional disk persistence."""

    def __init__(self, persist_path: str | None = None) -> None:
        self.lock = threading.Lock()
        self.drives: dict[str, dict[str, Any]] = {}   # keyed by slug id
        self.order: list[str] = []
        self.pools: dict[str, dict[str, Any]] = {}    # keyed by pool name
        self.pool_order: list[str] = []
        self._poll_count = 0
        self._last_poll: float | None = None
        self._persist_path = persist_path

    def record_poll(self) -> None:
        with self.lock:
            self._poll_count += 1
            self._last_poll = time.time()

    @property
    def poll_info(self) -> dict:
        with self.lock:
            return {
                "count": self._poll_count,
                "last": self._last_poll,
            }

    # ── Persistence ──

    def _save(self) -> None:
        """Persist drives and pools to disk. Must be called while holding self.lock."""
        if not self._persist_path:
            return
        try:
            data = {"order": self.order, "drives": self.drives,
                    "pool_order": self.pool_order, "pools": self.pools}
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._persist_path)
        except Exception as e:
            print(f"[mock] Failed to save drives: {e}")

    def load(self) -> bool:
        """Load drives and pools from disk. Returns True if anything was loaded."""
        if not self._persist_path or not os.path.exists(self._persist_path):
            return False
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            with self.lock:
                self.drives = data.get("drives", {})
                self.order = data.get("order", [])
                self.pools = data.get("pools", {})
                self.pool_order = data.get("pool_order", list(self.pools))
                for drive in self.drives.values():
                    _upgrade_drive(drive)
                    _map_saved_record(drive)
                for pool in self.pools.values():
                    _map_saved_record(pool)
            print(f"[mock] Loaded {len(self.drives)} drives and {len(self.pools)} pools "
                  f"from {self._persist_path}")
            return len(self.drives) > 0 or len(self.pools) > 0
        except Exception as e:
            print(f"[mock] Failed to load drives: {e}")
            return False

    # ── Adding ──

    def _insert_drive(self, drive: dict) -> str:
        """Store a new drive. A serial already live gets - and 4 hex digits appended."""
        with self.lock:
            serial = base = drive["serial"]
            live = {d["serial"] for d in self.drives.values()}
            while serial in live or _make_slug(serial) in self.drives:
                serial = f"{base}-{uuid.uuid4().hex[:4].upper()}"
            smart = drive["smart_data"]
            if serial != base and smart.get("serial_number") == base:
                smart["serial_number"] = serial
            drive["serial"] = serial
            drive["id"] = _make_slug(serial)
            self.drives[drive["id"]] = drive
            self.order.append(drive["id"])
            self._save()
            return drive["id"]

    def _insert_pool(self, rec: dict, name: str | None = None) -> str:
        if name is not None:
            if not isinstance(name, str) or not _POOL_NAME_RE.match(name):
                raise BadRequest("name must be a valid pool name")
        with self.lock:
            if name is None:
                base, n = rec["name"], 1
                name = base
                while name in self.pools:
                    n += 1
                    name = f"{base}{n}"
            elif name in self.pools:
                raise BadRequest(f"pool {name} already exists", 409)
            rec["name"] = name
            self.pools[name] = rec
            self.pool_order.append(name)
            self._save()
        return name

    def add_replica(self, file_id: str, name: str | None = None) -> dict:
        """Add a live drive or pool from a replica file. Returns the add response."""
        file_rec = library.find(file_id)
        if file_rec is None:
            raise BadRequest("not_found", 404, message=f"no replica file {file_id}")
        if file_rec.get("doc") is None:
            raise BadRequest("invalid_replica", 422, message=file_rec["error"])
        if file_rec["doc"]["kind"] == "zfs_pool":
            pool = self._insert_pool(pool_from_file(file_rec), name)
            return {"id": pool, "name": pool, "kind": "pool", "replica": file_id}
        return {"id": self._insert_drive(drive_from_file(file_rec)), "kind": "drive", "replica": file_id}

    def add_preset(self, preset_key: str, name: str | None = None) -> dict | None:
        """Add the healthy version of the first file naming an old preset key."""
        rec = _resolve_preset(preset_key)
        if rec is None:
            return None
        if "smart_data" not in rec:
            pool = self._insert_pool(rec, name)
            return {"id": pool, "name": pool, "kind": "pool", "replica": rec["_replica"]}
        return {"id": self._insert_drive(rec), "kind": "drive", "replica": rec["_replica"]}

    def add_drive(self, preset_key: str) -> str:
        """Add a drive by its old preset key (kept for older callers)."""
        added = self.add_preset(preset_key)
        if added is None or added["kind"] != "drive":
            raise BadRequest(f"unknown preset: {preset_key}")
        return added["id"]

    def add_pool(self, preset_key: str, name: str | None = None) -> str:
        """Add a pool by its old preset key (kept for older callers)."""
        added = self.add_preset(preset_key, name)
        if added is None or added["kind"] != "pool":
            raise BadRequest(f"unknown preset: {preset_key}")
        return added["id"]

    # ── Drives ──

    def remove_drive(self, drive_id: str) -> bool:
        with self.lock:
            if drive_id in self.drives:
                del self.drives[drive_id]
                self.order = [d for d in self.order if d != drive_id]
                self._save()
                return True
        return False

    def update_smart(self, drive_id: str, updates: dict[str, Any]) -> bool:
        """PATCH /api/drives/{id} and /smart (see _apply_smart)."""
        with self.lock:
            if drive_id not in self.drives:
                return False
            _apply_smart(self.drives[drive_id], updates)
            self._save()
            return True

    def update_devstat(self, drive_id: str, body: dict[str, Any]) -> dict:
        """PATCH /api/drives/{id}/devstat. Returns device_statistics and derived."""
        with self.lock:
            drive = self.drives.get(drive_id)
            if drive is None:
                raise BadRequest("drive not found", 404)
            _apply_devstat(drive, body)
            self._save()
            ds = devstat_block(drive)
            return {"ok": True, "device_statistics": ds, "derived": derive_volumes(drive, ds)}

    def update_derived(self, drive_id: str, body: dict[str, Any]) -> dict:
        """PATCH /api/drives/{id}/derived: set Data Written / Read in decimal TB."""
        with self.lock:
            drive = self.drives.get(drive_id)
            if drive is None:
                raise BadRequest("drive not found", 404)
            _apply_derived(drive, body)
            self._save()
            ds = devstat_block(drive)
            return {"ok": True, "derived": derive_volumes(drive, ds)}

    def _public_drive(self, d: dict) -> dict:
        """One drive as GET /api/drives/{id} returns it. Hold self.lock."""
        ds = devstat_block(d)
        return {
            "id": d["id"],
            "device_path": d["device_path"],
            "model": d["model"],
            "serial": d["serial"],
            "protocol": d["protocol"],
            "readable": True,
            "last_updated": _now_iso(),
            "smart_data": copy.deepcopy(d["smart_data"]),
            "device_statistics": ds,
            "derived": derive_volumes(d, ds),
        }

    def get_summaries(self) -> list[dict]:
        with self.lock:
            return [
                {"id": self.drives[d]["id"],
                 "device_path": self.drives[d]["device_path"],
                 "model": self.drives[d]["model"],
                 "serial": self.drives[d]["serial"],
                 "protocol": self.drives[d]["protocol"],
                 "readable": True}
                for d in self.order if d in self.drives
            ]

    def get_drive(self, drive_id: str) -> dict | None:
        with self.lock:
            d = self.drives.get(drive_id)
            return self._public_drive(d) if d else None

    def _lab_drive(self, d: dict) -> dict:
        """A drive for the control views: the public payload plus a lab block."""
        payload = self._public_drive(d)
        natural = _natural_devstat(d)
        override = d.get("_devstat_override")
        lab: dict[str, Any] = {
            "preset": d.get("_preset"),
            "devstat_default": {k: natural[k] for k in ("status", "reason") if k in natural},
            "devstat_override": copy.deepcopy(override) if isinstance(override, dict) else None,
            "devstat_counts": None,
        }
        pages = natural.get("pages")
        if isinstance(pages, list):
            lab["devstat_counts"] = {
                key: (_find_entry(pages, page, offset) or {}).get("value")
                for key, (page, offset, _, _) in DEVSTAT_COUNTS.items()
            }
        lab.update(_replica_lab(d))
        payload["lab"] = lab
        return payload

    def get_all(self) -> list[dict]:
        with self.lock:
            return [self._lab_drive(self.drives[d]) for d in self.order if d in self.drives]

    # ── Pools ──

    def remove_pool(self, name: str) -> bool:
        with self.lock:
            if name in self.pools:
                del self.pools[name]
                self.pool_order = [p for p in self.pool_order if p != name]
                self._save()
                return True
        return False

    def set_vanished(self, name: str, vanished: bool) -> dict:
        with self.lock:
            rec = self.pools.get(name)
            if rec is None:
                raise BadRequest("pool not found", 404)
            rec["vanished"] = vanished
            self._save()
            return {"ok": True, "pool": self._lab_pool(rec)}

    def update_pool(self, name: str, body: dict[str, Any]) -> dict:
        """PATCH /api/pools/{name}. Validates the whole body before applying it."""
        with self.lock:
            rec = self.pools.get(name)
            if rec is None:
                raise BadRequest("pool not found", 404)
            self.pools[name] = _apply_pool(rec, body)
            self._save()
            return {"ok": True, "pool": self._lab_pool(self.pools[name])}

    def _lab_pool(self, rec: dict) -> dict:
        """A pool for the control views: the served payload plus a lab block."""
        payload = pool_payload(rec)
        payload["lab"] = {
            "preset": rec.get("_preset"),
            "vanished": bool(rec.get("vanished")),
            "vdev": copy.deepcopy(rec.get("vdev")),
            "devices": copy.deepcopy(rec.get("devices")),
            **_replica_lab(rec),
        }
        return payload

    def pools_enabled(self) -> bool:
        with self.lock:
            return bool(self.pools)

    def served_pools(self) -> list[dict]:
        with self.lock:
            return [pool_payload(self.pools[p]) for p in self.pool_order
                    if p in self.pools and not self.pools[p].get("vanished")]

    def get_pool(self, name: str) -> dict | None:
        with self.lock:
            rec = self.pools.get(name)
            return self._lab_pool(rec) if rec else None

    def all_pools(self) -> list[dict]:
        with self.lock:
            return [self._lab_pool(self.pools[p]) for p in self.pool_order if p in self.pools]


# ── Global store (replaced in build_server()) ────────────────────────────────
store = DriveStore()

# ── Dashboard HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SMART Sniffer Mock Agent</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --text: #e4e4e7;
          --muted: #9ca3af; --accent: #3b82f6; --accent-hover: #2563eb;
          --danger: #ef4444; --danger-hover: #dc2626; --warn: #f59e0b;
          --success: #22c55e; --input-bg: #0f1117; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: var(--bg); color: var(--text); padding: 24px; max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }
  .status-bar { display: flex; gap: 20px; align-items: center; padding: 12px 16px;
                background: var(--card); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px; font-size: 0.85rem; }
  .status-bar .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot-green { background: var(--success); }
  .dot-gray { background: var(--muted); }
  .add-bar { display: flex; gap: 10px; margin-bottom: 20px; align-items: center; }
  select, input[type=number], input[type=text] {
    background: var(--input-bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 12px; font-size: 0.85rem; }
  select:focus, input:focus { outline: none; border-color: var(--accent); }
  button { cursor: pointer; border: none; border-radius: 6px; padding: 8px 16px; font-size: 0.85rem; font-weight: 500; transition: background 0.15s; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .btn-sm { padding: 4px 10px; font-size: 0.78rem; }
  .drive-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 14px; }
  .drive-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
  .drive-title { font-weight: 600; font-size: 0.95rem; }
  .drive-meta { color: var(--muted); font-size: 0.78rem; }
  .drive-extra { color: var(--muted); font-size: 0.78rem; margin-bottom: 10px; }
  .drive-extra strong { color: var(--text); font-weight: 500; }
  .drive-protocol { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.72rem;
                    font-weight: 600; text-transform: uppercase; margin-left: 8px; }
  .proto-ata { background: #1e3a5f; color: #60a5fa; }
  .proto-nvme { background: #1e3a2a; color: #4ade80; }
  .proto-scsi { background: #3a2a1e; color: #fb923c; }
  .proto-zfs { background: #2e1e3a; color: #c084fc; }
  .attrs-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .attr-row { display: flex; align-items: center; gap: 8px; }
  .attr-label { font-size: 0.78rem; color: var(--muted); min-width: 110px; flex-shrink: 0; }
  .attr-hint { font-size: 0.65rem; color: #6b7280; margin-left: 4px; }
  .attr-input { width: 80px; text-align: right; }
  .attr-row.critical .attr-label { color: var(--danger); }
  .attr-row.warning .attr-label { color: var(--warn); }
  .empty-state { text-align: center; padding: 48px; color: var(--muted); font-size: 0.9rem; }
  .drive-status { font-size: 0.78rem; padding: 3px 10px; border-radius: 12px; font-weight: 600; }
  .status-no { background: #14532d; color: #4ade80; }
  .status-maybe { background: #422006; color: #fbbf24; }
  .status-yes { background: #450a0a; color: #f87171; }
  .status-unsupported { background: #1f2937; color: #9ca3af; }
  .smart-toggle { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .smart-toggle label { font-size: 0.78rem; color: var(--muted); }
</style>
</head>
<body>

<h1>SMART Sniffer Mock Agent</h1>
<p class="subtitle">Fake smartha-agent for testing the HA integration. Change values and HA sees them on the next poll.</p>

<div class="status-bar">
  <div><span class="dot dot-green" id="server-dot"></span> Listening on port <strong id="port-display">9099</strong></div>
  <div>Auth: <strong id="auth-display">off</strong></div>
  <div>Drives: <strong id="drive-count">0</strong></div>
  <div>Pools: <strong id="pool-count">0</strong></div>
  <div>HA polls: <strong id="poll-count">0</strong></div>
  <div>Last poll: <span id="last-poll">-</span></div>
</div>

<div class="add-bar">
  <select id="replica-select">
    <!-- populated by JS: one option per replica file -->
  </select>
  <button class="btn-primary" onclick="addReplica()">+ Add</button>
</div>

<div id="drives-container">
  <div class="empty-state" id="empty-state">No drives configured. Add one above to get started.</div>
</div>

<script>
const REPLICAS = REPLICAS_JSON;

/* ── Attention thresholds (a rough copy of attention.py, for the badge only) ── */
const ATA_CRITICAL = new Set([
  "Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable",
  "Reported_Uncorrect",
]);
const ATA_WARNING = new Set([
  "Reallocated_Event_Count", "Spin_Retry_Count", "Command_Timeout",
]);

function attrClass(name) {
  if (ATA_CRITICAL.has(name)) return "critical";
  if (ATA_WARNING.has(name)) return "warning";
  return "";
}

function attrHint(name, protocol) {
  if (protocol === "NVMe") {
    const nvmeHints = {
      critical_warning: "≠0 → YES", media_errors: "≥1 → YES, ≥2^64 unknown",
      available_spare: "≤threshold → YES, <20 → MAYBE",
      percentage_used: "≥90 → MAYBE", temperature: "",
      power_on_hours: "", power_cycles: "",
      available_spare_threshold: "drive's min spare",
    };
    return nvmeHints[name] || "";
  }
  const hints = {
    Reallocated_Sector_Ct: "≥1 → YES", Current_Pending_Sector: "≥1 → YES",
    Offline_Uncorrectable: "≥1 → YES", Reported_Uncorrect: "≥1 → YES",
    Reallocated_Event_Count: "≥1 → MAYBE", Spin_Retry_Count: "≥1 → MAYBE",
    Command_Timeout: ">100 → MAYBE", Wear_Leveling_Count: "read from VALUE, not raw",
    Temperature_Celsius: "", Power_On_Hours: "", Power_Cycle_Count: "",
  };
  return hints[name] || "";
}

function devstatCount(drive, page, offset) {
  const ds = drive.device_statistics || {};
  for (const p of ds.pages || []) {
    if (p.number !== page) continue;
    for (const e of p.table || []) {
      if (e.offset === offset && (e.flags || {}).valid) return e.value || 0;
    }
  }
  return 0;
}

function predictState(drive) {
  const sd = drive.smart_data;
  if (!sd || (!sd.ata_smart_attributes && !sd.nvme_smart_health_information_log && !sd.smart_status)) {
    return "UNSUPPORTED";
  }
  const nvme = sd.nvme_smart_health_information_log;
  if (nvme) {
    if ((nvme.critical_warning || 0) !== 0) return "YES";
    // A count at or above 2^64 is unreadable, never a reason (attention.py).
    const media = nvme.media_errors || 0;
    if (media > 0 && media < 2 ** 64) return "YES";
    const spare = nvme.available_spare, thresh = nvme.available_spare_threshold;
    if (spare != null && thresh != null && spare <= thresh) return "YES";
    if (spare != null && spare < 20) return "MAYBE";
    if ((nvme.percentage_used || 0) >= 90) return "MAYBE";
    return "NO";
  }
  const table = (sd.ata_smart_attributes || {}).table || [];
  let hasCrit = false, hasWarn = false;
  for (const attr of table) {
    const raw = (attr.raw || {}).value || 0;
    if (raw <= 0) continue;
    if (ATA_CRITICAL.has(attr.name)) hasCrit = true;
    if (attr.name === "Command_Timeout") { if (raw > 100) hasWarn = true; }
    else if (ATA_WARNING.has(attr.name)) hasWarn = true;
  }
  // Gap-fill: no attribute 187, so Device Statistics page 4 counts.
  if (!table.some(a => a.id === 187) && devstatCount(drive, 4, 8) > 0) hasCrit = true;
  if (hasCrit) return "YES";
  if (hasWarn) return "MAYBE";
  return "NO";
}

function devstatText(ds) {
  if (!ds || !ds.status) return "not reported";
  const s = ds.status.replace("_", " ");
  return ds.reason ? `${s} (${ds.reason})` : s;
}

function volumeText(derived, key) {
  const v = (derived || {})[key];
  if (v) return `${(v.bytes / 1e12).toFixed(2)} TB (${v.attribute_name || v.source})`;
  const why = (derived || {})[key + "_omitted"];
  return why ? `omitted: ${why}` : "-";
}

/* ── Populate the replica dropdown (reload the page to see new files) ── */
const sel = document.getElementById("replica-select");
for (const r of REPLICAS) {
  const opt = document.createElement("option");
  opt.value = r.id;
  opt.textContent = r.name + "  (" + r.id + ")";
  sel.appendChild(opt);
}

/* ── API helpers ── */
async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch("/mock" + path, opts);
  return resp.json();
}

async function addReplica() {
  const replica = document.getElementById("replica-select").value;
  if (replica) await api("/drives", "POST", { replica });
  refresh();
}


async function removeDrive(id) {
  await api("/drives/" + id, "DELETE");
  refresh();
}

async function updateAttr(driveId, attrName, value) {
  await api("/drives/" + driveId, "PATCH", { [attrName]: value });
  refresh();
}

async function updateSmartPassed(driveId, passed) {
  await api("/drives/" + driveId, "PATCH", { smart_passed: passed });
  refresh();
}

async function updateDevstat(driveId, body) {
  await api("/drives/" + driveId + "/devstat", "PATCH", body);
  refresh();
}

async function updateWrites(driveId, tb) {
  await api("/drives/" + driveId + "/derived", "PATCH", { host_writes_tb: tb });
  refresh();
}

async function updatePool(name, body) {
  await api("/pools/" + name, "PATCH", body);
  refresh();
}

async function poolAction(name, action) {
  await api("/pools/" + name + "/" + action, "POST");
  refresh();
}

async function removePool(name) {
  await api("/pools/" + name, "DELETE");
  refresh();
}

/* ── Render ── */
function renderPool(pool) {
  const lab = pool.lab || {};
  const states = ["ONLINE", "DEGRADED", "FAULTED"];
  const stateOpts = states.concat(states.includes(pool.state) ? [] : [pool.state])
    .map(s => `<option value="${s}" ${s === pool.state ? "selected" : ""}>${s}</option>`).join("");
  const badge = lab.vanished ? "status-unsupported" : (pool.problem_vdevs.length || pool.state !== "ONLINE" ? "status-yes" : "status-no");
  let html = `<div class="drive-card">
    <div class="drive-header">
      <div>
        <span class="drive-title">${lab.name || "ZFS pool " + pool.name}</span>
        <span class="drive-protocol proto-zfs">ZFS</span>
        <span class="drive-status ${badge}">${lab.vanished ? "VANISHED" : pool.state}</span>
        <div class="drive-meta">${(lab.vdev || {}).name || ""} · ${(lab.devices || []).length} disks · last scrub ${pool.last_scrub_end || "never"}</div>
      </div>
      <div>
        <button class="btn-primary btn-sm" onclick="poolAction('${pool.name}','${lab.vanished ? "restore" : "vanish"}')">${lab.vanished ? "Restore" : "Vanish"}</button>
        <button class="btn-danger btn-sm" onclick="removePool('${pool.name}')">Remove</button>
      </div>
    </div>
    <div class="smart-toggle"><label>Pool State:</label>
      <select onchange="updatePool('${pool.name}', {state: this.value})" style="width:auto">${stateOpts}</select>
    </div>
    <div class="attrs-grid">`;
  for (const d of lab.devices || []) {
    const devOpts = ["ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"]
      .map(s => `<option value="${s}" ${s === d.state ? "selected" : ""}>${s}</option>`).join("");
    html += `<div class="attr-row ${d.state !== "ONLINE" ? "critical" : ""}">
      <span class="attr-label">${d.name}</span>
      <select onchange="updatePool('${pool.name}', {devices: [{name: '${d.name}', state: this.value}]})">${devOpts}</select></div>`;
    for (const [k, label] of [["read", "read errors"], ["write", "write errors"], ["cksum", "checksum errors"]]) {
      html += `<div class="attr-row ${d[k] > 0 ? "critical" : ""}">
        <span class="attr-label">${d.name} ${label}</span>
        <input type="number" class="attr-input" value="${d[k]}"
          onchange="updatePool('${pool.name}', {devices: [{name: '${d.name}', ${k}: parseInt(this.value)||0}]})"></div>`;
    }
  }
  html += `</div></div>`;
  return html;
}

async function refresh() {
  const data = await api("/state");
  const pools = data.pools || [];
  document.getElementById("drive-count").textContent = data.drives.length;
  document.getElementById("pool-count").textContent = pools.length;
  document.getElementById("poll-count").textContent = data.poll_count;
  document.getElementById("last-poll").textContent = data.last_poll
    ? new Date(data.last_poll * 1000).toLocaleTimeString() : "-";
  document.getElementById("port-display").textContent = data.port;
  document.getElementById("auth-display").textContent = data.auth ? "on" : "off";

  const container = document.getElementById("drives-container");
  const empty = document.getElementById("empty-state");

  if (data.drives.length === 0 && pools.length === 0) {
    container.innerHTML = "";
    container.appendChild(empty);
    empty.style.display = "block";
    return;
  }

  let html = "";
  for (const drive of data.drives) {
    const state = predictState(drive);
    const statusCls = { NO: "status-no", MAYBE: "status-maybe", YES: "status-yes", UNSUPPORTED: "status-unsupported" }[state];
    const protoCls = { ATA: "proto-ata", NVMe: "proto-nvme", SCSI: "proto-scsi" }[drive.protocol] || "proto-ata";
    const sd = drive.smart_data || {};
    const passed = (sd.smart_status || {}).passed;
    const lab = drive.lab || {};

    html += `<div class="drive-card">
      <div class="drive-header">
        <div>
          <span class="drive-title">${lab.name || drive.model}</span>
          <span class="drive-protocol ${protoCls}">${drive.protocol}</span>
          <span class="drive-status ${statusCls}">${state}</span>
          <div class="drive-meta">${drive.serial} · ${drive.device_path} · ${drive.id}</div>
        </div>
        <button class="btn-danger btn-sm" onclick="removeDrive('${drive.id}')">Remove</button>
      </div>
      <div class="drive-extra">${drive.model} · Device Statistics: <strong>${devstatText(drive.device_statistics)}</strong>
        · Data Written: <strong>${volumeText(drive.derived, "host_writes")}</strong>
        · Data Read: <strong>${volumeText(drive.derived, "host_reads")}</strong></div>`;

    // SMART passed toggle
    if (passed !== undefined) {
      html += `<div class="smart-toggle">
        <label>SMART Status:</label>
        <select onchange="updateSmartPassed('${drive.id}', this.value === 'true')" style="width:auto">
          <option value="true" ${passed ? "selected" : ""}>PASSED</option>
          <option value="false" ${!passed ? "selected" : ""}>FAILED</option>
        </select>
      </div>`;
    }

    html += `<div class="attrs-grid">`;

    // Device Statistics count and Data Written, where the drive has them
    const counts = lab.devstat_counts;
    if (counts) {
      const unc = counts.reported_uncorrectable ?? 0;
      html += `<div class="attr-row ${unc > 0 ? "critical" : ""}">
        <span class="attr-label">Devstat Reported Uncorrectable<span class="attr-hint"> ≥1 → YES</span></span>
        <input type="number" class="attr-input" value="${unc}"
          onchange="updateDevstat('${drive.id}', {reported_uncorrectable: parseInt(this.value)||0})">
      </div>`;
    }
    const writes = (drive.derived || {}).host_writes;
    if (writes) {
      html += `<div class="attr-row">
        <span class="attr-label">Data Written (TB)</span>
        <input type="number" step="0.01" class="attr-input" value="${(writes.bytes / 1e12).toFixed(2)}"
          onchange="updateWrites('${drive.id}', parseFloat(this.value)||0)">
      </div>`;
    }

    // NVMe attributes
    const nvme = sd.nvme_smart_health_information_log;
    if (nvme) {
      const nvmeFields = [
        ["critical_warning", "Critical Warning"],
        ["temperature", "Temperature (°C)"],
        ["available_spare", "Available Spare (%)"],
        ["available_spare_threshold", "Spare Threshold (%)"],
        ["percentage_used", "Percentage Used (%)"],
        ["power_on_hours", "Power-On Hours"],
        ["power_cycles", "Power Cycles"],
        ["media_errors", "Media Errors"],
      ];
      for (const [key, label] of nvmeFields) {
        const val = nvme[key] ?? 0;
        const hint = attrHint(key, "NVMe");
        const cls = (key === "critical_warning" && val !== 0) || (key === "media_errors" && val > 0) ? "critical"
                  : (key === "available_spare" && val < 20) || (key === "percentage_used" && val >= 90) ? "warning" : "";
        html += `<div class="attr-row ${cls}">
          <span class="attr-label">${label}<span class="attr-hint">${hint ? " " + hint : ""}</span></span>
          <input type="number" class="attr-input" value="${val}"
            onchange="updateAttr('${drive.id}','${key}',parseInt(this.value)||0)">
        </div>`;
      }
    }

    // ATA attributes
    const ataTable = (sd.ata_smart_attributes || {}).table || [];
    if (ataTable.length > 0) {
      for (const attr of ataTable) {
        const raw = (attr.raw || {}).value || 0;
        const cls = attrClass(attr.name);
        const hint = attrHint(attr.name, "ATA");
        const label = attr.name.replace(/_/g, " ");
        html += `<div class="attr-row ${cls}">
          <span class="attr-label">${label}<span class="attr-hint">${hint ? " " + hint : ""}</span></span>
          <input type="number" class="attr-input" value="${raw}"
            onchange="updateAttr('${drive.id}','${attr.name}',parseInt(this.value)||0)">
        </div>`;
      }
    }

    // Empty / unsupported
    if (!nvme && ataTable.length === 0) {
      html += `<div style="color:var(--muted);font-size:0.82rem;grid-column:1/-1;">
        No SMART attributes, so this drive will show as UNSUPPORTED in HA.</div>`;
    }

    html += `</div></div>`;
  }
  for (const pool of pools) html += renderPool(pool);
  container.innerHTML = html;
}

/* ── Auto-refresh every 3s ── */
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


# ── HTTP Handler ─────────────────────────────────────────────────────────────


class MockHandler(BaseHTTPRequestHandler):

    server_version = f"SmartSnifferMock/{VERSION}"
    token: str = ""
    port: int = 9099

    def log_message(self, fmt, *args):
        # Quieter logging: skip the routine poll requests.
        path = str(args[0]) if args else ""
        if "GET /api/" in path:
            return
        super().log_message(fmt, *args)

    def _check_auth(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {self.token}":
            return True
        self._json_response(401, {"error": "unauthorized"})
        return False

    def _json_response(self, code: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object")
        return body

    def _path(self) -> str:
        """The request path, with /mock/... read as /api/... (except /mock/state)."""
        path = unquote(urlparse(self.path).path).rstrip("/")
        if path.startswith("/mock/") and path != "/mock/state":
            path = "/api/" + path[len("/mock/"):]
        return path

    def _dispatch(self, method: str) -> None:
        try:
            body = self._read_body() if method in ("POST", "PATCH") else {}
            handler = getattr(self, f"_route_{method.lower()}")
            handler(self._path(), body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "the body is not valid JSON"})
        except BadRequest as err:
            self._json_response(err.code, {"error": str(err), **err.extra})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ── Read-only views ──

    def _health(self) -> dict:
        endpoints = ["/api/health", "/api/drives", "/api/drives/{id}"]
        with store.lock:
            drive_count = len(store.drives)
        health: dict[str, Any] = {
            "status": "ok",
            "version": VERSION,
            "os": "mock",
            "uptime_seconds": int(time.time() - STARTED_AT),
            "endpoints": endpoints,
            "drives": drive_count,
            "filesystems": 0,
        }
        if store.pools_enabled():
            endpoints.append("/api/pools")
            health["pools"] = len(store.served_pools())
            health["pools_status"] = "ok"
        # Not in the real agent: for the app's Control Center and dashboard.
        health.update({
            "mock": True,
            "hostname": socket.gethostname().split(".")[0],
            "port": self.port,
            "auth_enabled": bool(self.token),
            "drive_count": drive_count,
        })
        return health

    def _lab_state(self) -> dict:
        poll = store.poll_info
        return {
            "version": VERSION,
            "drives": store.get_all(),
            "pools": store.all_pools(),
            "presets": library.preset_keys(),
            "poll_count": poll["count"],
            "last_poll": poll["last"],
            "port": self.port,
            "auth": bool(self.token),
        }

    # ── Routes ──

    def _route_get(self, path: str, _body: dict) -> None:
        # Dashboard and its state (no auth required).
        if path == "" or path == "/":
            listing = library.listing()["files"]
            replicas = [{"id": f["id"], "name": f["name"], "kind": f["kind"]} for f in listing if f["valid"]]
            replicas_json = json.dumps(replicas).replace("</", "<\\/")
            self._html_response(DASHBOARD_HTML.replace("REPLICAS_JSON", replicas_json))
            return
        if path == "/mock/state":
            self._json_response(200, self._lab_state())
            return

        # ── Agent API (auth required) ──
        if not self._check_auth():
            return
        parts = path.strip("/").split("/")

        if path == "/api/health":
            self._json_response(200, self._health())
        elif path == "/api/drives":
            store.record_poll()
            self._json_response(200, store.get_summaries())
        elif len(parts) == 3 and parts[:2] == ["api", "drives"]:
            drive = store.get_drive(parts[2])
            if drive:
                self._json_response(200, drive)
            else:
                self._json_response(404, {"error": "drive not found"})
        elif path == "/api/pools" and store.pools_enabled():
            self._json_response(200, store.served_pools())
        elif len(parts) == 3 and parts[:2] == ["api", "pools"]:
            pool = store.get_pool(parts[2])
            if pool:
                self._json_response(200, pool)
            else:
                self._json_response(404, {"error": "pool not found"})
        elif path in ("/api/lab", "/api/state"):
            # /api/state is /mock/state after the app's proxy rewrites it.
            self._json_response(200, self._lab_state())
        elif path == "/api/replicas":
            self._json_response(200, library.listing())
        else:
            self._json_response(404, {"error": "not found"})

    def _add(self, body: dict) -> None:
        """POST /api/drives and /api/pools: {"replica": id} or {"preset": key}.

        Either route takes either kind; the file decides whether a drive or
        a pool is added. "name" names a new pool.
        """
        name = body.get("name")
        if "replica" in body:
            if "preset" in body:
                raise BadRequest("give replica or preset, not both")
            if not isinstance(body["replica"], str):
                raise BadRequest("replica must be a file id such as shipped.nvme-wear")
            self._json_response(201, store.add_replica(body["replica"], name))
            return
        preset = body.get("preset")
        added = store.add_preset(preset, name) if isinstance(preset, str) else None
        if added is None:
            self._json_response(400, {"error": f"unknown preset: {preset}"})
        else:
            self._json_response(201, added)

    def _route_post(self, path: str, body: dict) -> None:
        parts = path.strip("/").split("/")
        if path in ("/api/drives", "/api/pools"):
            self._add(body)
        elif len(parts) == 4 and parts[:2] == ["api", "pools"] and parts[3] in ("vanish", "restore"):
            self._json_response(200, store.set_vanished(parts[2], parts[3] == "vanish"))
        else:
            self._json_response(404, {"error": "not found"})

    def _route_patch(self, path: str, body: dict) -> None:
        parts = path.strip("/").split("/")
        if len(parts) in (3, 4) and parts[:2] == ["api", "drives"]:
            drive_id = parts[2]
            sub = parts[3] if len(parts) == 4 else "smart"
            if sub == "smart":
                self._patch_smart(drive_id, body)
            elif sub == "devstat":
                self._json_response(200, store.update_devstat(drive_id, body))
            elif sub == "derived":
                self._json_response(200, store.update_derived(drive_id, body))
            else:
                self._json_response(404, {"error": "not found"})
        elif len(parts) == 3 and parts[:2] == ["api", "pools"]:
            self._json_response(200, store.update_pool(parts[2], body))
        else:
            self._json_response(404, {"error": "not found"})

    def _patch_smart(self, drive_id: str, body: dict) -> None:
        # Convert numeric strings to ints.
        updates = {}
        for k, v in body.items():
            if k == "smart_passed":
                updates[k] = bool(v)
            elif isinstance(v, str):
                try:
                    updates[k] = int(v)
                except ValueError:
                    updates[k] = v
            else:
                updates[k] = v
        if store.update_smart(drive_id, updates):
            self._json_response(200, {"ok": True})
        else:
            self._json_response(404, {"error": "drive not found"})

    def _route_delete(self, path: str, _body: dict) -> None:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "drives"]:
            if store.remove_drive(parts[2]):
                self._json_response(200, {"ok": True})
            else:
                self._json_response(404, {"error": "drive not found"})
        elif len(parts) == 3 and parts[:2] == ["api", "pools"]:
            if store.remove_pool(parts[2]):
                self._json_response(200, {"ok": True})
            else:
                self._json_response(404, {"error": "pool not found"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


# ── mDNS advertisement (optional) ───────────────────────────────────────────

def start_mdns(port: int, token: str) -> Any:
    """Try to advertise via zeroconf. Returns the Zeroconf instance or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print("[mock] zeroconf not installed, skipping mDNS advertisement.")
        print("[mock] Install with: pip install zeroconf")
        return None

    hostname = socket.gethostname().split(".")[0]
    instance = f"smartha-mock-{hostname}"
    stype = "_smartha._tcp.local."
    props = {
        b"txtvers": b"1",
        b"version": VERSION.encode(),
        b"hostname": hostname.encode(),
        b"os": b"mock",
        b"auth": b"1" if token else b"0",
        b"drives": str(len(store.drives)).encode(),
    }

    info = ServiceInfo(
        stype,
        f"{instance}.{stype}",
        port=port,
        properties=props,
        server=f"{hostname}.local.",
    )

    zc = Zeroconf()
    zc.register_service(info)
    print(f"[mock] mDNS: advertising {instance}.{stype} on port {port}")
    return zc


# ── Main ─────────────────────────────────────────────────────────────────────


class MockHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_server(argv: list[str] | None = None) -> tuple[MockHTTPServer, Any, argparse.Namespace]:
    """Parse the command line, set up the store and bind the server.

    Returns (server, zeroconf or None, args). main() serves forever; tests
    call this with --port 0 and run serve_forever in a thread.
    """
    parser = argparse.ArgumentParser(
        description="SMART Sniffer Mock Agent: fake smartha-agent for testing",
    )
    parser.add_argument("--port", type=int, default=9099, help="Port to listen on (default: 9099)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Address to bind (default: 0.0.0.0)")
    parser.add_argument("--token", type=str, default="", help="Bearer token (default: none)")
    parser.add_argument("--no-mdns", action="store_true", help="Disable mDNS advertisement")
    parser.add_argument("--preload", type=str, default="",
                        help="Comma-separated preset keys to load on startup (e.g. sata_hdd,nvme,zfs_pool)")
    parser.add_argument("--data-dir", type=str, default="",
                        help="Directory for persistent drive and pool data (default: none, in-memory only)")
    parser.add_argument("--replicas", action="append", default=[], metavar="DIR",
                        help="Read-only folder of replica files; repeatable "
                             "(default: tools/replicas and tools/replicas/extra beside this script)")
    parser.add_argument("--user-replicas", type=str, default="", metavar="DIR",
                        help="Read-write folder for uploaded and saved replica files (default: none)")
    args = parser.parse_args(argv)

    # The replica files, then the store with optional persistence.
    global store, library, STARTED_AT
    STARTED_AT = time.time()
    library = ReplicaLibrary(args.replicas or DEFAULT_REPLICA_DIRS, args.user_replicas or None)
    persist_path = None
    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        persist_path = os.path.join(args.data_dir, "mock-drives.json")
    store = DriveStore(persist_path=persist_path)

    # Try to load saved drives first; only preload if nothing was saved.
    loaded = store.load()
    if not loaded and args.preload:
        for key in args.preload.split(","):
            key = key.strip()
            try:
                added = store.add_preset(key)
                if added is None and library.find(key) is not None:
                    added = store.add_replica(key)
            except BadRequest as err:
                print(f"[mock] Cannot preload {key}: {err.extra.get('message', err)}")
                continue
            if added is None:
                print(f"[mock] Unknown preset or replica: {key}")
            else:
                print(f"[mock] Preloaded {key} -> {added['kind']} {added['id']}")
    elif loaded:
        print(f"[mock] Restored {len(store.drives)} drives and {len(store.pools)} pools "
              "from disk, skipping preload")

    server = MockHTTPServer((args.host, args.port), MockHandler)

    # Set handler class attributes.
    MockHandler.token = args.token
    MockHandler.port = server.server_address[1]

    # Start mDNS.
    zc = None
    if not args.no_mdns:
        zc = start_mdns(MockHandler.port, args.token)
    return server, zc, args


def main(argv: list[str] | None = None) -> None:
    server, zc, args = build_server(argv)
    port = MockHandler.port

    auth_str = "enabled" if args.token else "disabled"
    print(f"\n  SMART Sniffer Mock Agent v{VERSION}")
    print(f"  Dashboard:  http://localhost:{port}/")
    print(f"  API:        http://localhost:{port}/api/drives")
    print(f"  Auth:       {auth_str}")
    print(f"  Drives:     {len(store.drives)}")
    print(f"  Pools:      {len(store.pools)}")
    files = library.listing()
    valid = sum(1 for f in files["files"] if f["valid"])
    print(f"  Replicas:   {valid} files, user folder {files['folder']['path'] or 'none'} "
          f"({files['folder']['status']})")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] Shutting down...")
    finally:
        server.server_close()
        if zc:
            zc.unregister_all_services()
            zc.close()


if __name__ == "__main__":
    main()
