# Attention Severity Logic

**Project:** SMART Sniffer
**Module:** `attention.py`, `sensor.py`, `coordinator.py`
**Purpose:** Documents how SMART Sniffer classifies drive health into actionable states and notifies the user.

---

## Architecture

SMART Sniffer exposes two sensors per drive for health assessment:

| Sensor | Entity type | What it tracks | Indicator type |
|---|---|---|---|
| **Health** | Binary sensor | SMART official pass/fail + NVMe critical_warning | Lagging |
| **Attention Needed** | Enum sensor | Individual early-warning attributes | Leading |

The **Health** sensor reflects the drive's own self-assessment. It is a lagging indicator — drives can report PASSED right up until catastrophic failure.

The **Attention Needed** sensor is SMART Sniffer's proactive evaluation. It monitors individual attributes that research shows are predictive of failure *before* the SMART status flips, giving time to act.

---

## Attention Needed States

The attention sensor is an enum sensor (`device_class: enum`) with four possible states:

| State | Severity | Meaning | Recommended action |
|---|---|---|---|
| **NO** | `none` | All monitored indicators clear. | No action required. |
| **MAYBE** | `warning` | Early degradation signals detected. | Monitor closely. Schedule replacement. |
| **YES** | `critical` | Data integrity at risk. | **Back up immediately.** Replace drive at first opportunity. |
| **UNSUPPORTED** | `none` | Drive returned no usable SMART data. | SMART monitoring is not possible for this drive. Common with USB enclosures. |

### Attributes

The attention sensor carries these attributes for automations and dashboards:

```yaml
# When state is YES
severity: "critical"
reasons:
  - "Reallocated Sector Count: 3 (expected 0)"
  - "Current Pending Sector Count: 1 (expected 0)"
issue_count: 2

# When state is NO
severity: "none"
reasons:
  - "No issues detected"
issue_count: 0

# When state is UNSUPPORTED
severity: "none"
reasons:
  - "Drive data unavailable"
issue_count: 0
```

---

## Classification Rules

### Data-Quality Gate

Before evaluating any attributes, the system checks whether the drive returned usable SMART data. A drive has usable data if **any** of:
- `smart_status.passed` field is present (even if `false`)
- `ata_smart_attributes.table` has at least one entry
- `nvme_smart_health_information_log` has any keys

If none of these conditions are met → state is **UNSUPPORTED**.

This handles USB enclosures that block SMART passthrough, drives where smartctl times out, and SAS/SCSI drives that return no health page.

---

### ATA / SATA Drives

#### CRITICAL (state: YES) — Any non-zero value

These attributes should always read **0** on a healthy drive. Non-zero indicates the drive has encountered unrecoverable errors or remapped bad sectors.

| SMART Attribute Name(s) | ID | What it means |
|---|---|---|
| `Reallocated_Sector_Ct` | 5 | Drive remapped a bad sector. ~14× higher failure rate. |
| `Current_Pending_Sector` / `Current_Pending_Sector_Ct` / `Total_Pending_Sectors` | 197 | Sectors currently unreadable, waiting to be remapped. Urgent. |
| `Offline_Uncorrectable` / `Reported_Uncorrect` / `Uncorrectable_Error_Cnt` / `Total_Offl_Uncorrectabl` | 198 / 187 | Sectors failed during offline scan or ECC. Data loss likely. ~7.5× higher failure rate. |

#### WARNING (state: MAYBE) — Any non-zero value

These indicate early degradation but don't necessarily mean data has been lost yet.

| SMART Attribute Name(s) | ID | What it means |
|---|---|---|
| `Reallocated_Event_Count` | 196 | Individual reallocation events. Can increment even after ID 5 stops changing. |
| `Spin_Retry_Count` | 10 | HDD only. Motor struggling to spin up. Early mechanical wear. |
| `Command_Timeout` | 188 | Drive internally timed out. Controller or interconnect issues. |

#### WARNING (state: MAYBE) -- SSD wear threshold

