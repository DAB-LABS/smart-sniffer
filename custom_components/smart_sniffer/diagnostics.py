"""Diagnostics support for SMART Sniffer.

Provides a downloadable JSON snapshot of the integration's cached data for
troubleshooting and bug reports. Accessible from the integration's page in
Settings → Integrations → SMART Sniffer → three-dot menu → Download Diagnostics.

Sensitive fields (serial numbers, tokens) are automatically redacted.
"""

from __future__ import annotations

import copy
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .attention import evaluate_attention, get_thresholds
from .const import CONF_TOKEN, DOMAIN
from .coordinator import SmartSnifferCoordinator
from .pool_health import pool_problems, reported_missing, reported_pools

# Keys to redact from config entry data (connection secrets).
CONFIG_REDACT_KEYS = {CONF_TOKEN}

# Keys to redact from per-drive SMART data (personally identifiable).
DRIVE_REDACT_KEYS = {"serial", "serial_number", "firmware_version"}

# Keys inside smart_data JSON that may contain serial numbers.
SMART_DATA_REDACT_KEYS = {"serial_number"}


def _redact_smart_data(smart_data: dict[str, Any]) -> dict[str, Any]:
    """Deep-redact sensitive fields from the raw SMART JSON payload."""
    redacted = copy.deepcopy(smart_data)

    # Top-level serial in smartctl output
    for key in SMART_DATA_REDACT_KEYS:
        if key in redacted:
            redacted[key] = "**REDACTED**"

    # Device sub-dict may also contain serial
    device = redacted.get("device", {})
    if isinstance(device, dict):
        for key in ("serial_number", "name"):
            if key in device:
                device[key] = "**REDACTED**"

    return redacted


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

    # Redact config entry.
    config_data = async_redact_data(dict(entry.data), CONFIG_REDACT_KEYS)

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
                "model":    drive_data.get("model"),
                "serial":   drive_data.get("serial"),
                "protocol": drive_data.get("protocol"),
                "device":   drive_data.get("device"),
            },
            DRIVE_REDACT_KEYS,
        )

        # Redact the full SMART data payload.
        raw_smart = drive_data.get("smart_data", {})
        if isinstance(raw_smart, dict):
            redacted_smart = _redact_smart_data(raw_smart)
        else:
            redacted_smart = "**UNPARSEABLE**"

        drives_diag[drive_id] = {
            "summary": drive_summary,
            "attention": {
                "state": state,
                "severity": severity,
                "reasons": reasons,
                "accepted": accepted,
                "thresholds": get_thresholds(entry, drive_id),
            },
            "smart_data": redacted_smart,
        }

    return {
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
