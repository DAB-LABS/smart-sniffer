# SMART Early-Warning Attributes

**Project:** SMART Sniffer
**Purpose:** Documents which SMART attributes predict drive failure *before* the official SMART status flips, and how SMART Sniffer uses them.

---

## Why SMART Status Alone Is Not Enough

The official SMART pass/fail verdict (`smart_status.passed` in smartctl JSON) is determined by the drive's own firmware. Drives can and do report PASSED right up until catastrophic failure. Backblaze's longitudinal studies on millions of drive-hours found that specific individual attributes are far more predictive than the headline status.

SMART Sniffer exposes two sensors per drive for this reason:

| Sensor | Type | What it tracks | Indicator type |
|---|---|---|---|
| **Health** | Binary sensor | SMART official pass/fail + NVMe critical_warning | Lagging |
| **Attention Needed** | Enum sensor | Individual early-warning attributes (below) | Leading |

The `Attention Needed` sensor is an enum with four states: `NO` (clear), `MAYBE` (warning), `YES` (critical), `UNSUPPORTED` (no usable SMART data). It carries a `reasons` attribute listing exactly what triggered it, making it easy to build informative automations.

---

## Early-Warning Attributes by Drive Type

### ATA / SATA Drives

All of these should read **0** on a healthy drive. Any non-zero value triggers the Attention Needed sensor.

| Attribute | SMART ID | Alert threshold | Backblaze correlation | Notes |
|---|---|---|---|---|
| **Reallocated Sector Count** | 5 | > 0 | ~14× higher failure rate | Drive remapped a bad sector to a spare. Even 1 is a warning sign. The count rarely goes back down — treat as permanent watch. |
| **Reallocated Event Count** | 196 | > 0 | High | Counts individual reallocation *events* (separate from the total count). Can increase even when ID 5 plateaus. Added in this release. |
| **Current Pending Sector Count** | 197 | > 0 | High | Sectors the drive wants to reallocate but hasn't yet — they are currently unreadable. Non-zero is urgent. Note: not all SSDs report this attribute (e.g., Samsung 870 EVO does not); the entity is only created when the drive supports it. |
| **Offline Uncorrectable / Reported Uncorrectable Errors** | 198 / 187 | > 0 | ~7.5× higher failure rate | Sectors that failed during offline scan or ECC correction. Non-zero means data loss has likely occurred. |
| **Spin Retry Count** | 10 | > 0 | Moderate | HDD only. Drive is struggling to spin up the platter motor. Non-zero means mechanical wear is starting. Entity only created for drives that report this attribute. |
| **Command Timeout** | 188 | > 100 | Moderate | Drive internally timed out on a command. Low counts (1~100) are common from USB sleep/wake cycles, SATA power management (ALPM), and NCQ reordering ~ not failure indicators. Counts above 100 suggest controller or interconnect degradation. Seagate/OEM drives pack three 16-bit counters into the 48-bit raw value; the integration decodes to the lower 16 bits automatically (v0.4.26+). Backblaze lists attribute 188 as one of five failure-correlated attributes, but 84% of drives show non-zero values at some point ~ the correlation is with elevated counts, not merely non-zero. |
| **SSD Wear Level** | varies (177, 202, 230, 231, 233) | >= 90% used | Moderate | ATA SSD endurance indicator. The normalized VALUE from the drive represents "life remaining" (100 = new, 0 = worn); SMART Sniffer inverts this to "percentage used" for consistency with NVMe. Warning triggers at >= 90% used, matching NVMe threshold. Added in v0.5.6. |

### NVMe Drives

NVMe drives use a different health log structure. The following map to the `attention_needed` sensor:

| Field | Alert condition | Notes |
|---|---|---|
| **critical_warning** | Any bit set (≠ 0) | Bitmask. Bits indicate: spare below threshold, temperature out of range, NVM subsystem reliability degraded, read-only mode, volatile backup device failed. |
| **media_errors** | > 0 | Cumulative unrecoverable media errors. Equivalent to Offline Uncorrectable for ATA. Should always be 0. |
| **available_spare** | ≤ available_spare_threshold OR < 20% | Percentage of spare NVMe blocks remaining. The drive reports its own threshold; SMART Sniffer also warns at < 20% as an early heads-up before the official threshold is reached. |
| **percentage_used** | >= 90% | 0% = new drive, 100% = fully worn. At 90%+ the drive is nearing end of rated write endurance. As of v0.5.6, ATA SSDs use the same unified scale and threshold. |

