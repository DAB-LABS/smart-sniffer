#!/usr/bin/env python3
"""SMART Sniffer Mock Agent — a fake smartha-agent for integration testing.

Serves the same REST API as the real Go agent (/api/health, /api/drives,
/api/drives/{id}) but with fully controllable fake drive data.  A built-in
web dashboard lets you add/remove drives, change SMART attributes in real
time, and watch the HA integration react.

Usage:
    python3 mock-agent.py                       # port 9099, no auth
    python3 mock-agent.py --port 9100           # custom port
    python3 mock-agent.py --token mysecret      # enable bearer auth
    python3 mock-agent.py --no-mdns             # disable mDNS advertisement

Requirements: Python 3.9+ (stdlib only — no pip dependencies).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── Version ──────────────────────────────────────────────────────────────────
VERSION = "0.3.1-mock"

# ── Drive presets ────────────────────────────────────────────────────────────
# Each preset returns (drive_meta, smart_data) matching the real agent's JSON.

def _ata_attrs(attrs: list[dict]) -> dict:
    """Wrap a list of ATA attribute dicts in the smartctl-compatible structure."""
    return {"ata_smart_attributes": {"table": attrs}, "smart_status": {"passed": True}}


def _ata_attr(attr_id: int, name: str, raw_value: int) -> dict:
    return {"id": attr_id, "name": name, "value": 100, "worst": 100, "thresh": 0,
            "raw": {"value": raw_value, "string": str(raw_value)}}


def preset_sata_hdd() -> tuple[dict, dict]:
    """Seagate Barracuda 2TB — spinning rust with all ATA attributes."""
    meta = {"model": "Seagate Barracuda ST2000DM008", "serial": "WFL3MOCK01",
            "protocol": "ATA", "device_path": "/dev/sda"}
    smart = _ata_attrs([
        _ata_attr(5,   "Reallocated_Sector_Ct",     0),
        _ata_attr(10,  "Spin_Retry_Count",           0),
        _ata_attr(188, "Command_Timeout",            0),
        _ata_attr(196, "Reallocated_Event_Count",    0),
        _ata_attr(197, "Current_Pending_Sector",     0),
        _ata_attr(198, "Offline_Uncorrectable",      0),
        _ata_attr(194, "Temperature_Celsius",        36),
        _ata_attr(9,   "Power_On_Hours",             8760),
        _ata_attr(12,  "Power_Cycle_Count",          142),
    ])
    return meta, smart


def preset_sata_ssd() -> tuple[dict, dict]:
    """Samsung 870 EVO 500GB — SATA SSD with wear leveling."""
    meta = {"model": "Samsung SSD 870 EVO 500GB", "serial": "S4ENMOCK02",
            "protocol": "ATA", "device_path": "/dev/sdb"}
    smart = _ata_attrs([
        _ata_attr(5,   "Reallocated_Sector_Ct",     0),
        _ata_attr(196, "Reallocated_Event_Count",    0),
        _ata_attr(198, "Offline_Uncorrectable",      0),
        _ata_attr(177, "Wear_Leveling_Count",        2),
        _ata_attr(188, "Command_Timeout",            0),
        _ata_attr(194, "Temperature_Celsius",        31),
        _ata_attr(9,   "Power_On_Hours",             4200),
        _ata_attr(12,  "Power_Cycle_Count",          315),
    ])
    return meta, smart


def preset_nvme() -> tuple[dict, dict]:
    """Samsung 980 PRO 1TB — NVMe SSD."""
    meta = {"model": "Samsung 980 PRO 1TB", "serial": "S5GXMOCK03",
            "protocol": "NVMe", "device_path": "/dev/nvme0"}
    smart: dict[str, Any] = {
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "temperature": 38,
            "available_spare": 100,
            "available_spare_threshold": 10,
            "percentage_used": 3,
            "power_on_hours": 2100,
            "power_cycles": 87,
            "media_errors": 0,
        },
    }
    return meta, smart


def preset_nvme_usb_working() -> tuple[dict, dict]:
    """Sabrent NVMe USB-C enclosure — passthrough works."""
    meta = {"model": "Sabrent Rocket NVMe 500GB (USB)", "serial": "SB50MOCK04",
            "protocol": "NVMe", "device_path": "/dev/nvme1"}
    smart: dict[str, Any] = {
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "temperature": 42,
            "available_spare": 95,
            "available_spare_threshold": 10,
            "percentage_used": 8,
            "power_on_hours": 900,
            "power_cycles": 210,
            "media_errors": 0,
        },
    }
    return meta, smart


def preset_usb_blocked() -> tuple[dict, dict]:
    """WD Elements USB — SMART blocked by USB bridge chip."""
    meta = {"model": "WD Elements 2TB (USB)", "serial": "WX72MOCK05",
            "protocol": "ATA", "device_path": "/dev/sdc"}
    smart: dict[str, Any] = {}   # empty — triggers UNSUPPORTED
    return meta, smart


def preset_virtual_disk() -> tuple[dict, dict]:
    """QEMU VirtIO virtual disk — no real SMART data."""
    meta = {"model": "QEMU HARDDISK", "serial": "QM00MOCK06",
            "protocol": "ATA", "device_path": "/dev/vda"}
    smart: dict[str, Any] = {}   # empty — triggers UNSUPPORTED
    return meta, smart


def preset_sas_enterprise() -> tuple[dict, dict]:
    """Seagate Exos 10E2400 SAS — enterprise 2.5" 10K RPM."""
    meta = {"model": "Seagate Exos 10E2400 ST1200MM0129", "serial": "WFK0MOCK07",
            "protocol": "SCSI", "device_path": "/dev/sg0"}
    # SAS drives use SCSI log/mode pages, not ATA attributes.
    # smartctl returns data under different keys — currently UNSUPPORTED.
    smart: dict[str, Any] = {
        "smart_status": {"passed": True},
        "scsi_grown_defect_list": 0,
        "scsi_error_counter_log": {
            "read":  {"total_uncorrected_errors": 0},
            "write": {"total_uncorrected_errors": 0},
        },
    }
    return meta, smart


