"""Build the integration's seed replica set from tools/replicas with the
unchanged mock agent: each seed is a mock file, optionally with one of its
scenarios applied, exported as a complete snapshot (no scenario block)."""

import copy
import importlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2])
sys.path.insert(0, str(ROOT))
import tests.conftest  # noqa: E402,F401

panel_view = importlib.import_module("custom_components.smart_sniffer.panel_view")
devstat = importlib.import_module("custom_components.smart_sniffer.devstat")

spec = importlib.util.spec_from_file_location("mock", ROOT / "tools" / "mock-agent.py")
mock = importlib.util.module_from_spec(spec)
sys.modules["mock"] = mock
spec.loader.exec_module(mock)
mock.library = mock.ReplicaLibrary(mock.DEFAULT_REPLICA_DIRS, None)

AT = "2026-10-18T00:00:00Z"
RULES = "0.9.0"

SEEDS = [
    # stem, source file id, scenario or None, name, description
    ("sata-hdd-healthy", "shipped.sata-hdd-healthy", None, "SATA HDD, healthy",
     "A Seagate Barracuda 2TB with nothing wrong."),
    ("sata-hdd-pending-sectors", "shipped.sata-hdd-healthy", "pending_sectors",
     "SATA HDD, pending sectors",
     "A Seagate Barracuda 2TB with 3 sectors waiting to be reallocated."),
    ("sata-hdd-reallocated", "shipped.sata-hdd-reallocated", None,
     "SATA HDD, reallocated sectors",
     "A Seagate Barracuda 2TB with 8 reallocated sectors and 2 reallocation events."),
    ("sata-hdd-smart-failed", "shipped.smart-failed", None, "SATA HDD, SMART failed",
     "A drive whose own SMART self-assessment failed, with reallocated and pending sectors."),
    ("sata-hdd-devstat-uncorrectables", "shipped.sata-hdd-devstat-uncorrectables", None,
     "SATA HDD, uncorrectables in Device Statistics",
     "A drive whose attribute table is clean but whose Device Statistics count reported "
     "uncorrectable errors."),
    ("sata-ssd-healthy", "shipped.sata-ssd", None, "SATA SSD, healthy",
     "A SATA SSD with little wear."),
    ("nvme-healthy", "shipped.nvme-wear", "healthy", "NVMe, healthy",
     "An NVMe SSD with nothing wrong."),
    ("nvme-wear", "shipped.nvme-wear", None, "NVMe, worn",
     "An NVMe SSD low on spare blocks."),
    ("zfs-pool-healthy", "shipped.zfs-pool-degraded", "healthy", "ZFS pool, healthy",
     "A two-disk ZFS mirror, both disks online."),
    ("zfs-pool-degraded", "shipped.zfs-pool-degraded", None, "ZFS pool, degraded",
     "A two-disk ZFS mirror with one disk faulted."),
    ("usb-smart-blocked", "shipped.usb-blocked", None, "USB drive, SMART blocked",
     "A drive behind a USB bridge that does not pass SMART through."),
]


# What the agent's smartctl -a gives about the drive itself (firmware,
# capacity, the SATA link), by model. Made up for the seeds; nothing here
# comes from a real drive.
IDENTITY = {
    "Seagate Barracuda ST2000DM008": {"firmware_version": "0001", "blocks": 3907029168, "sata": True},
    "WDC  WUH721414ALE604": {"firmware_version": "LDGNW400", "blocks": 27344764928, "sata": True},
    "Samsung SSD 870 EVO 500GB": {"firmware_version": "SVT02B6Q", "blocks": 976773168, "sata": True},
    "Samsung SSD 980 PRO 1TB": {"firmware_version": "5B2QGXA7", "blocks": 1953525168, "nvme": True},
}

# Device Statistics for the SATA seeds: every page a drive of the kind has
# (a hard disk: 1, 3, 4, 5, 6; an SSD: 1, 4, 5, 6, 7), worked out from the
# seed's own SMART data so the two agree (power-on hours, power cycles,
# sectors written and read, reallocated and pending sectors, uncorrectables,
# temperature, CRC errors). Entries a source file already has keep its values.
# The SSD's endurance used matches its wear attribute.
DEVSTAT_SEEDS = {
    "sata-hdd-healthy": {"kind": "hdd"},
    "sata-hdd-pending-sectors": {"kind": "hdd"},
    "sata-hdd-reallocated": {"kind": "hdd", "read_recovery": 6},
    "sata-hdd-smart-failed": {"kind": "hdd", "read_recovery": 412, "read_factor": 1.6},
    "sata-hdd-devstat-uncorrectables": {"kind": "hdd", "read_recovery": 37},
    "sata-ssd-healthy": {"kind": "ssd", "read_factor": 1.4, "wear_used": 1},
}