---

## What's Monitored vs. Not (and Why)

| Attribute | Monitored | Reason if not |
|---|---|---|
| Reallocated Sector Count | ✅ | Leading failure indicator |
| Reallocated Event Count | ✅ | Added in this release |
| Current Pending Sector Count | ✅ | Leading failure indicator (entity skipped if drive doesn't report it) |
| Uncorrectable Errors | ✅ | Leading failure indicator |
| Spin Retry Count | ✅ | Added in this release (HDD only) |
| Command Timeout | ✅ | Added in this release |
| Power Cycle Count | ✅ | Diagnostic sensor — not a failure trigger, but context for wear |
| NVMe Available Spare | ✅ | Added in this release |
| ATA SSD Wear Level | ✅ (via Wear Level sensor) | Added in v0.5.6 -- unified with NVMe scale |
| NVMe Percentage Used | ✅ (via Wear Level sensor) | Already mapped |
| UDMA CRC Error Count (ID 199) | ❌ | Indicates cable/interconnect issues, not drive failure. Useful but out of scope for v1. |
| Raw Read Error Rate (ID 1) | ❌ | Highly manufacturer-specific encoding; Seagate packs ECC stats into the raw value making direct comparison unreliable. |
| Seek Error Rate (ID 7) | ❌ | Same Seagate encoding issue as above. |
| Temperature trending | ❌ | Current temp exposed as sensor; trend alerting requires HA history automation. |

---

## How the Attention Needed Sensor Works

`sensor.{drive}_attention_needed` evaluates every poll cycle and returns one of four enum states:

| State | Meaning |
|---|---|
| `NO` | All monitored indicators clear |
| `MAYBE` | Early degradation signals detected (warning) |
| `YES` | Data integrity at risk (critical) |
| `UNSUPPORTED` | No usable SMART data (e.g., USB enclosures) |

The `reasons` attribute contains a human-readable list of what triggered the state:

```yaml
# Example: critical state
state: "YES"
attributes:
  reasons:
    - "Reallocated Sector Count: 3 (expected 0)"
    - "Current Pending Sector Count: 1 (expected 0)"
```

```yaml
# Example: all clear
state: "NO"
attributes:
  reasons:
    - "No issues detected"
```

The integration also fires persistent notifications automatically on state transitions — no automations needed. See [attention-severity-logic.md](attention-severity-logic.md) for the full notification lifecycle.

---

## Clearing Attention / Acknowledgment

The `attention_needed` sensor is **self-clearing**: when the underlying condition resolves, it automatically returns to `NO`. In practice, for most failure-type attributes (reallocated sectors, uncorrectable errors), the count will not go back to zero — the sensor stays in `YES` or `MAYBE` until the drive is replaced.

**Optional HA automation pattern for custom alerting:**

```yaml
automation:
  - alias: "Disk attention alert"
    trigger:
      - platform: state
        entity_id: sensor.your_drive_attention_needed
        to: "YES"
    action:
      - service: persistent_notification.create
        data:
          title: "Drive Attention Required"
          message: >
            {{ state_attr('sensor.your_drive_attention_needed', 'reasons') | join('\n') }}
          notification_id: "disk_attention_{{ trigger.entity_id }}"
```

Note: The integration already fires persistent notifications automatically on state changes, so this automation is only needed if you want additional custom alerting (e.g., mobile push, Slack, email).

For suppressing repeat notifications on a known-stable degraded drive:

```yaml
# Create an input_boolean helper: input_boolean.drive_xyz_acknowledged
# Add a condition to the above automation:
condition:
  - condition: state
    entity_id: input_boolean.drive_xyz_acknowledged
    state: "off"
```

---

## Sources

- [Backblaze Hard Drive Stats](https://www.backblaze.com/b2/hard-drive-test-data.html) — Backblaze (2013–present). Reallocated sectors (ID 5), pending sectors (ID 197), and uncorrectable errors (ID 198) are the three attributes most correlated with imminent failure.
- [smartmontools drivedb.h](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/drivedb.h) — Attribute ID and name reference.
- [NVMe Specification 1.4](https://nvmexpress.org/specifications/) — NVMe SMART / Health Information Log (Section 5.14.1.2).
