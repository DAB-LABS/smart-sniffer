"""The Replica machine's engine: replica files served as an agent (v0.9).

HA-free and stdlib only; every call runs in Home Assistant's executor.

- The library: the replica files in up to three folders, the seed set
  shipped with the integration, the user's own folder (Save as replica and
  uploads land there) and a read-only drop folder. Files are read as JSON
  and checked (replica_file.validate_replica), never executed; a file is
  re-read when its modification time or size changes.
- render(): the drives and pools the user picked, each from its file, in
  the shapes the agent serves (/api/health, /api/drives, /api/drives/{id},
  /api/filesystems, /api/pools), with a little live drift so the cards
  look alive: power-on hours grow with the time since the pick, and the
  temperature moves by at most one degree. Nothing the rules judge
  changes, so a file's verdict never changes by drift.

Each pick gets its own drive id ("replica-<entry8>-<pick8>"), serial,
device path and filesystem ids, chosen when it is picked and never taken
from the file, so a replica never collides with a real drive or another
pick of the same file.

The payload helpers between the two rules below are copied unchanged from
tools/mock-agent.py, so the Replica serves what the mock agent serves for
the same file; tests/test_replica_engine.py checks both the copy and the
payloads.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any

from .replica_file import (
    MAX_REPLICA_BYTES,
    REPLICA_KINDS,
    InvalidReplica,
    _file_slug,
    validate_replica,
)

VERSION = "0.8.0-mock"  # what the mock agent reports (tools/mock-agent.py)
DEFAULT_PORT = 9099

MAX_DRIVES = 12
MAX_POOLS = 4
MAX_FOLDER_FILES = 200
MAX_FOLDER_BYTES = 50 * 1024 * 1024

SEED, USER, SHARE = "seed", "user", "share"
SOURCES = (SEED, USER, SHARE)

DRIFT_STEP = 600  # seconds: the temperature may change once per step

# ── Copied from tools/mock-agent.py (unchanged) ─────────────────────────────

def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

_MISSING, _OK, _INVALID, _OVERFLOW = "missing", "ok", "invalid", "overflow"

_U64 = 1 << 64

NVME_DATA_UNIT = 512_000

_RATE_BOUND = 1.2e9

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

def _mirror_state(devices: list[dict]) -> str:
    states = [d.get("state") for d in devices]
    if all(s == "ONLINE" for s in states):
        return "ONLINE"
    if all(s != "ONLINE" for s in states):
        return "UNAVAIL"
    return "DEGRADED"

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

def _fs_percent(used: int, available: int) -> float:
    """df's percent, used / (used + available), to one decimal as the agent rounds it."""
    denom = used + available
    if denom <= 0:
        return 0.0
    return float(int(used / denom * 1000 + 0.5)) / 10

def fs_payload(fs: dict, unavailable: bool = False, stored: bool = False) -> dict:
    """One filesystem as GET /api/filesystems lists it.

    unavailable forces the agent's unreadable form (a vanished pool's
    datasets). stored keeps the bytes of an unavailable one, for a file.
    """
    status = "unavailable" if unavailable else fs["status"]
    out = {"id": fs["id"], "uuid": fs["uuid"], "mountpoint": fs["mountpoint"], "device": fs["device"],
           "fstype": fs["fstype"], "total_bytes": 0, "used_bytes": 0, "available_bytes": 0,
           "use_percent": 0.0, "status": status}
    if status == "ok" or stored:
        total, used = fs["total_bytes"], fs["used_bytes"]
        available = max(total - used, 0)
        out.update(total_bytes=total, used_bytes=used, available_bytes=available,
                   use_percent=_fs_percent(used, available))
    return out

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


# ── End of the copy ─────────────────────────────────────────────────────────