_PAGE_NAMES = {1: "General Statistics", 3: "Rotating Media Statistics", 4: "General Errors Statistics",
               5: "Temperature Statistics", 6: "Transport Statistics", 7: "Solid State Device Statistics"}
_ENTRY = {  # (page, offset): (smartctl's name, size)
    (1, 8): ("Lifetime Power-On Resets", 4), (1, 16): ("Power-on Hours", 4),
    (1, 24): ("Logical Sectors Written", 6), (1, 32): ("Number of Write Commands", 6),
    (1, 40): ("Logical Sectors Read", 6), (1, 48): ("Number of Read Commands", 6),
    (3, 8): ("Spindle Motor Power-on Hours", 4), (3, 16): ("Head Flying Hours", 4),
    (3, 24): ("Head Load Events", 4), (3, 32): ("Number of Reallocated Logical Sectors", 4),
    (3, 40): ("Read Recovery Attempts", 4), (3, 48): ("Number of Mechanical Start Failures", 4),
    (3, 56): ("Number of Realloc. Candidate Logical Sectors", 4),
    (4, 8): ("Number of Reported Uncorrectable Errors", 4),
    (4, 16): ("Resets Between Cmd Acceptance and Completion", 4),
    (5, 8): ("Current Temperature", 1), (5, 16): ("Average Short Term Temperature", 1),
    (5, 24): ("Average Long Term Temperature", 1), (5, 32): ("Highest Temperature", 1),
    (5, 40): ("Lowest Temperature", 1), (5, 80): ("Time in Over-Temperature", 4),
    (5, 88): ("Specified Maximum Operating Temperature", 1), (5, 96): ("Time in Under-Temperature", 4),
    (5, 104): ("Specified Minimum Operating Temperature", 1),
    (6, 8): ("Number of Hardware Resets", 4), (6, 16): ("Number of ASR Events", 4),
    (6, 24): ("Number of Interface CRC Errors", 4),
    (7, 8): ("Percentage Used Endurance Indicator", 1),
}


def _raw(smart, attr_id):
    for a in (smart.get("ata_smart_attributes") or {}).get("table") or []:
        if a.get("id") == attr_id:
            match = __import__("re").match(r"\s*(\d+)", str((a.get("raw") or {}).get("string", "")))
            return int(match.group(1)) if match else (a.get("raw") or {}).get("value")
    return None


def seed_devstat(doc, spec):
    """The devstat part for a SATA seed (see DEVSTAT_SEEDS)."""
    smart = doc["smart"]
    hours = (smart.get("power_on_time") or {}).get("hours") or _raw(smart, 9) or 0
    cycles = smart.get("power_cycle_count") or _raw(smart, 12) or 0
    temp = (smart.get("temperature") or {}).get("current") or _raw(smart, 194) or 30
    written = _raw(smart, 241) or 0
    read = _raw(smart, 242) or int(written * spec.get("read_factor", 1.5))
    hdd = spec["kind"] == "hdd"
    values = {
        (1, 8): cycles, (1, 16): hours, (1, 24): written, (1, 32): written // 64,
        (1, 40): read, (1, 48): read // 96,
        (4, 8): _raw(smart, 187) or 0, (4, 16): 0,
        (5, 8): temp, (5, 32): temp + (16 if hdd else 24), (5, 40): 18 if hdd else 17,
        (5, 80): 0, (5, 88): 60 if hdd else 70, (5, 96): 0, (5, 104): 5 if hdd else 0,
        (6, 8): cycles + 3, (6, 16): 0, (6, 24): _raw(smart, 199) or 0,
    }
    if hdd:
        values.update({(3, 8): hours - hours // 50, (3, 16): hours - hours // 14,
                       (3, 24): _raw(smart, 193) or cycles * 9, (3, 32): _raw(smart, 5) or 0,
                       (3, 40): spec.get("read_recovery", 0), (3, 48): 0, (3, 56): _raw(smart, 197) or 0,
                       (5, 16): temp - 1, (5, 24): temp - 2})
    else:
        values[(7, 8)] = spec["wear_used"]
    # A source file's own entries keep their values.
    old = doc.get("devstat") or {}
    for page in old.get("pages") or []:
        for e in page.get("table") or []:
            values[(page["number"], e["offset"])] = e["value"]
    pages = {}
    for (number, offset), value in sorted(values.items()):
        name, size = _ENTRY[(number, offset)]
        normalized = number == 7
        flags = {"value": 224 if normalized else 192, "string": "VN-- " if normalized else "V--- ",
                 "valid": True, "normalized": normalized, "supports_dsn": False,
                 "monitored_condition_met": False}
        pages.setdefault(number, []).append({"offset": offset, "name": name, "size": size, "value": value,
                                             "flags": flags})
    return {"status": "present", "complete": True, "logical_block_size": 512,
            "pages": [{"number": n, "name": _PAGE_NAMES[n], "revision": 1, "table": t}
                      for n, t in sorted(pages.items())]}


