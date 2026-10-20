"""Diagnostics support for SMART Sniffer.

Provides a downloadable JSON snapshot of the integration's cached data for
troubleshooting and bug reports. Accessible from the integration's page in
Settings → Integrations → SMART Sniffer → three-dot menu → Download Diagnostics.

Sensitive fields (serial numbers, WWNs and other drive identifiers, tokens)
are automatically redacted. Drives are keyed drive_1, drive_2, ... in the
agent's order, not by their id, which is a slug of the serial.
"""

from __future__ import annotations

import copy
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .agent_identity import ENTRY_AGENT_DRIVES
from .attention import evaluate_attention, get_thresholds
from .const import CONF_TOKEN, DOMAIN
from .coordinator import SmartSnifferCoordinator
from .pool_health import pool_problems, reported_missing, reported_pools
from .source import is_replica_data

# Keys to redact from config entry data (connection secrets, and the digest
# of the drive serials the coordinator keeps for rediscovery).
CONFIG_REDACT_KEYS = {CONF_TOKEN, ENTRY_AGENT_DRIVES}

# Keys to redact from per-drive SMART data (personally identifiable).
DRIVE_REDACT_KEYS = {"serial", "serial_number", "firmware_version"}

# Keys inside smart_data JSON that carry a drive's serial or world-wide
# identifiers: the WWN, the SCSI logical unit id and the NVMe IEEE OUI.
SMART_DATA_REDACT_KEYS = {"serial_number", "wwn", "logical_unit_id", "nvme_ieee_oui_identifier"}

# Keys in each entry of smart_data's nvme_namespaces (per-namespace ids).
NAMESPACE_REDACT_KEYS = {"eui64", "nguid"}

REDACTED = "**REDACTED**"


def _redact_smart_data(
    smart_data: dict[str, Any], identifiers: set[str] | None = None
) -> dict[str, Any]:
    """Deep-redact sensitive fields from the raw SMART JSON payload.

    ``identifiers`` are also replaced wherever else they appear in a string,
    such as a /dev/disk/by-id path in info_name or smartctl's argv.
    """
    redacted = copy.deepcopy(smart_data)

    # Top-level serial and world-wide ids in smartctl output
    for key in SMART_DATA_REDACT_KEYS:
        if key in redacted:
            redacted[key] = REDACTED

    namespaces = redacted.get("nvme_namespaces")
    if isinstance(namespaces, list):
        for ns in namespaces:
            if isinstance(ns, dict):
                for key in NAMESPACE_REDACT_KEYS:
                    if key in ns:
                        ns[key] = REDACTED

    # Device sub-dict may also contain serial
    device = redacted.get("device", {})
    if isinstance(device, dict):
        for key in ("serial_number", "name"):
            if key in device:
                device[key] = REDACTED

    for value in sorted(identifiers or (), key=len, reverse=True):
        redacted = _replace_in_strings(redacted, value)
    return redacted


def _replace_in_strings(obj: Any, old: str) -> Any:
    """``obj`` with ``old`` replaced by REDACTED in every string value."""
    if isinstance(obj, str):
        return obj.replace(old, REDACTED)
    if isinstance(obj, list):
        return [_replace_in_strings(v, old) for v in obj]
    if isinstance(obj, dict):
        return {k: _replace_in_strings(v, old) for k, v in obj.items()}
    return obj


