"""Agent payloads with the v0.8.0 fields, built from fixtures/devstat.

The fixtures are whitelisted extracts of the GH #56 corpus (plan v3, 7.7):
``<name>.a.json`` holds the keys the code reads from the ``-a`` call, and
``<name>.devstat.json`` the ``-i -l devstat`` call's identity keys and the
pages pruned to the entries the agent and integration read. Serials are
FIXTURE-<slot>; no WWN, firmware or path survives.

The agent shapes ``device_statistics`` and ``derived`` from them as plan 7.3
shows. ``derived`` is the agent's arithmetic, restated here only for these
fixtures (value x logical block size, NVMe units x 512000, sectors x 512); the
integration never computes it.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from tests.golden_payloads import DEVSTAT, drive_payload

# Plan 7.3 / 7.7 figures, in bytes.
HGST_WRITES = 163_663_499_752_448
HGST_READS = 184_215_110_001_152
WDC_UNC_WRITES = 107_830_377_472_000
SAMSUNG850_WRITES = 105_959_765_530_112
SABRENT_WRITES = 206_177_714_688_000


def read_fixture(name: str, part: str) -> dict[str, Any]:
    with (DEVSTAT / f"{name}.{part}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def pages_of(name: str) -> list[dict[str, Any]]:
    return copy.deepcopy(read_fixture(name, "devstat")["ata_device_statistics"]["pages"])


def entry_value(pages: list[dict[str, Any]], page: int, offset: int) -> int | None:
    for p in pages:
        if p["number"] == page:
            for e in p["table"]:
                if e["offset"] == offset:
                    return e.get("value")
    return None


def devstat_volume(name: str, offset: int) -> dict[str, Any]:
    stats = read_fixture(name, "devstat")
    value = entry_value(stats["ata_device_statistics"]["pages"], 1, offset)
    return {"bytes": value * stats["logical_block_size"], "source": "ata_device_statistics"}


def agent_drive(
    name: str,
    *,
    status: str | None = "present",
    complete: bool = True,
    pages: list[dict[str, Any]] | None = None,
    derived: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """One drive as a v0.8.0 agent reports it. ``status`` None: an older agent."""
    drive = drive_payload(name, read_fixture(name, "a"))
    if status is None:
        return drive
    block: dict[str, Any] = {"status": status}
    if reason is not None:
        block["reason"] = reason
    if status == "present":
        block.update(
            complete=complete,
            exit_status=0 if complete else 4,
            logical_block_size=read_fixture(name, "a").get("logical_block_size", 512),
            pages=pages if pages is not None else pages_of(name),
        )
    drive["device_statistics"] = block
    if derived is None:
        derived = default_derived(name, status)
    drive["derived"] = derived
    return drive


def default_derived(name: str, status: str) -> dict[str, Any]:
    if status == "present":
        return {
            "host_writes": devstat_volume(name, 0x018),
            "host_reads": devstat_volume(name, 0x028),
        }
    if status == "unavailable":
        return {
            "host_writes_omitted": "device_statistics_unavailable",
            "host_reads_omitted": "device_statistics_unavailable",
        }
    if name == "ata_no_devstat_samsung850":
        return {
            "host_writes": {
                "bytes": SAMSUNG850_WRITES,
                "source": "ata_attribute",
                "attribute_id": 241,
                "attribute_name": "Total_LBAs_Written",
            },
            "host_reads_omitted": "no_source",
        }
    if name == "nvme_sabrent":
        log = read_fixture(name, "a")["nvme_smart_health_information_log"]
        return {
            "host_writes": {"bytes": log["data_units_written"] * 512000, "source": "nvme"},
            "host_reads": {"bytes": log["data_units_read"] * 512000, "source": "nvme"},
        }
    return {"host_writes_omitted": "no_source", "host_reads_omitted": "no_source"}


def drop_entry(pages: list[dict[str, Any]], page: int, offset: int | None = None) -> None:
    """Remove a page (offset None) or one entry, in place: a partial read."""
    for p in list(pages):
        if p["number"] != page:
            continue
        if offset is None:
            pages.remove(p)
        else:
            p["table"] = [e for e in p["table"] if e["offset"] != offset]
