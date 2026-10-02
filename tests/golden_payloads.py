"""Coordinator payloads behind the v0.7.0 entity goldens (plan v3, 7.6).

One payload holds every fixture in tests/fixtures and tests/fixtures/devstat as
a drive, as an agent older than v0.8.0 would report it (no device_statistics,
no derived), plus the cases setup has rules for: an unreadable drive, a drive
in standby, two filesystems and five ZFS pools, one of them registered but
missing.

The goldens in fixtures/golden were written once from the v0.7.0 platform
setup (base 36ebe65) under Home Assistant 2024.4.0 by a throwaway script; the
tests compare the v0.8.0 code against them. Nothing here imports Home
Assistant.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEVSTAT = FIXTURES / "devstat"
GOLDEN = FIXTURES / "golden"
ENTRY_ID = "01KRS6Q8KDEN7SQJFHWCZAQ6EM"
REGISTERED_POOLS = ["oldpool"]


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def smart_data_sources() -> dict[str, dict[str, Any]]:
    """Fixture name -> smartctl -a JSON, for every drive fixture."""
    out = {p.stem: _read(p) for p in sorted(FIXTURES.glob("*.json"))}
    for path in sorted(DEVSTAT.glob("*.a.json")):
        out[path.name[: -len(".a.json")]] = _read(path)
    return out


def drive_payload(name: str, smart_data: dict[str, Any]) -> dict[str, Any]:
    """One drive as the agent's /api/drives/{id} returns it (v0.7.0 fields)."""
    device = smart_data.get("device") or {}
    return {
        "id": f"fx-{name.replace('_', '-')}",
        "device_path": "/dev/fixture",
        "model": smart_data.get("model_name", "Unknown Drive"),
        "serial": smart_data.get("serial_number", ""),
        "protocol": device.get("protocol", ""),
        "readable": True,
        "last_updated": "2026-10-01T12:00:00Z",
        "smart_data": copy.deepcopy(smart_data),
    }


def golden_payload() -> dict[str, Any]:
    """The whole coordinator payload the goldens were captured from."""
    data: dict[str, Any] = {}
    for name, smart_data in smart_data_sources().items():
        drive = drive_payload(name, smart_data)
        data[drive["id"]] = drive

    asleep = drive_payload("standby", smart_data_sources()["ata_healthy"])
    asleep["id"] = "fx-standby"
    asleep["serial"] = "FIXTURE-standby"
    asleep["in_standby"] = True
    data[asleep["id"]] = asleep

    unreadable = drive_payload("unreadable", smart_data_sources()["ata_healthy"])
    unreadable["id"] = "fx-unreadable"
    unreadable["readable"] = False
    data[unreadable["id"]] = unreadable

    data["_filesystems"] = [
        {
            "id": "fs-root", "mountpoint": "/", "device": "/dev/fixture",
            "fstype": "ext4", "total_bytes": 100 * 1024**3,
            "used_bytes": 41 * 1024**3, "available_bytes": 59 * 1024**3,
            "use_percent": 41.0, "status": "ok",
        },
        {
            "id": "fs-data", "mountpoint": "/data", "device": "/dev/fixture",
            "fstype": "xfs", "total_bytes": 0, "used_bytes": 0,
            "available_bytes": 0, "use_percent": None, "status": "error",
        },
    ]
    data["_pools"] = _read(FIXTURES / "pools" / "gh50-three-pools.json") + _read(
        FIXTURES / "pools" / "degraded-faulted.json"
    )
    data["_pools_missing"] = list(REGISTERED_POOLS)
    return data