class ReplicaError(Exception):
    """A replica request refused: code is the panel's error code (the mock
    agent's codes plus replica_cap and replica_folder_full)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number in JSON")


def parse_document(raw: bytes) -> dict:
    """A replica file's bytes as a checked document: at most 1 MB (checked
    before anything is parsed), UTF-8, JSON (no NaN or Infinity), a valid
    replica. Raises ReplicaError too_large or invalid_replica."""
    if len(raw) > MAX_REPLICA_BYTES:
        raise ReplicaError("too_large", "A replica file is at most 1 MB.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ReplicaError("invalid_replica", "not UTF-8 text") from None
    try:
        doc = json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise ReplicaError("invalid_replica", "not valid JSON") from None
    try:
        return validate_replica(doc)
    except (InvalidReplica, RecursionError, TypeError, AttributeError, KeyError) as err:
        raise ReplicaError("invalid_replica", str(err) or "not a replica file") from None


def upload_origin(origin: dict) -> dict:
    """The origin of a file that came in by upload: uploaded, unless it was
    saved from a real drive (then saved, with its saved_at)."""
    if origin.get("type") == "saved":
        saved = {"type": "saved"}
        if isinstance(origin.get("saved_at"), str):
            saved["saved_at"] = origin["saved_at"]
        return saved
    return {"type": "uploaded"}


def _entry(rec: dict) -> dict:
    """One file as the panel lists it. Every string in it is user data."""
    base = {"id": rec["id"], "source": rec["source"], "valid": rec.get("doc") is not None}
    doc = rec.get("doc")
    if doc is None:
        return {**base, "name": rec["stem"], "kind": None, "description": "",
                "reason": rec.get("reason") or "invalid", "error": rec.get("error") or ""}
    return {**base, "name": doc["name"], "kind": doc["kind"], "description": doc["description"],
            "order": doc.get("order") if rec["source"] == SEED else None,
            "pool": doc["kind"] == "zfs_pool"}


class ReplicaLibrary:
    """The replica files on disk, re-read when they change (mtime and size).

    folders: {source: path}; the seed and share folders are read only, the
    user folder is where files are written. A folder that does not exist
    (or cannot be read) lists no files."""

    def __init__(self, folders: dict[str, str | None]) -> None:
        self.lock = threading.Lock()
        self.folders = {s: folders.get(s) for s in SOURCES}
        self._cache: dict[str, tuple[int, int, dict]] = {}
        self.files: list[dict] = []
        self.counts: dict[str, tuple[int, int]] = {}

    # ── Reading ──

    def _read(self, path: str, st: os.stat_result, source: str, stem: str) -> dict:
        cached = self._cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        rec: dict[str, Any] = {"id": f"{source}.{stem}", "source": source, "stem": stem,
                               "path": path, "doc": None, "error": None, "reason": None}
        try:
            if st.st_size > MAX_REPLICA_BYTES:
                raise ReplicaError("too_large", "larger than 1 MB")
            with open(path, "rb") as f:
                raw = f.read(MAX_REPLICA_BYTES + 1)
            rec["doc"] = parse_document(raw)
        except ReplicaError as err:
            rec["error"] = err.message
            rec["reason"] = "too_large" if err.code == "too_large" else "invalid"
        except OSError as err:
            rec["error"] = f"cannot read the file: {err.strerror or err}"
            rec["reason"] = "unreadable"
        self._cache[path] = (st.st_mtime_ns, st.st_size, rec)
        return rec

    def _scan(self, source: str) -> list[dict]:
        folder = self.folders.get(source)
        if not folder:
            return []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return []
        out = []
        size = 0
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            stem = name[:-len(".json")]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", stem):
                continue
            path = os.path.join(folder, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            # Regular files only: never a link out of the folder.
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            size += st.st_size
            if len(out) >= MAX_FOLDER_FILES:
                continue
            out.append(self._read(path, st, source, stem))
        self.counts[source] = (len(out), size)
        return out

    def refresh(self) -> None:
        """List the folders again and parse any file that changed."""
        with self.lock:
            files: list[dict] = []
            for source in SOURCES:
                files.extend(self._scan(source))

            def sort_key(rec: dict) -> tuple:
                doc = rec.get("doc") or {}
                order = doc.get("order") if rec["source"] == SEED else None
                return (SOURCES.index(rec["source"]), order is None, order or 0,
                        str(doc.get("name", rec["stem"])).lower(), rec["id"])

            self.files = sorted(files, key=sort_key)
            live = {rec["path"] for rec in files}
            self._cache = {p: v for p, v in self._cache.items() if p in live}

    def folder_status(self, source: str = USER) -> str:
        """ok, missing, read_only or full (the user folder); ok, missing or
        unreadable (the others)."""
        path = self.folders.get(source)
        if not path or not os.path.isdir(path):
            return "missing"
        if source != USER:
            return "ok" if os.access(path, os.R_OK | os.X_OK) else "unreadable"
        if not os.access(path, os.W_OK | os.X_OK):
            return "read_only"
        count, size = self.counts.get(USER, (0, 0))
        if count >= MAX_FOLDER_FILES or size >= MAX_FOLDER_BYTES:
            return "full"
        return "ok"

    def listing(self) -> dict:
        self.refresh()
        with self.lock:
            files = [_entry(rec) for rec in self.files]
        return {"files": files,
                "folders": {source: self.folder_status(source) for source in SOURCES
                            if source == USER or self.folders.get(source)}}

    def find(self, file_id: Any) -> dict | None:
        """The file record for an id from the listing (the folders are read
        again first). Ids are looked up, never turned into a path."""
        if not isinstance(file_id, str):
            return None
        self.refresh()
        with self.lock:
            return next((rec for rec in self.files if rec["id"] == file_id), None)

    # ── Writing ──

    def write_user(self, doc: dict) -> dict:
        """Write a checked document into the user folder under a name made
        here from its name; return its listing entry."""
        folder = self.folders.get(USER)
        if not folder:
            raise ReplicaError("replica_folder_unavailable", "Your replica folder is not available.")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            raise ReplicaError("replica_folder_unavailable",
                               "Your replica folder is not available.") from None
        self.refresh()
        status = self.folder_status(USER)
        if status == "full":
            raise ReplicaError("replica_folder_full", "Your replica folder is full.")
        if status != "ok":
            raise ReplicaError("replica_folder_unavailable", "Your replica folder is not available.")
        validate_replica(doc)
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        data = text.encode("utf-8")
        if len(data) > MAX_REPLICA_BYTES:
            raise ReplicaError("too_large", "A replica file is at most 1 MB.")
        if self.counts.get(USER, (0, 0))[1] + len(data) > MAX_FOLDER_BYTES:
            raise ReplicaError("replica_folder_full", "Your replica folder is full.")
        base = _file_slug(doc["name"])
        with self.lock:
            n = 1
            while True:
                stem = base if n == 1 else f"{base}-{n}"
                path = os.path.join(folder, stem + ".json")
                if not os.path.lexists(path):
                    break
                n += 1
            tmp = os.path.join(folder, f".{stem}.json.{uuid.uuid4().hex[:8]}.tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise ReplicaError("replica_folder_unavailable",
                                   "Your replica folder is not available.") from None
        rec = self.find(f"{USER}.{stem}")
        if rec is None or rec.get("doc") is None:  # pragma: no cover - just written
            raise ReplicaError("replica_folder_unavailable", "The new file could not be read back.")
        return _entry(rec)


    def delete_user(self, file_id: Any) -> dict:
        """Delete one of the user's own files (Save as replica, uploads),
        found by its id in the listing; seed and drop-folder files are never
        deleted. Returns the deleted file's listing entry."""
        rec = self.find(file_id)
        if rec is None:
            raise ReplicaError("not_found", "That replica file is gone.")
        folder = self.folders.get(USER)
        if rec["source"] != USER or not folder:
            raise ReplicaError("not_deletable", "Only your own replica files can be deleted.")
        path = rec["path"]
        # Only a regular file directly in the user folder, never a link.
        if os.path.dirname(os.path.realpath(path)) != os.path.realpath(folder) \
                or os.path.islink(path) or not os.path.isfile(path):
            raise ReplicaError("not_deletable", "Only your own replica files can be deleted.")
        entry = _entry(rec)
        with self.lock:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                raise ReplicaError("replica_folder_unavailable",
                                   "Your replica folder is not available.") from None
            self._cache.pop(path, None)
        self.refresh()
        return entry

    def update_user(self, file_id: Any, doc: dict) -> dict:
        """Write a checked document over one of the user's own files, in
        place (atomically); return its listing entry."""
        rec = self.find(file_id)
        folder = self.folders.get(USER)
        if rec is None:
            raise ReplicaError("not_found", "That replica file is gone.")
        if rec["source"] != USER or not folder or os.path.islink(rec["path"]) or \
                os.path.dirname(os.path.realpath(rec["path"])) != os.path.realpath(folder):
            raise ReplicaError("not_deletable", "Only your own replica files can be changed.")
        try:
            validate_replica(doc)
        except InvalidReplica as err:
            raise ReplicaError("invalid_replica", str(err)) from None
        data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_REPLICA_BYTES:
            raise ReplicaError("too_large", "A replica file is at most 1 MB.")
        path = rec["path"]
        with self.lock:
            tmp = os.path.join(folder, f".{rec['stem']}.json.{uuid.uuid4().hex[:8]}.tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise ReplicaError("replica_folder_unavailable",
                                   "Your replica folder is not available.") from None
            self._cache.pop(path, None)
        rec = self.find(file_id)
        if rec is None or rec.get("doc") is None:  # pragma: no cover - just written
            raise ReplicaError("replica_folder_unavailable", "The file could not be read back.")
        return _entry(rec)