PRESETS: dict[str, tuple[str, callable]] = {
    "sata_hdd":         ("SATA HDD (Seagate Barracuda 2TB)",    preset_sata_hdd),
    "sata_ssd":         ("SATA SSD (Samsung 870 EVO 500GB)",    preset_sata_ssd),
    "nvme":             ("NVMe SSD (Samsung 980 PRO 1TB)",      preset_nvme),
    "nvme_usb":         ("NVMe USB-C Enclosure (Sabrent)",      preset_nvme_usb_working),
    "usb_blocked":      ("USB External — SMART Blocked (WD Elements)", preset_usb_blocked),
    "virtual_disk":     ("Virtual Disk (QEMU VirtIO)",          preset_virtual_disk),
    "sas_enterprise":   ("Enterprise SAS (Seagate Exos 10E2400)", preset_sas_enterprise),
}

# ── Drive store ──────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _make_slug(serial: str) -> str:
    slug = _SLUG_RE.sub("-", serial.lower()).strip("-")
    return slug or "unknown"


class DriveStore:
    """Thread-safe in-memory store of fake drives."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.drives: dict[str, dict[str, Any]] = {}   # keyed by slug id
        self.order: list[str] = []
        self._poll_count = 0
        self._last_poll: float | None = None

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

    def add_drive(self, preset_key: str) -> str:
        _, factory = PRESETS[preset_key]
        meta, smart = factory()

        # Make serial unique so we can add multiple of the same preset.
        suffix = uuid.uuid4().hex[:4].upper()
        meta["serial"] = meta["serial"][:-2] + suffix

        drive_id = _make_slug(meta["serial"])
        drive = {
            "id": drive_id,
            "device_path": meta["device_path"],
            "model": meta["model"],
            "serial": meta["serial"],
            "protocol": meta["protocol"],
            "smart_data": smart,
            "_preset": preset_key,
        }
        with self.lock:
            self.drives[drive_id] = drive
            self.order.append(drive_id)
        return drive_id

    def remove_drive(self, drive_id: str) -> bool:
        with self.lock:
            if drive_id in self.drives:
                del self.drives[drive_id]
                self.order = [d for d in self.order if d != drive_id]
                return True
        return False

    def update_smart(self, drive_id: str, updates: dict[str, Any]) -> bool:
        """Apply targeted updates to a drive's smart_data.

        For ATA drives, updates look like:
            {"Reallocated_Sector_Ct": 5, "Temperature_Celsius": 45, ...}

        For NVMe drives:
            {"critical_warning": 1, "available_spare": 5, ...}

        Special keys:
            "smart_passed": bool  — sets smart_status.passed
        """
        with self.lock:
            if drive_id not in self.drives:
                return False
            drive = self.drives[drive_id]
            smart = drive["smart_data"]

            # Handle smart_passed
            if "smart_passed" in updates:
                if "smart_status" not in smart:
                    smart["smart_status"] = {}
                smart["smart_status"]["passed"] = updates.pop("smart_passed")

            # NVMe path
            nvme_log = smart.get("nvme_smart_health_information_log")
            if nvme_log is not None:
                for key, val in updates.items():
                    if key in nvme_log:
                        nvme_log[key] = val

            # ATA path
            ata_table = smart.get("ata_smart_attributes", {}).get("table", [])
            if ata_table:
                for attr in ata_table:
                    if attr["name"] in updates:
                        attr["raw"]["value"] = updates[attr["name"]]
                        attr["raw"]["string"] = str(updates[attr["name"]])

            return True

    def get_summaries(self) -> list[dict]:
        with self.lock:
            return [
                {"id": self.drives[d]["id"],
                 "device_path": self.drives[d]["device_path"],
                 "model": self.drives[d]["model"],
                 "serial": self.drives[d]["serial"],
                 "protocol": self.drives[d]["protocol"]}
                for d in self.order if d in self.drives
            ]

    def get_drive(self, drive_id: str, include_internal: bool = False) -> dict | None:
        with self.lock:
            d = self.drives.get(drive_id)
            if d and not include_internal:
                return {k: v for k, v in d.items() if not k.startswith("_")}
            return d

    def get_all(self) -> list[dict]:
        with self.lock:
            return [self.drives[d] for d in self.order if d in self.drives]


# ── Global store ─────────────────────────────────────────────────────────────
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
  .drive-protocol { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.72rem;
                    font-weight: 600; text-transform: uppercase; margin-left: 8px; }
  .proto-ata { background: #1e3a5f; color: #60a5fa; }
  .proto-nvme { background: #1e3a2a; color: #4ade80; }
  .proto-scsi { background: #3a2a1e; color: #fb923c; }
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
<p class="subtitle">Fake smartha-agent for testing the HA integration. Change values → HA sees them next poll.</p>

<div class="status-bar">
  <div><span class="dot dot-green" id="server-dot"></span> Listening on port <strong id="port-display">9099</strong></div>
  <div>Auth: <strong id="auth-display">off</strong></div>
  <div>Drives: <strong id="drive-count">0</strong></div>
  <div>HA polls: <strong id="poll-count">0</strong></div>
  <div>Last poll: <span id="last-poll">—</span></div>
</div>

<div class="add-bar">
  <select id="preset-select">
    <!-- populated by JS -->
  </select>
  <button class="btn-primary" onclick="addDrive()">+ Add Drive</button>
</div>

<div id="drives-container">
  <div class="empty-state" id="empty-state">No drives configured. Add one above to get started.</div>
</div>

<script>
const PRESETS = PRESETS_JSON;

/* ── Attention thresholds (mirrors attention.py) ── */
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
      critical_warning: "≠0 → YES", media_errors: "≥1 → YES",
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
    Command_Timeout: "≥1 → MAYBE", Wear_Leveling_Count: "info only",
    Temperature_Celsius: "", Power_On_Hours: "", Power_Cycle_Count: "",
  };
  return hints[name] || "";
}

function predictState(drive) {
  const sd = drive.smart_data;
  if (!sd || (!sd.ata_smart_attributes && !sd.nvme_smart_health_information_log && !sd.smart_status)) {
    return "UNSUPPORTED";
  }
  const nvme = sd.nvme_smart_health_information_log;
  if (nvme) {
    if ((nvme.critical_warning || 0) !== 0) return "YES";
    if ((nvme.media_errors || 0) > 0) return "YES";
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
    if (ATA_WARNING.has(attr.name)) hasWarn = true;
  }
  if (hasCrit) return "YES";
  if (hasWarn) return "MAYBE";
  return "NO";
}

/* ── Populate preset dropdown ── */
const sel = document.getElementById("preset-select");
for (const [key, label] of Object.entries(PRESETS)) {
  const opt = document.createElement("option");
  opt.value = key;
  opt.textContent = label;
  sel.appendChild(opt);
}

/* ── API helpers ── */
async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch("/mock" + path, opts);
  return resp.json();
}

async function addDrive() {
  const preset = document.getElementById("preset-select").value;
  await api("/drives", "POST", { preset });
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

/* ── Render ── */
async function refresh() {
  const data = await api("/state");
  document.getElementById("drive-count").textContent = data.drives.length;
  document.getElementById("poll-count").textContent = data.poll_count;
  document.getElementById("last-poll").textContent = data.last_poll
    ? new Date(data.last_poll * 1000).toLocaleTimeString() : "—";
  document.getElementById("port-display").textContent = data.port;
  document.getElementById("auth-display").textContent = data.auth ? "on" : "off";

  const container = document.getElementById("drives-container");
  const empty = document.getElementById("empty-state");

  if (data.drives.length === 0) {
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

    html += `<div class="drive-card">
      <div class="drive-header">
        <div>
          <span class="drive-title">${drive.model}</span>
          <span class="drive-protocol ${protoCls}">${drive.protocol}</span>
          <span class="drive-status ${statusCls}">${state}</span>
          <div class="drive-meta">${drive.serial} · ${drive.device_path} · ${drive.id}</div>
        </div>
        <button class="btn-danger btn-sm" onclick="removeDrive('${drive.id}')">Remove</button>
      </div>`;

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
        No SMART attributes — this drive will show as UNSUPPORTED in HA.</div>`;
    }

    html += `</div></div>`;
  }
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
        # Quieter logging — skip noisy poll requests.
        path = args[0] if args else ""
        if "/api/drives" in str(path) or "/api/health" in str(path):
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
        return json.loads(self.rfile.read(length))

    # ── Routes ──

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        # Dashboard (no auth required).
        if path == "" or path == "/":
            presets_json = json.dumps({k: v[0] for k, v in PRESETS.items()})
            html = DASHBOARD_HTML.replace("PRESETS_JSON", presets_json)
            self._html_response(html)
            return

        # Mock control API (no auth required).
        if path == "/mock/state":
            poll = store.poll_info
            self._json_response(200, {
                "drives": store.get_all(),
                "poll_count": poll["count"],
                "last_poll": poll["last"],
                "port": self.port,
                "auth": bool(self.token),
            })
            return

        # ── Agent API (auth required) ──
        if not self._check_auth():
            return

        if path == "/api/health":
            self._json_response(200, {"status": "ok"})
            return

        if path == "/api/drives":
            store.record_poll()
            self._json_response(200, store.get_summaries())
            return

        if path.startswith("/api/drives/"):
            drive_id = path.split("/api/drives/")[1]
            drive = store.get_drive(drive_id)
            if drive:
                self._json_response(200, drive)
            else:
                self._json_response(404, {"error": "drive not found"})
            return

        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if path == "/mock/drives":
            body = self._read_body()
            preset = body.get("preset")
            if preset not in PRESETS:
                self._json_response(400, {"error": f"unknown preset: {preset}"})
                return
            drive_id = store.add_drive(preset)
            self._json_response(201, {"id": drive_id})
            return

        self._json_response(404, {"error": "not found"})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/")

        if path.startswith("/mock/drives/"):
            drive_id = path.split("/mock/drives/")[1]
            body = self._read_body()
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
            return

        self._json_response(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")

        if path.startswith("/mock/drives/"):
            drive_id = path.split("/mock/drives/")[1]
            if store.remove_drive(drive_id):
                self._json_response(200, {"ok": True})
            else:
                self._json_response(404, {"error": "drive not found"})
            return

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
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
        print("[mock] zeroconf not installed — skipping mDNS advertisement.")
        print("[mock] Install with: pip install zeroconf")
        return None

    hostname = socket.gethostname().split(".")[0]
    instance = f"smartha-{hostname}"
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

def main():
    parser = argparse.ArgumentParser(
        description="SMART Sniffer Mock Agent — fake smartha-agent for testing",
    )
    parser.add_argument("--port", type=int, default=9099, help="Port to listen on (default: 9099)")
    parser.add_argument("--token", type=str, default="", help="Bearer token (default: none)")
    parser.add_argument("--no-mdns", action="store_true", help="Disable mDNS advertisement")
    parser.add_argument("--preload", type=str, default="",
                        help="Comma-separated preset keys to load on startup (e.g. sata_hdd,nvme)")
    args = parser.parse_args()

    # Preload drives if requested.
    if args.preload:
        for key in args.preload.split(","):
            key = key.strip()
            if key in PRESETS:
                drive_id = store.add_drive(key)
                print(f"[mock] Preloaded {key} → {drive_id}")
            else:
                print(f"[mock] Unknown preset: {key}")

    # Set handler class attributes.
    MockHandler.token = args.token
    MockHandler.port = args.port

    # Start mDNS.
    zc = None
    if not args.no_mdns:
        zc = start_mdns(args.port, args.token)

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    server = ReusableHTTPServer(("0.0.0.0", args.port), MockHandler)

    auth_str = "enabled" if args.token else "disabled"
    print(f"\n  SMART Sniffer Mock Agent v{VERSION}")
    print(f"  Dashboard:  http://localhost:{args.port}/")
    print(f"  API:        http://localhost:{args.port}/api/drives")
    print(f"  Auth:       {auth_str}")
    print(f"  Drives:     {len(store.drives)}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] Shutting down…")
    finally:
        server.server_close()
        if zc:
            zc.unregister_all_services()
            zc.close()


if __name__ == "__main__":
    main()
