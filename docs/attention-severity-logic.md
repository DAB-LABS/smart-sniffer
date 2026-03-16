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

This handles USB enclosures that block SMART passthrough, drives where smartctl times out, and protocols we don't yet support (SAS/SCSI).

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

> **Note:** When both critical and warning triggers are active, the state is **YES** (critical wins) and all reasons are combined in the reasons list — critical reasons first, warning reasons appended.

---

### NVMe Drives

#### CRITICAL (state: YES)

| Field | Condition | What it means |
|---|---|---|
| `critical_warning` | ≠ 0 | Bitmask. Any set bit = act now. Bits: spare below threshold, temp out of range, reliability degraded, read-only mode, volatile backup failed. |
| `media_errors` | > 0 | Cumulative unrecoverable media errors. Should always be 0. |
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
| _(first poll)_ | any | Record baseline, **no notification** (avoids spam on HA restart) |
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

## Suppressing Known Alerts

For drives with permanent issues (e.g., a drive with reallocated sectors you've chosen to keep in service), create an `input_boolean` helper and a suppression automation:

```yaml
automation:
  - alias: "Suppress known SMART alerts"
    trigger:
      - platform: state
        entity_id: binary_sensor.your_drive_attention_needed
        to: "on"
    condition:
      - condition: state
        entity_id: input_boolean.drive_sxxx_acknowledged
        state: "on"
    action:
      - service: persistent_notification.dismiss
        data:
          notification_id: "smart_sniffer_attention_your_drive_id"
```

---

## Source References

- [Backblaze Hard Drive Stats](https://www.backblaze.com/b2/hard-drive-test-data.html) — Failure correlation data for SMART attributes.
- [smartmontools drivedb.h](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/drivedb.h) — Attribute ID/name reference.
- [NVMe Base Specification 1.4](https://nvmexpress.org/specifications/) — SMART / Health Information Log definitions.