# ── Picks ───────────────────────────────────────────────────────────────────
#
# A pick is one drive or pool card of the Replica machine: which file it
# shows, and the identity chosen for it when it was picked. Kept in the
# Replica's Store by the integration; nothing in it comes from a client.


def pick_counts(picks: list[dict]) -> tuple[int, int]:
    """(drives, pools) among the picks."""
    pools = sum(1 for p in picks if p.get("kind") == "pool")
    return len(picks) - pools, pools


def _free_path(path: str, taken: set[str]) -> str:
    """The file's device path, or the next free one like it."""
    if path and path not in taken:
        return path
    m = re.fullmatch(r"(/dev/nvme)(\d+)(n\d+)", path or "")
    if m:
        n = int(m.group(2))
        while True:
            n += 1
            cand = f"{m.group(1)}{n}{m.group(3)}"
            if cand not in taken:
                return cand
    m = re.fullmatch(r"(/dev/sd)([a-z]+)", path or "")
    if m:
        for first in [""] + [chr(c) for c in range(ord("a"), ord("z") + 1)]:
            for second in (chr(c) for c in range(ord("a"), ord("z") + 1)):
                cand = f"{m.group(1)}{first}{second}"
                if cand not in taken:
                    return cand
    base = path or "/dev/replica"
    n = 1
    while True:
        n += 1
        cand = f"{base}-{n}"
        if cand not in taken:
            return cand


