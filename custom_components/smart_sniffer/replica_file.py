"""A drive as a replica file: the card download of the SMART Sniffer panel.

The Control Center's download icon on a drive card gives the drive as a
replica file, serial and WWN replaced, as the HA App 0.3.1 did through its
replica agent. These are that agent's own functions, copied unchanged from
tools/mock-agent.py (replica_from_drive, validate_replica and what they
use); tests/test_panel_view.py checks the copy still matches the tool.

Stdlib only; nothing here imports Home Assistant.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

_DEVICE_STATES = ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED")

DEVSTAT_STATUSES = ("present", "absent", "unavailable", "not_applicable", "off")

FS_STATUSES = ("ok", "unavailable")

REPLICA_FORMAT = "smart-sniffer-replica"

REPLICA_VERSION = 1

REPLICA_KINDS = ("ata", "ata_devstat", "nvme", "scsi", "unsupported", "zfs_pool")

REPLICA_ORIGINS = ("shipped", "saved", "uploaded")

MAX_REPLICA_BYTES = 1024 * 1024          # 1 MB per file

_POOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")

_FILE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return _rfc3339(time.time())


class BadRequest(Exception):
    """A request the mock refuses, with the HTTP status to answer."""

    def __init__(self, error: str, code: int = 400, **extra: Any) -> None:
        super().__init__(error)
        self.code = code
        self.extra = extra


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


def _check_filesystems(doc: dict) -> None:
    """The optional filesystems list: the agent's items, on this drive or pool."""
    items = doc.get("filesystems")
    if items is None:
        return
    _need(isinstance(items, list), "filesystems must be a list")
    is_pool = doc["kind"] == "zfs_pool"
    root = doc["pool"]["name"] if is_pool else doc["meta"]["device_path"]
    where = f"{root} or {root}/<dataset>" if is_pool else f"{root or 'the drive'} or a partition of it"
    ids: set[str] = set()
    for fs in items:
        _need(isinstance(fs, dict), "each filesystem must be an object")
        for key in ("mountpoint", "device", "fstype"):
            _need(isinstance(fs.get(key), str) and fs[key] != "", f"filesystem {key} must be a non-empty string")
        for key in ("id", "uuid"):
            _need(fs.get(key) is None or isinstance(fs[key], str), f"filesystem {key} must be a string")
        dev = fs["device"]
        on_it = (dev == root or dev.startswith(root + "/")) if is_pool else (root != "" and dev.startswith(root))
        _need(on_it, f"filesystem {fs['mountpoint']}: device must be {where}")
        _need(_is_count(fs.get("total_bytes")) and _is_count(fs.get("used_bytes")),
              "filesystem total_bytes and used_bytes must be whole numbers")
        _need(fs["used_bytes"] <= fs["total_bytes"],
              f"filesystem {fs['mountpoint']}: used_bytes is more than total_bytes")
        _need(fs.get("status", "ok") in FS_STATUSES, f"filesystem status must be one of {', '.join(FS_STATUSES)}")
        if fs.get("id"):
            _need(fs["id"] not in ids, f"filesystem id {fs['id']} is listed twice")
            ids.add(fs["id"])


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
    _check_filesystems(doc)
    return doc


def _attr_table(smart: dict) -> list[dict]:
    table = (smart.get("ata_smart_attributes") or {}).get("table") or []
    return [a for a in table if isinstance(a, dict)]


def _protocol(drive: dict) -> str:
    device = (drive.get("smart_data") or {}).get("device")
    if isinstance(device, dict) and device.get("protocol"):
        return str(device["protocol"])
    return str(drive.get("protocol") or "")


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


def _fake_like(value: Any) -> Any:
    """A fixed fake value of the same shape: zeros for numbers and hex strings."""
    if isinstance(value, dict):
        return {k: _fake_like(v) for k, v in value.items()}
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return 0
    if isinstance(value, str):
        return re.sub(r"[0-9A-Za-z]", "0", value)
    return None


def _replace_strings(obj: Any, old: str, new: str, whole: bool = False) -> Any:
    """Every string in obj (keys included) with old replaced by new.

    whole: replace only strings equal to old, for values short enough to
    turn up inside unrelated text.
    """
    if isinstance(obj, str):
        if whole:
            return new if obj == old else obj
        return obj.replace(old, new) if old in obj else obj
    if isinstance(obj, list):
        return [_replace_strings(v, old, new, whole) for v in obj]
    if isinstance(obj, dict):
        return {_replace_strings(k, old, new, whole): _replace_strings(v, old, new, whole)
                for k, v in obj.items()}
    return obj