def _drive_identifiers(drive_id: str, drive_data: dict[str, Any]) -> set[str]:
    """A drive's serial (as the agent and smartctl gave it), its id, and its
    WWN as smartctl prints it in text, in the cases they may appear in."""
    smart = drive_data.get("smart_data")
    found = {drive_id, drive_data.get("serial")}
    if isinstance(smart, dict):
        found.add(smart.get("serial_number"))
        wwn = smart.get("wwn")
        if isinstance(wwn, dict):
            try:
                found.add(f"{int(wwn['naa']):x}{int(wwn['oui']):06x}{int(wwn['id']):09x}")
            except (KeyError, TypeError, ValueError):
                pass
    values = {s.strip() for s in found if isinstance(s, str)}
    # Short values would match inside unrelated strings; their own keys are
    # redacted all the same.
    return {v for s in values if len(s) >= 6 for v in (s, s.lower(), s.upper())}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics data for a SMART Sniffer config entry.

    Includes:
      - Redacted config entry data (host, port, interval — no token).
      - Integration version from manifest.
      - Per-drive summary: model, protocol, attention state, reasons.
      - Full (redacted) SMART data per drive for deep debugging.
      - ZFS pools as the agent reported them, with the problem reasons the
        integration derived (None when the last pool fetch failed), and the
        registered pools that fetch left out. Pool names and zpool's status
        text carry nothing identifying.
    """
    coordinator: SmartSnifferCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # Diagnostics keys for the drives: drive_1, drive_2, ... in the agent's
    # order. The drive id is a slug of the serial, so it is never a key here.
    labels = {
        drive_id: f"drive_{n}"
        for n, drive_id in enumerate(
            (k for k in coordinator.data if not k.startswith("_")), start=1
        )
    }

    # Redact config entry. Threshold overrides are keyed by drive id: they
    # are listed under the drive's diagnostics key, or as other_1, ... for
    # a drive the agent no longer reports.
    config_data = async_redact_data(dict(entry.data), CONFIG_REDACT_KEYS)
    if isinstance(config_data.get("thresholds"), dict):
        others = (f"other_{n}" for n in range(1, len(config_data["thresholds"]) + 1))
        config_data["thresholds"] = {
            labels.get(drive_id) or next(others): values
            for drive_id, values in config_data["thresholds"].items()
        }
    # The Replica has no address: its placeholder host is left out (v0.9).
    if is_replica_data(entry.data):
        config_data = {k: v for k, v in config_data.items() if k not in ("host", "port")}

    # Build per-drive diagnostics.
    drives_diag: dict[str, Any] = {}
    for drive_id, drive_data in coordinator.data.items():
        if drive_id.startswith("_"):
            continue  # skip internal keys like _filesystems
        # Attention evaluation for this drive.
        state, severity, reasons, accepted = evaluate_attention(
            drive_data, get_thresholds(entry, drive_id)
        )

        # Redact the top-level drive fields.
        drive_summary = async_redact_data(
            {
                "model":       drive_data.get("model"),
                "serial":      drive_data.get("serial"),
                "protocol":    drive_data.get("protocol"),
                "device":      drive_data.get("device"),
                "device_path": drive_data.get("device_path"),
            },
            DRIVE_REDACT_KEYS,
        )

        # Redact the full SMART data payload.
        raw_smart = drive_data.get("smart_data", {})
        if isinstance(raw_smart, dict):
            redacted_smart = _redact_smart_data(raw_smart, _drive_identifiers(drive_id, drive_data))
        else:
            redacted_smart = "**UNPARSEABLE**"

        drives_diag[labels[drive_id]] = {
            "summary": drive_summary,
            "attention": {
                "state": state,
                "severity": severity,
                "reasons": reasons,
                "accepted": accepted,
                "thresholds": get_thresholds(entry, drive_id),
            },
            "smart_data": redacted_smart,
            # v0.8.0 agents; absent from older ones. The pages carry no
            # identifiers (the -i identity keys are not published).
            "device_statistics": drive_data.get("device_statistics"),
            "derived": drive_data.get("derived"),
        }

    return {
        "kind": "replica" if is_replica_data(entry.data) else "agent",
        "config_entry": config_data,
        "coordinator": {
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "last_update_success": coordinator.last_update_success,
            "drive_count": sum(1 for k in coordinator.data if not k.startswith("_")),
        },
        "drives": drives_diag,
        "pools": _pools_diagnostics(coordinator.data),
        "pools_missing": (
            reported_missing(coordinator.data) if reported_pools(coordinator.data) is not None else None
        ),
    }


def _pools_diagnostics(data: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    pools = reported_pools(data)
    if pools is None:
        return None
    return [{**pool, "problems": pool_problems(pool)} for pool in pools]