def seed_identity(doc):
    """Firmware, capacity and the SATA link in the seed's SMART data."""
    ident = IDENTITY.get(doc["meta"]["model"])
    if not ident:
        return
    smart = doc["smart"]
    smart.setdefault("model_name", doc["meta"]["model"])
    smart.setdefault("firmware_version", ident["firmware_version"])
    smart.setdefault("user_capacity", {"blocks": ident["blocks"], "bytes": ident["blocks"] * 512})
    if ident.get("nvme"):
        smart.setdefault("nvme_total_capacity", ident["blocks"] * 512)
    if ident.get("sata"):
        speed = {"sata_value": 3, "string": "6.0 Gb/s", "units_per_second": 60, "bits_per_unit": 100000000}
        smart.setdefault("interface_speed", {"max": dict(speed, sata_value=14), "current": speed})


def verdict_of(doc):
    if doc["kind"] == "zfs_pool":
        pool = mock.pool_payload(doc["pool"])
        v = panel_view.pool_verdict(pool)
        return {"state": v["state"], "severity": "critical" if v["state"] == "YES" else "none",
                "reasons": v.get("problems") or [], "rules_version": RULES, "at": AT}
    drive = {"id": "x", "model": doc["meta"]["model"], "serial": doc["meta"]["serial"],
             "protocol": doc["meta"]["protocol"], "device_path": doc["meta"]["device_path"],
             "smart_data": copy.deepcopy(doc["smart"]),
             "device_statistics": mock.devstat_block({"_devstat": mock._natural_from_file(doc["devstat"])})}
    drive[devstat.DEVSTAT_KEY], _ = devstat.merge_devstat(drive, None)
    v = panel_view.evaluate(drive, {}, RULES)
    return {"state": v["state"], "severity": v["severity"], "reasons": v["reasons"],
            "rules_version": RULES, "at": AT}


OUT.mkdir(parents=True, exist_ok=True)
for order, (stem, file_id, scenario, name, description) in enumerate(SEEDS, start=1):
    store = mock.DriveStore()
    added = store.add_replica(file_id)
    kind = added["kind"]
    if scenario:
        store.apply_scenario(kind, added["id"], scenario)
    doc = store.export(kind, added["id"])
    doc.pop("scenario", None)
    doc.pop("preset", None)
    if kind == "pool":
        doc["pool"].pop("vanished", None)
        # The scrub time back as the source file had it (the mock shifts it to now).
        src = mock.library.find(file_id)["doc"]
        doc["pool"]["last_scrub_end"] = src["pool"].get("last_scrub_end")
    if kind == "drive":
        seed_identity(doc)
    if stem in DEVSTAT_SEEDS:
        spec = DEVSTAT_SEEDS[stem]
        if spec.get("wear_used") is not None:
            for a in doc["smart"]["ata_smart_attributes"]["table"]:
                if a["name"] == "Wear_Leveling_Count":
                    a["value"] = a["worst"] = 100 - spec["wear_used"]
        doc["devstat"] = seed_devstat(doc, spec)
        doc["kind"] = "ata_devstat"
    doc["name"] = name
    doc["description"] = description
    doc["origin"] = {"type": "shipped"}
    doc["order"] = order
    doc = {k: doc[k] for k in ("format", "version", "name", "kind", "description", "origin", "order")
           if k in doc} | {k: v for k, v in doc.items() if k not in
                          ("format", "version", "name", "kind", "description", "origin", "order")}
    doc["verdict"] = verdict_of(doc)
    mock.validate_replica(doc)
    (OUT / f"{stem}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(stem, doc["kind"], doc["verdict"]["state"], doc["verdict"]["reasons"])