| SMART Attribute Name(s) | Condition | What it means |
|---|---|---|
| `Wear_Leveling_Count`, `Media_Wearout_Indicator`, `SSD_Life_Left`, `Remaining_Lifetime_Perc`, `Percent_Lifetime_Remain`, `Perc_Rated_Life_Remain`, `Percent_Life_Remaining` (WD Blue's ID 230 arrives as `Media_Wearout_Indicator`) | >= 90% used (after inversion from normalized VALUE) | SSD nearing end of rated write endurance. ATA normalized VALUE is inverted to "percentage used" for consistency with NVMe. Added in v0.5.6. A row whose normalized value, worst, threshold and flags are all 0 is not a gauge and is skipped, so a drive with no other wear row raises no wear warning. |

> **Note:** When both critical and warning triggers are active, the state is **YES** (critical wins) and all reasons are combined in the reasons list -- critical reasons first, warning reasons appended.

#### Device Statistics fill gaps (v0.8.0+)

Some drives keep a count in their Device Statistics log that their SMART attribute table does not show: a WDC Ultrastar without attribute 187 still counts reported uncorrectable errors there. When the table has no attribute 187 (or 5, or no usable wear attribute), SMART Sniffer reads Reported Uncorrectable Errors (or Reallocated Sector Count, or SSD Wear Percent Used) from the log instead, and the reason says so: "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)". Thresholds apply as usual. The first time this turns a drive to attention you get one notification, even if it is the first poll after an upgrade. Run a long self-test (`smartctl -t long`); if it passes and the count does not grow, set the threshold to the current reading to accept it. The Reported Uncorrectable Errors and Reallocated Sector Count sensors keep their table value (unknown when the table has no row) and show the log's count as the `device_statistics_count` or `device_statistics_logical_sectors` attribute; their icon does not change for a gap-filled reason. Turning Device Statistics off removes these reasons, which can clear the state and dismiss the notification.

Data Written and Data Read keep long-term statistics as a `total`. The long-term graph starts at zero on the entity's first day, not at the drive's lifetime total, and if short-term statistics are purged the sum restarts at zero. A change of source (for example turning Device Statistics off) shows as one step, not a dip.

---

### NVMe Drives

#### CRITICAL (state: YES)

| Field | Condition | What it means |
|---|---|---|
| `critical_warning` | ≠ 0 | Bitmask. Any set bit = act now. Bits: spare below threshold, temp out of range, reliability degraded, read-only mode, volatile backup failed. |
| `media_errors` | > 0 | Cumulative unrecoverable media errors. Should always be 0. A value at or above 2^64 is not a count (NVMe counters are 128-bit, and some Windows drivers hand back the log with the next field's bytes inside this one); it reads as unknown and is not a trigger (v0.8.0+). |
| `available_spare` | ≤ `available_spare_threshold` | Drive's reserve block pool at or below manufacturer threshold. |

#### WARNING (state: MAYBE)

| Field | Condition | What it means |
|---|---|---|
| `available_spare` | < 20% (but above threshold) | Early warning before hitting official threshold. |
| `percentage_used` | ≥ 90% | Drive at 90%+ of rated write endurance. At 100% → read-only mode. |

---

## Health Binary Sensor (No-Data Fix)

The health binary sensor now returns **three** possible states:

| `is_on` value | HA rendering | Meaning |
|---|---|---|
| `False` | "OK" | SMART PASSED, all clear. |
| `True` | "Problem" | SMART FAILED or critical attribute triggered. |
| `None` | "Unknown" | No usable SMART data — can't determine health. |

Previously, drives with no usable data (like the external USB drive) would show "OK" because the evaluation fell through with no failures to detect. Now they show "Unknown" — which is accurate and not misleading.

---

## Persistent Notification Behavior

The coordinator (`coordinator.py`) evaluates attention after every poll and manages HA persistent notifications automatically. **No user automation is required.**

### Transition Rules

| Previous state | New state | Action |
|---|---|---|
| _(first poll)_ | any | Record baseline, **no notification** (avoids spam on HA restart). Exception (v0.8.0): a drive whose reasons include one ending "from device statistics" that was never announced for it notifies once, with the line "First reading from this drive's Device Statistics." The record is kept per drive and reading across restarts. |
| `NO` | `MAYBE` | Fire ⚠️ WARNING notification |
| `NO` | `YES` | Fire 🔴 CRITICAL notification |
| `MAYBE` | `YES` | Update notification → escalate to CRITICAL |
| `YES` | `MAYBE` | Update notification → de-escalate to WARNING |
| `YES` / `MAYBE` | `NO` | Dismiss notification (resolved) |
| any | `UNSUPPORTED` | Fire ℹ️ informational notification |
| `UNSUPPORTED` | any | Dismiss informational notification |
| Drive removed | — | Dismiss notification, clean up state |

### Notification Examples

**Critical:**
> **🔴 Drive Attention Required — Samsung SSD 870 EVO 500GB (S6PXNS0L100992M)**
>
> **CRITICAL — Back up your data immediately.**
>
> • Reallocated Sector Count: 3 (expected 0)
> • Current Pending Sector Count: 1 (expected 0)

**Warning:**
> **⚠️ Drive Attention Required — Seagate Barracuda ST2000DM008 (ZFN4XXXX)**
>
> **WARNING — Monitor closely and plan for replacement.**
>
> • Spin Retry Count: 2 (expected 0)

**Unsupported:**
> **ℹ️ SMART Monitoring Unavailable — Proxmox External 1TB USB Drive**
>
> SMART Sniffer cannot read health data from this drive. This commonly happens with USB enclosures that block SMART passthrough. Health monitoring is not available for this drive.

### Notification ID

Each drive has a stable notification ID: `smart_sniffer_attention_{drive_id}`

Escalations/de-escalations overwrite the existing notification (same ID). Dismissing the notification = user acknowledgment. If the condition changes again, a new notification fires.

---

## Per-Drive Thresholds (v0.6.3+)

For a drive with old, stable damage you have chosen to keep in service, raise the limit instead of suppressing the alert: Settings > Devices & Services > SMART Sniffer > Configure > Alert thresholds, then pick the drive. Each attribute shows the drive's current reading and the built-in default. Attention is raised only when a reading passes the limit you set, so new damage is still reported. Accepted readings are listed in the sensor's `accepted` attribute. Type the default back in to clear a threshold.

---

## ZFS Pool Health (v0.7.0+)

Pools are evaluated separately from drives. Each pool has a **Problem** binary sensor, on when any of these hold:

- the pool state is not `ONLINE` (`DEGRADED`, `FAULTED`, `UNAVAIL`, `SUSPENDED`, and so on);
- any read, write or checksum error count is above zero. Counts are summed over every disk in the pool, because ZFS records an error on the disk where it happened: a failing disk that redundancy covers shows errors on its own row while the pool row reads zero;
- the pool reports data errors;
- a disk or group in the pool is not `ONLINE` or has errors (listed in `problem_devices`);
- the pool is no longer reported by `zpool status` while the agent is reachable (state `MISSING`).

The `status:` and `action:` text from `zpool status` is shown as attributes but never counts on its own, so "Some supported and requested features are not enabled" does not raise an alert.

A notification fires when a pool becomes unhealthy, updates when the reasons change, and dismisses when the pool recovers. A pool that is missing when Home Assistant starts notifies; a pool that is already unhealthy at start turns its Problem sensor on but does not notify until something changes. If the agent cannot read `zpool`, pool entities go unavailable and nothing is notified. The notification states each error count once: when a single disk carries the whole pool total, only the disk line shows it (v0.8.0). Pool devices are named "ZFS pool <name> (<host>)" so two hosts with the same pool name stay apart (v0.8.1).

---

## Source References

- [Backblaze Hard Drive Stats](https://www.backblaze.com/b2/hard-drive-test-data.html) — Failure correlation data for SMART attributes.
- [smartmontools drivedb.h](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/drivedb.h) — Attribute ID/name reference.
- [NVMe Base Specification 1.4](https://nvmexpress.org/specifications/) — SMART / Health Information Log definitions.
