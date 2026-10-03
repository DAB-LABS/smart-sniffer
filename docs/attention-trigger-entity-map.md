# Attention Trigger → Entity Map

Every attribute that can trigger an attention state change must have a visible sensor entity on the HA device page, with a dynamic icon that reflects whether it's currently contributing to the alert. This document maps each trigger to its entity and tracks coverage. One exception since v0.8.0: a reading taken from the Device Statistics log because the attribute table has no row shows as an attribute on the matching sensor, not as its state, and the icon does not change for it.

Last updated: v0.8.0

---

## ATA / SATA Triggers

### Critical (→ YES)

| smartctl Attribute | Sensor Entity | Key | Dynamic Icon |
|---|---|---|---|
| `Reallocated_Sector_Ct` | Reallocated Sector Count | `reallocated_sector_count` | `mdi:alert-octagon` when > 0 |
| `Current_Pending_Sector` / `Current_Pending_Sector_Ct` / `Total_Pending_Sectors` | Current Pending Sector Count | `current_pending_sector_count` | `mdi:alert-octagon` when > 0 |
| `Offline_Uncorrectable` / `Reported_Uncorrect` / `Uncorrectable_Error_Cnt` / `Total_Offl_Uncorrectabl` | Reported Uncorrectable Errors | `reported_uncorrectable_errors` | `mdi:alert-octagon` when > 0 |
| Device Statistics page 4, Reported Uncorrectable Errors (only when the table has no attribute 187) | Reported Uncorrectable Errors | `reported_uncorrectable_errors` | No icon change: the state is the table value (unknown) and the log count is the `device_statistics_count` attribute. Reason ends "from device statistics" (v0.8.0) |
| Device Statistics page 3, Reallocated Logical Sectors (only when the table has no attribute 5) | Reallocated Sector Count | `reallocated_sector_count` | No icon change: the log count is the `device_statistics_logical_sectors` attribute (v0.8.0) |

### Warning (→ MAYBE)

| smartctl Attribute | Sensor Entity | Key | Dynamic Icon |
|---|---|---|---|
| `Reallocated_Event_Count` | Reallocated Event Count | `reallocated_event_count` | `mdi:alert-circle` when > 0 |
| `Spin_Retry_Count` | Spin Retry Count | `spin_retry_count` | `mdi:alert-circle` when > 0 |
| `Command_Timeout` | Command Timeout | `command_timeout` | `mdi:alert-circle` when > 0 |
| `Wear_Leveling_Count` / `Media_Wearout_Indicator` / `SSD_Life_Left` / `Remaining_Lifetime_Perc` / `Percent_Lifetime_Remain` / `Perc_Rated_Life_Remain` / `Percent_Life_Remaining` (>= 90% used) | Wear Level (% Used) | `wear_leveling_count` | `mdi:alert-circle` when >= 90 |
| Device Statistics page 7, Percentage Used Endurance Indicator (only when no usable wear attribute) | none; the Wear Level (% Used) sensor is not created without a table row | -- | Reason ends "from device statistics" (v0.8.0) |

---

## NVMe Triggers

### Critical (→ YES)

| NVMe Health Log Field | Sensor Entity | Key | Dynamic Icon |
|---|---|---|---|
| `critical_warning` (bitmask ≠ 0) | Critical Warning | `critical_warning` | `mdi:alert-octagon` when ≠ 0 |
| `media_errors` (≥ 1) | Media Errors | `media_errors` | `mdi:alert-octagon` when > 0. A value at or above 2^64, or one smartctl prints with a `_le` byte array, reads as unknown and is not a trigger (v0.8.0) |
| `available_spare` ≤ `available_spare_threshold` | Available Spare | `available_spare` | `mdi:alert-octagon` when ≤ threshold |

### Warning (→ MAYBE)

| NVMe Health Log Field | Sensor Entity | Key | Dynamic Icon |
|---|---|---|---|
| `available_spare` < 20% | Available Spare | `available_spare` | `mdi:alert-circle` when < 20 |
| `percentage_used` ≥ 90% | Wear Leveling / Percentage Used | `wear_leveling_count` | `mdi:alert-circle` when ≥ 90 |

---

## Universal Triggers

| Condition | Sensor Entity | Key | Dynamic Icon |
|---|---|---|---|
| `smart_status.passed` = false | SMART Status | `smart_status` | `mdi:alert-octagon` when "FAILED" |

---

## Summary Entities (per drive, always created)

| Entity | Type | Description |
|---|---|---|
| **Attention Needed** | Enum sensor | Primary state: NO / MAYBE / YES / UNSUPPORTED. Icon changes per state. Attributes include `severity`, `reasons` list, and `issue_count`. |
| **Attention Reasons** | Text sensor (diagnostic) | Human-readable semicolon-separated list of what's triggering the alert. Shows "No issues detected" when clean, "No usable SMART data" for UNSUPPORTED. Icon matches attention severity. |

---

## Icon Behavior

All diagnostic sensors that participate in attention evaluation have dynamic icons:

- **Normal state (value is 0 or within safe range):** Default icon from `SensorEntityDescription` (varies per sensor — thermometer, clock, harddisk, etc.).
- **Warning trigger active (MAYBE):** Switches to `mdi:alert-circle` (filled circle with exclamation).
- **Critical trigger active (YES):** Switches to `mdi:alert-octagon` (octagon stop sign with exclamation).

Icons revert to default automatically when the triggering value returns to safe range.

The Attention Needed sensor itself has state-based icons: `mdi:check-circle-outline` (NO), `mdi:alert-circle-outline` (MAYBE), `mdi:alert-octagon` (YES), `mdi:help-circle-outline` (UNSUPPORTED).

---

## Notification Behavior

Persistent notifications are managed by the coordinator and fire on state or reason transitions:

| Transition | Action |
|---|---|
| First poll | Silent baseline, no notification, except a drive with a never-announced reason ending "from device statistics", which notifies once (v0.8.0) |
| NO → MAYBE | Fire warning notification |
| NO → YES | Fire critical notification |
| MAYBE → YES | Update notification (escalate) |
| YES → MAYBE | Update notification (de-escalate) |
| YES/MAYBE → NO | Dismiss notification |
| * → UNSUPPORTED | Fire informational notification (once) |
| Same state, reasons changed | Update notification with new reason list |

Notification IDs are stable per drive: `smart_sniffer_attention_{drive_id}`. Each notification lists all active trigger reasons as bullet points.

---

## Known Gaps (Future Work)

- **Temperature triggers** — not yet implemented. Planned: absolute threshold (e.g., > 55°C → MAYBE) and trend-over-time detection (sustained rise over multiple polls). Requires historical storage in the coordinator.
- **SAS/SCSI triggers** — `scsi_grown_defect_list` and SCSI error counters are not yet parsed. SAS drives currently show as UNSUPPORTED.
- **NVMe `critical_warning` bitmask decoding** — currently treated as a single flag (≠ 0 → YES). Future: decode individual bits (spare below threshold, temperature exceeded, reliability degraded, read-only mode, volatile memory backup failed) into separate reasons.