def _wwn_text(wwn: Any) -> str | None:
    """The WWN as smartctl prints it in text (naa, oui, id run together in hex)."""
    if not isinstance(wwn, dict):
        return None
    try:
        return f"{int(wwn['naa']):x}{int(wwn['oui']):06x}{int(wwn['id']):09x}"
    except (KeyError, TypeError, ValueError):
        return None


def _file_devstat(block: Any, protocol: str) -> dict:
    """The devstat part of a file from an agent's device_statistics block."""
    if not isinstance(block, dict) or block.get("status") not in DEVSTAT_STATUSES:
        return {"status": "absent" if protocol.upper() == "ATA" else "not_applicable"}
    out = {k: copy.deepcopy(block[k]) for k in ("status", "reason") if k in block}
    if block["status"] == "present":
        out["complete"] = bool(block.get("complete", True))
        out["logical_block_size"] = block.get("logical_block_size", 512)
        out["pages"] = copy.deepcopy(block.get("pages") or [])
    return out


def _file_verdict(verdict: Any) -> dict | None:
    if not isinstance(verdict, dict):
        return None
    out = {k: copy.deepcopy(verdict[k]) for k in ("state", "severity", "reasons", "rules_version", "at")
           if k in verdict}
    out.setdefault("at", _now_iso())
    return out


def replica_from_drive(payload: Any, verdict: Any = None) -> dict:
    """A user replica file from a real drive's /api/drives/{id} payload.

    The serial becomes REPLICA- plus 8 random hex digits, everywhere it
    appears; the WWN, the NVMe IEEE OUI and each namespace's EUI-64 and NGUID
    become fixed fake values; smartctl's argv and local_time are dropped.
    Model, firmware and every counter stay.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("smart_data"), dict):
        raise BadRequest("bad_request", 400, message="from_drive must be a drive payload with smart_data")
    smart = copy.deepcopy(payload["smart_data"])
    protocol = str(payload.get("protocol") or _protocol(payload) or "")
    devstat = _file_devstat(payload.get("device_statistics"), protocol)
    kind = drive_kind(smart, devstat, protocol)
    if kind == "unsupported":
        raise BadRequest("unsupported_drive", 409, message="This drive gives no SMART data to copy.")

    old_serials = {s for s in (payload.get("serial"), smart.get("serial_number"))
                   if isinstance(s, str) and s.strip()}
    old_wwn = _wwn_text(smart.get("wwn"))
    new_serial = "REPLICA-" + uuid.uuid4().hex[:8].upper()

    smart.pop("local_time", None)
    if isinstance(smart.get("smartctl"), dict):
        smart["smartctl"].pop("argv", None)
    if "wwn" in smart:
        smart["wwn"] = {"naa": 5, "oui": 0, "id": int(uuid.uuid4().hex[:9], 16)}
    if "nvme_ieee_oui_identifier" in smart:
        smart["nvme_ieee_oui_identifier"] = _fake_like(smart["nvme_ieee_oui_identifier"])
    if "logical_unit_id" in smart:
        smart["logical_unit_id"] = _fake_like(smart["logical_unit_id"])
    for ns in smart.get("nvme_namespaces") or []:
        if isinstance(ns, dict):
            for key in ("eui64", "nguid"):
                if key in ns:
                    ns[key] = _fake_like(ns[key])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    model = str(payload.get("model") or smart.get("model_name") or "Drive")
    doc: dict[str, Any] = {
        "format": REPLICA_FORMAT,
        "version": REPLICA_VERSION,
        "name": f"{model.strip()}, saved {today}",
        "kind": kind,
        "description": f"Saved from a real drive on {today}. Serial and WWN replaced.",
        "origin": {"type": "saved", "saved_at": _now_iso()},
        "meta": {"model": model, "serial": new_serial, "protocol": protocol,
                 "device_path": str(payload.get("device_path") or "")},
        "smart": smart,
        "devstat": devstat,
    }
    file_verdict = _file_verdict(verdict)
    if file_verdict is not None:
        doc["verdict"] = file_verdict
    for old in sorted(old_serials, key=len, reverse=True):
        for variant in sorted({old, old.strip(), old.lower(), old.upper()}):
            doc = _replace_strings(doc, variant, new_serial, whole=len(variant) < 6)
    if old_wwn:
        for variant in (old_wwn, old_wwn.upper()):
            doc = _replace_strings(doc, variant, "5" + "0" * (len(old_wwn) - 1))
    if "serial_number" in doc["smart"]:
        doc["smart"]["serial_number"] = new_serial
    doc["meta"]["serial"] = new_serial
    return validate_replica(doc)



def file_name(doc: dict) -> str:
    """The download's file name, as the replica agent named it."""
    return f"{_file_slug(str(doc.get('name') or 'replica'))}.json"