def _free_pool_name(name: str, taken: set[str]) -> str:
    """The file's pool name, or name2, name3 (as the mock agent names them)."""
    if name not in taken:
        return name
    n = 1
    while True:
        n += 1
        if f"{name}{n}" not in taken:
            return f"{name}{n}"


def new_pick(file_rec: dict, picks: list[dict], entry_id: str, now: float | None = None,
             pick_id: str | None = None) -> dict:
    """A new pick of a valid file, its identity chosen against the other
    picks. Raises ReplicaError for an invalid file or past the caps."""
    doc = file_rec.get("doc")
    if doc is None:
        raise ReplicaError("invalid_replica", file_rec.get("error") or "not a replica file")
    drives, pools = pick_counts(picks)
    is_pool = doc["kind"] == "zfs_pool"
    if (is_pool and pools >= MAX_POOLS) or (not is_pool and drives >= MAX_DRIVES):
        raise ReplicaError("replica_cap", "The Replica holds up to 12 drives and 4 pools.")
    pick_id = pick_id or uuid.uuid4().hex
    pick: dict[str, Any] = {
        "pick_id": pick_id, "file_id": file_rec["id"], "name": doc["name"],
        "added_at": float(now if now is not None else time.time()),
        "kind": "pool" if is_pool else "drive",
    }
    if is_pool:
        pick["pool_name"] = _free_pool_name(
            doc["pool"]["name"], {str(p.get("pool_name")) for p in picks if p.get("kind") == "pool"})
    else:
        pick["drive_id"] = f"replica-{entry_id[:8].lower()}-{pick_id[:8]}"
        pick["serial"] = f"REPLICA-{pick_id[:8].upper()}"
        pick["device_path"] = _free_path(
            doc["meta"]["device_path"],
            {str(p.get("device_path")) for p in picks if p.get("kind") == "drive"})
    return pick


def clean_picks(raw: Any) -> list[dict]:
    """The picks of a Store, the ones that make sense."""
    out = []
    seen: set[str] = set()
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("pick_id"), str):
            continue
        if not isinstance(p.get("file_id"), str) or p["pick_id"] in seen:
            continue
        drive_ok = p.get("kind") == "drive" and all(
            isinstance(p.get(k), str) for k in ("drive_id", "serial", "device_path"))
        pool_ok = p.get("kind") == "pool" and isinstance(p.get("pool_name"), str)
        if not (drive_ok or pool_ok):
            continue
        if not isinstance(p.get("added_at"), (int, float)) or isinstance(p.get("added_at"), bool):
            continue
        seen.add(p["pick_id"])
        out.append(p)
    return out


def card_key(pick: dict) -> str:
    """The pick's card key on the panel (panel_view.drive_card_key)."""
    return f"pool:{pick['pool_name']}" if pick["kind"] == "pool" else pick["serial"]


# ── Serving ─────────────────────────────────────────────────────────────────


def _drift_smart(smart: dict, hours: int, delta: int) -> None:
    """Power-on hours plus ``hours`` and the temperature plus ``delta``
    degrees, in the top-level keys and the rows they are read from."""
    if hours:
        pot = smart.get("power_on_time")
        if isinstance(pot, dict) and isinstance(pot.get("hours"), int) and not isinstance(pot["hours"], bool):
            pot["hours"] += hours
        log = smart.get("nvme_smart_health_information_log")
        if isinstance(log, dict) and isinstance(log.get("power_on_hours"), int):
            log["power_on_hours"] += hours
    temp = smart.get("temperature")
    if delta and isinstance(temp, dict) and isinstance(temp.get("current"), int) \
            and not isinstance(temp["current"], bool):
        temp["current"] = max(0, temp["current"] + delta)
    log = smart.get("nvme_smart_health_information_log")
    if delta and isinstance(log, dict) and isinstance(log.get("temperature"), int):
        log["temperature"] = max(0, log["temperature"] + delta)
    for attr in _attr_table(smart):
        raw = attr.get("raw")
        if not isinstance(raw, dict) or not isinstance(raw.get("value"), int):
            continue
        if attr.get("id") == 9 and hours:
            value = shown = raw["value"] + hours
        elif attr.get("id") in (190, 194) and delta:
            low = raw["value"] & 0xFF
            shown = min(255, max(0, low + delta))
            value = raw["value"] - low + shown
        else:
            continue
        attr["raw"] = {**raw, "value": value}
        if isinstance(raw.get("string"), str):
            attr["raw"]["string"] = re.sub(r"^\d+", str(shown), raw["string"], count=1)


def _drift_devstat(natural: dict, smart: dict, hours: int) -> None:
    """Device Statistics as the drifted SMART data has them: power-on hours
    plus ``hours``, the current temperature as the drive's now."""
    if hours:
        entry = _devstat_entry_of(natural, 1, 16)
        if entry is not None and isinstance(entry.get("value"), int):
            entry["value"] += hours
    temp = (smart.get("temperature") or {}).get("current")
    if not isinstance(temp, int) or isinstance(temp, bool):
        raws = {a.get("id"): (a.get("raw") or {}).get("value") for a in _attr_table(smart)}
        temp = next((v & 0xFF for k in ATA_TEMP_IDS if isinstance(v := raws.get(k), int)), None)
    entry = _devstat_entry_of(natural, 5, 8)
    if entry is not None and isinstance(temp, int) and not isinstance(temp, bool):
        entry["value"] = temp


def drift_for(pick: dict, now: float) -> tuple[int, int]:
    """(hours, degrees) of drift for a pick now: whole hours since it was
    picked, and -1, 0 or +1 degree, the same for every read in one step."""
    hours = max(0, int((now - float(pick.get("added_at") or now)) // 3600))
    step = int(now // DRIFT_STEP)
    delta = zlib.crc32(f"{pick['pick_id']}:{step}".encode()) % 3 - 1
    return hours, delta


def _fs_records(doc: dict, pick: dict, old_root: str, new_root: str, is_pool: bool) -> list[dict]:
    """The file's filesystems for one pick: on the pick's device or pool,
    with ids of the pick's own."""
    out = []
    for i, item in enumerate(doc.get("filesystems") or []):
        rec = {k: copy.deepcopy(item.get(k)) for k in
               ("mountpoint", "device", "fstype", "total_bytes", "used_bytes", "status")}
        rec["status"] = rec["status"] or "ok"
        device = str(rec["device"])
        if old_root and new_root != old_root and (device == old_root or device.startswith(
                old_root + ("/" if is_pool else ""))):
            rec["device"] = new_root + device[len(old_root):]
        rec["uuid"] = f"{pick['pick_id'][:8]}-0000-4000-8000-{i + 1:012d}"
        rec["id"] = f"fs-{pick['pick_id'][:8]}-{i + 1}"
        out.append(rec)
    return out


def _drive_payload(doc: dict, pick: dict, now: float, drift: bool) -> tuple[dict, list[dict]]:
    meta = doc["meta"]
    smart = copy.deepcopy(doc["smart"])
    if "serial_number" in smart:
        smart["serial_number"] = pick["serial"]
    hours, delta = drift_for(pick, now) if drift else (0, 0)
    if drift:
        _drift_smart(smart, hours, delta)
    natural = _natural_from_file(doc["devstat"])
    _drift_devstat(natural, smart, hours)
    status = natural.get("status")
    if status == "present":
        complete = bool(natural.get("complete", True))
        ds: dict[str, Any] = {"status": "present", "complete": complete,
                              "exit_status": 0 if complete else 4,
                              "logical_block_size": natural.get("logical_block_size", 512),
                              "pages": copy.deepcopy(natural.get("pages") or [])}
    else:
        ds = {"status": status}
        if natural.get("reason"):
            ds["reason"] = natural["reason"]
    drive = {
        "id": pick["drive_id"],
        "device_path": pick["device_path"],
        "model": meta["model"],
        "serial": pick["serial"],
        "protocol": meta["protocol"],
        "readable": True,
        "last_updated": _rfc3339(now),
        "smart_data": smart,
        "device_statistics": ds,
        "derived": derive_volumes({"smart_data": smart, "protocol": meta["protocol"]}, ds),
    }
    fs = _fs_records(doc, pick, meta["device_path"], pick["device_path"], False)
    return drive, fs


def _pool_record(file_rec: dict, pick: dict, now: float) -> tuple[dict, list[dict]]:
    doc = file_rec["doc"]
    rec = copy.deepcopy(doc["pool"])
    rec.setdefault("status", None)
    rec.setdefault("action", None)
    # A shipped file's last scrub is an age: kept from the file's verdict
    # time, counted back from now (as the mock agent does).
    if file_rec["source"] == SEED:
        end = _parse_time(rec.get("last_scrub_end"))
        at = _parse_time((doc.get("verdict") or {}).get("at"))
        if end is not None and at is not None:
            shifted = datetime.fromtimestamp(int(now), tz=timezone.utc) - (at - end)
            rec["last_scrub_end"] = shifted.strftime("%Y-%m-%dT%H:%M:%SZ")
    old = rec["name"]
    rec["name"] = pick["pool_name"]
    fs = _fs_records(doc, pick, old, pick["pool_name"], True)
    return rec, fs


def render(picks: list[dict], library: ReplicaLibrary, *, agent_id: str, started_at: float,
           now: float | None = None, drift: bool = True, port: int = DEFAULT_PORT) -> dict:
    """What the Replica serves now for these picks.

    {"health", "drives" (the /api/drives list), "drive" (id -> payload),
    "filesystems", "pools", "cards" (card key -> the pick's file, name and
    source), "missing" (picks whose file is gone or no longer valid)}."""
    now = time.time() if now is None else now
    drives: list[dict] = []
    payloads: dict[str, dict] = {}
    pools: list[dict] = []
    filesystems: list[dict] = []
    cards: dict[str, dict] = {}
    missing: list[dict] = []
    for pick in picks:
        rec = library.find(pick["file_id"])
        doc = (rec or {}).get("doc")
        wanted = "zfs_pool" if pick["kind"] == "pool" else None
        if doc is None or (doc["kind"] == "zfs_pool") != (wanted == "zfs_pool"):
            missing.append(copy.deepcopy(pick))
            continue
        info = {"pick_id": pick["pick_id"], "file_id": pick["file_id"], "name": doc["name"],
                "kind": doc["kind"], "source": rec["source"]}
        if pick["kind"] == "pool":
            pool, fs = _pool_record(rec, pick, now)
            pools.append(pool_payload(pool))
            cards[card_key(pick)] = info
        else:
            drive, fs = _drive_payload(doc, pick, now, drift)
            payloads[drive["id"]] = drive
            drives.append({k: drive[k] for k in
                           ("id", "device_path", "model", "serial", "protocol", "readable")})
            cards[card_key(pick)] = info
        filesystems.extend(fs_payload(f) for f in fs)
    endpoints = ["/api/health", "/api/drives", "/api/drives/{id}"]
    health: dict[str, Any] = {
        "status": "ok",
        "version": VERSION,
        "os": "mock",
        "uptime_seconds": max(0, int(now - started_at)),
        "endpoints": endpoints,
        "drives": len(drives),
        "filesystems": len(filesystems),
    }
    if agent_id:
        health["agent_id"] = agent_id
    if filesystems:
        endpoints.append("/api/filesystems")
    if pools:
        endpoints.append("/api/pools")
        health["pools"] = len(pools)
        health["pools_status"] = "ok"
    health.update({"mock": True, "port": port, "auth_enabled": False, "drive_count": len(drives)})
    return {"health": health, "drives": drives, "drive": payloads, "filesystems": filesystems,
            "pools": pools, "cards": cards, "missing": missing}


def serve(snapshot: dict, path: str) -> tuple[int, Any]:
    """(status, body) for one agent path from a render."""
    parts = path.strip("/").split("/")
    if path == "/api/health":
        return 200, snapshot["health"]
    if path == "/api/drives":
        return 200, snapshot["drives"]
    if len(parts) == 3 and parts[:2] == ["api", "drives"]:
        drive = snapshot["drive"].get(parts[2])
        return (200, drive) if drive is not None else (404, {"error": "drive not found"})
    if path == "/api/filesystems" and snapshot["filesystems"]:
        return 200, snapshot["filesystems"]
    if path == "/api/pools" and snapshot["pools"]:
        return 200, snapshot["pools"]
    return 404, {"error": "not found"}


def pool_document(pool: dict, filesystems: list[dict] | None = None) -> dict:
    """A replica file from a real pool card: the agent lists only the pool's
    problem vdevs, so the disk list is rebuilt from them (one ONLINE disk
    stands in when none is listed). Its verdict reads the same."""
    if not isinstance(pool, dict) or not isinstance(pool.get("name"), str):
        raise ReplicaError("bad_request", "not a pool")
    problems = [p for p in pool.get("problem_vdevs") or [] if isinstance(p, dict)]
    vdev = next((p for p in problems if str(p.get("type") or "") != "disk"), None)
    devices = []
    for p in problems:
        if str(p.get("type") or "") != "disk" or not isinstance(p.get("name"), str):
            continue
        state = p.get("state") if p.get("state") in (
            "ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED") else "FAULTED"
        devices.append({"name": p["name"], "state": state,
                        "read": int(p.get("read_errors") or 0),
                        "write": int(p.get("write_errors") or 0),
                        "cksum": int(p.get("checksum_errors") or 0)})
    # The disks the agent does not list are healthy: one stands in for them
    # when the pool has none listed, or its vdev still runs (DEGRADED).
    if not devices or (vdev is not None and vdev.get("state") == "DEGRADED"):
        devices.append({"name": f"{pool['name']}-disk", "state": "ONLINE",
                        "read": 0, "write": 0, "cksum": 0})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    keep = ("name", "state", "status", "action", "data_errors", "scan_function", "scan_state",
            "scrub_in_progress", "last_scrub_end", "last_scrub_repaired", "last_scrub_errors")
    rec = {k: copy.deepcopy(pool.get(k)) for k in keep if k in pool}
    rec["vdev"] = {"name": str(vdev.get("name")) if vdev else "mirror-0",
                   "type": str(vdev.get("type")) if vdev else "mirror"}
    rec["devices"] = devices
    doc: dict[str, Any] = {
        "format": "smart-sniffer-replica", "version": 1,
        "name": f"ZFS pool {pool['name']}, saved {today}",
        "kind": "zfs_pool",
        "description": f"Saved from a real pool on {today}. Its disk list is rebuilt from the "
                       "pool's problem list.",
        "origin": {"type": "saved", "saved_at": _rfc3339(time.time())},
        "pool": rec,
    }
    fs_items = []
    for fs in filesystems or []:
        if isinstance(fs, dict) and str(fs.get("device") or "").split("/", 1)[0] == pool["name"]:
            fs_items.append({k: fs.get(k) for k in ("mountpoint", "device", "fstype",
                                                    "total_bytes", "used_bytes", "status")})
    if fs_items:
        doc["filesystems"] = fs_items
    try:
        return validate_replica(doc)
    except InvalidReplica as err:
        raise ReplicaError("unsupported_pool", str(err)) from None


__all__ = ["DEVSTAT_EDITS", "MAX_DRIVES", "MAX_POOLS", "REPLICA_KINDS", "ReplicaError", "ReplicaLibrary",
           "clean_picks", "new_pick", "parse_document", "render", "serve"]


# ── Raw edits (the Replica card's tune editor) ─────────────────────────────

# The NVMe health log fields a Replica card's editor may change, with their
# ranges; ATA attributes are changed by their raw value, 0 to 2**48 - 1 (the
# six raw bytes), a temperature (190, 194) 0 to 150.
NVME_EDITS = {"temperature": (0, 150), "available_spare": (0, 100),
              "percentage_used": (0, 255), "media_errors": (0, 2 ** 48 - 1)}
ATA_RAW_MAX = 2 ** 48 - 1
ATA_TEMP_IDS = (190, 194)
# Device Statistics entries a Replica's editor changes, "page:offset" ->
# (lowest, highest): the readings the integration stands in with for a
# missing attribute (reported uncorrectable, reallocated, endurance used).
DEVSTAT_EDITS = {"4:8": (0, 2 ** 32 - 1), "3:32": (0, 2 ** 32 - 1), "7:8": (0, 255)}
# An attribute edit carries over to the Device Statistics entry that reads
# the same thing, when the file has it: attribute id -> (page, offset).
_ATTR_DEVSTAT = {5: (3, 32), 9: (1, 16), 12: (1, 8), 187: (4, 8), 190: (5, 8), 194: (5, 8), 197: (3, 56),
                 199: (6, 24)}


def _devstat_entry_of(doc_or_ds: dict, page: int, offset: int) -> dict | None:
    """The valid entry at page, offset of a devstat part with status present."""
    if not isinstance(doc_or_ds, dict) or doc_or_ds.get("status") != "present":
        return None
    for p in doc_or_ds.get("pages") or []:
        if isinstance(p, dict) and p.get("number") == page:
            for e in p.get("table") or []:
                if isinstance(e, dict) and e.get("offset") == offset:
                    flags = e.get("flags")
                    return e if isinstance(flags, dict) and flags.get("valid") is True else None
    return None


def _whole(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ReplicaError("bad_value", f"{what}: a whole number from {low} to {high}.")
    return value


def edit_document(doc: dict, ata: dict | None = None, nvme: dict | None = None,
                  devstat: dict | None = None) -> dict:
    """A copy of a drive replica with these raw values changed: ata
    {attribute id: raw value} for attributes the file has, nvme {field:
    value} for the fields in NVME_EDITS the file has, devstat {"page:offset":
    value} for the Device Statistics entries in DEVSTAT_EDITS the file has.
    The readings that stand for the same thing (power-on time, temperature,
    power cycles, and the Device Statistics entry an attribute matches)
    follow; a devstat value given for that entry wins. Identity, pools and
    everything else stay as they are."""
    if doc.get("kind") == "zfs_pool":
        raise ReplicaError("bad_value", "A pool's state cannot be edited here.")
    out = copy.deepcopy(doc)
    smart = out.get("smart")
    if not isinstance(smart, dict):
        raise ReplicaError("bad_value", "This replica has no SMART data to edit.")
    table = {a.get("id"): a for a in _attr_table(smart)}
    for key, value in (ata or {}).items():
        try:
            attr_id = int(key)
        except (TypeError, ValueError):
            raise ReplicaError("bad_value", f"attribute {key}: not an attribute number.") from None
        attr = table.get(attr_id)
        if attr is None:
            raise ReplicaError("bad_value", f"attribute {attr_id}: this drive does not report it.")
        name = str(attr.get("name") or attr_id)
        if attr_id in ATA_TEMP_IDS:
            shown = _whole(value, 0, 150, name)
            old = (attr.get("raw") or {}).get("value")
            old = old if isinstance(old, int) and not isinstance(old, bool) else 0
            raw_value = old - (old & 0xFF) + shown
            temp = smart.get("temperature")
            if isinstance(temp, dict):
                temp["current"] = shown
        else:
            shown = raw_value = _whole(value, 0, ATA_RAW_MAX, name)
            if attr_id == 9 and isinstance(smart.get("power_on_time"), dict):
                smart["power_on_time"]["hours"] = shown
            if attr_id == 12 and isinstance(smart.get("power_cycle_count"), int):
                smart["power_cycle_count"] = shown
        raw = attr.get("raw") if isinstance(attr.get("raw"), dict) else {}
        string = raw.get("string")
        if isinstance(string, str) and re.match(r"^\d+", string):
            string = re.sub(r"^\d+", str(shown), string, count=1)
        else:
            string = str(shown)
        attr["raw"] = {**raw, "value": raw_value, "string": string}
        follow = _ATTR_DEVSTAT.get(attr_id)
        entry = _devstat_entry_of(out.get("devstat"), *follow) if follow else None
        if entry is not None:
            entry["value"] = shown
    for key, value in (devstat or {}).items():
        if key not in DEVSTAT_EDITS:
            raise ReplicaError("bad_value", f"Device Statistics {key}: cannot be edited.")
        page, offset = (int(x) for x in key.split(":"))
        entry = _devstat_entry_of(out.get("devstat"), page, offset)
        if entry is None:
            raise ReplicaError("bad_value", f"Device Statistics {key}: this drive does not report it.")
        low, high = DEVSTAT_EDITS[key]
        entry["value"] = _whole(value, low, high, str(entry.get("name") or key))
    log = smart.get("nvme_smart_health_information_log")
    for field, value in (nvme or {}).items():
        if field not in NVME_EDITS:
            raise ReplicaError("bad_value", f"{field}: cannot be edited.")
        if not isinstance(log, dict) or field not in log:
            raise ReplicaError("bad_value", f"{field}: this drive does not report it.")
        low, high = NVME_EDITS[field]
        log[field] = _whole(value, low, high, field)
        if field == "temperature" and isinstance(smart.get("temperature"), dict):
            smart["temperature"]["current"] = log[field]
    try:
        return validate_replica(out)
    except (InvalidReplica, TypeError, KeyError) as err:
        raise ReplicaError("invalid_replica", str(err) or "not a replica file") from None
