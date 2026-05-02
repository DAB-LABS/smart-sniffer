# SMART Attribute Name Variants Across Drive Manufacturers

**Project:** SMART Sniffer
**Author:** Research compiled March 2026
**Purpose:** Documents the manufacturer-specific SMART attribute names used in `smartctl --json` output, informing the `ata_name_map` in `sensor.py`.

---

## Background

When `smartctl` reads a drive's SMART attribute table, it translates raw attribute IDs into human-readable names using its built-in `drivedb.h` database. The name strings it produces vary significantly between manufacturers — sometimes even between product lines from the same manufacturer. This is a real-world interoperability problem: two drives reporting the same physical measurement (e.g. temperature, wear level) may use entirely different name strings in their JSON output.

SMART Sniffer's `sensor.py` maps each logical sensor key (e.g. `wear_leveling_count`) to a list of known attribute name strings. This document records all known variants, their sources, and any gaps still requiring verification.

---

## Attribute Coverage by Manufacturer

The table below summarises which manufacturers have been confirmed for each of the five monitored attributes. ✅ = confirmed name known, ⚠️ = partially confirmed or needs verification, — = attribute not applicable to this drive type.

| Manufacturer        | Temp | Power-On Hours | Reallocated Sectors | Pending Sectors | Uncorrectable Errors | Wear Leveling |
|---------------------|:----:|:--------------:|:-------------------:|:---------------:|:--------------------:|:-------------:|
| Seagate (HDD)       | ✅   | ✅             | ✅                  | ✅              | ✅                   | —             |
| Western Digital (HDD)| ✅  | ✅             | ✅                  | ✅              | ✅                   | —             |
| Toshiba (HDD)       | ✅   | ✅             | ✅                  | ✅              | ✅                   | —             |
| HGST / Hitachi (HDD)| ✅   | ✅             | ✅                  | ✅              | ✅                   | —             |
| Samsung (SSD)       | ✅   | ✅             | ✅                  | ✅              | ✅                   | ✅            |
| Intel (SSD)         | ✅   | ✅             | ✅                  | ✅              | —                    | ✅            |
| Crucial / Micron    | ✅   | ✅             | ✅                  | ✅              | —                    | ✅            |
| Kingston            | ✅   | ✅             | ✅                  | ✅              | —                    | ✅            |
| SanDisk / WD (SSD)  | ✅   | ✅             | ✅                  | ✅              | —                    | ✅            |
| SK Hynix (SSD)      | ✅   | ✅             | ✅                  | ✅              | —                    | ⚠️            |
| NVMe (all)          | ✅   | ✅             | —                   | —               | ✅ (media_errors)    | ✅ (% used)   |

---

## Attribute Details

### 1. Temperature

**Logical key:** `temperature`
**Unit:** °C
**ATA attribute IDs typically used:** 190, 194

| Name String               | Manufacturers / Notes                                              |
|---------------------------|--------------------------------------------------------------------|
| `Temperature_Celsius`     | Most HDDs: Seagate, WD, Toshiba, HGST, Hitachi (ID 194)          |
| `Temperature_Internal`    | Some Intel SSDs (ID 194)                                          |
| `Airflow_Temperature_Cel` | Samsung SSDs (ID 190), some Intel SSDs (also ID 190)             |
| `HDA_Temperature`         | Older Hitachi and HGST HDDs (ID 194)                             |
| `Drive_Temperature`       | Some generic and OEM drives                                       |

**NVMe path:** `nvme_smart_health_information_log.temperature`

> **Note:** Samsung SSDs report temperature on ID 190 (`Airflow_Temperature_Cel`) rather than the more common ID 194. ID 190 measures the airflow temperature near the controller, which is functionally the same reading for monitoring purposes.

---

### 2. Power-On Hours

**Logical key:** `power_on_hours`
**Unit:** hours
**ATA attribute ID:** 9

| Name String                 | Manufacturers / Notes                            |
|-----------------------------|--------------------------------------------------|
| `Power_On_Hours`            | Most manufacturers — highly standardised         |
| `Power_On_Hours_and_Msec`   | Some Western Digital drives                      |
| `Power_On_Time`             | Some older drives, generic OEM firmware          |

**NVMe path:** `nvme_smart_health_information_log.power_on_hours`

> **Note:** `Power_On_Hours_and_Msec` encodes both hours and milliseconds in the raw value. `smartctl` extracts only the hours component in the `raw.value` field, so the mapping works correctly without special-casing.

---

### 3. Reallocated Sector Count

**Logical key:** `reallocated_sector_count`
**ATA attribute ID:** 5
**Applicability:** ATA/SATA only (not NVMe)

| Name String              | Manufacturers / Notes                                       |
|--------------------------|-------------------------------------------------------------|
| `Reallocated_Sector_Ct`  | Virtually all manufacturers — one of the most standardised SMART attributes |

> **Note:** This is the most consistent attribute name across the entire SMART ecosystem. The short form `_Ct` (not `_Count`) is the standard in `smartctl`'s drivedb. There are no known meaningful variants in active use.

---

### 4. Current Pending Sector Count

**Logical key:** `current_pending_sector_count`
**ATA attribute ID:** 197
**Applicability:** ATA/SATA only (not NVMe)

| Name String                  | Manufacturers / Notes                                |
|------------------------------|------------------------------------------------------|
| `Current_Pending_Sector`     | Most manufacturers (Seagate, Toshiba, HGST, Samsung) |
| `Current_Pending_Sector_Ct`  | Some Western Digital and HGST variants               |
| `Total_Pending_Sectors`      | Some Micron and Crucial SSDs                         |

> **Note:** A non-zero value here means sectors the drive has identified as potentially unreadable and is waiting to reallocate. Elevated counts warrant monitoring and imminent backup.

---

### 5. Reported Uncorrectable Errors

**Logical key:** `reported_uncorrectable_errors`
**ATA attribute IDs used:** 187 (uncorrectable errors count), 198 (offline uncorrectable)
**Applicability:** ATA/SATA primarily; NVMe uses `media_errors`

| Name String                  | Manufacturers / Notes                                          |
|------------------------------|----------------------------------------------------------------|
| `Offline_Uncorrectable`      | Most HDDs: Seagate, WD, Toshiba, HGST (ID 198)               |
| `Reported_Uncorrect`         | Some Seagate and WD variants                                   |
| `Uncorrectable_Error_Cnt`    | Samsung SSDs (ID 187) — semantically equivalent                |
| `Total_Offl_Uncorrectabl`    | Some Hitachi and HGST HDDs                                     |

**NVMe path:** `nvme_smart_health_information_log.media_errors`

> **Note:** IDs 187 and 198 measure slightly different things (ECC errors vs. offline surface scan errors), but both indicate unrecoverable data integrity issues and are treated as equivalent for alerting purposes. A healthy drive should show `0` for all variants.

---

### 6. Wear Leveling / SSD Endurance

**Logical key:** `wear_leveling_count`
**Unit:** % used (0% = new, 100% = fully worn -- normalized from ATA's inverted scale)
**Applicability:** SSDs only (ATA/SATA); NVMe uses a dedicated field

| Name String                   | Manufacturers / Notes                                                    |
|-------------------------------|--------------------------------------------------------------------------|
| `Wear_Leveling_Count`         | Samsung SSDs (ID 177) — 870 EVO, 860, 860 EVO, 850 Pro/EVO, etc. Reports remaining erase cycles (higher = newer) |
| `Wear_Range_Delta`            | Some Samsung SSD variants                                                |
| `Media_Wearout_Indicator`     | Intel SSDs (ID 233), some WD/SanDisk SSDs. 100 = new, 0 = worn out     |
| `SSD_Life_Left`               | Intel 520, 530 series; some Kingston SSDs (ID 231)                       |
| `Remaining_Lifetime_Perc`     | Kingston SSDs (ID 231)                                                   |
| `Percent_Lifetime_Remain`     | Crucial/Micron SSDs (ID 202)                                             |
| `Perc_Rated_Life_Remain`      | Some Micron enterprise SSD variants                                      |
| `Percent_Life_Remaining`      | Some SanDisk SSDs                                                        |
| `Drive_Life_Protection_Stat`  | Some WD Blue SSDs (ID 230)                                               |

**NVMe path:** `nvme_smart_health_information_log.percentage_used` (0% = new, 100% = fully worn)

> **ATA wear normalization (v0.5.6+):** ATA drives report the normalized VALUE column as "percentage of life remaining" (100 = new, 0 = worn). SMART Sniffer inverts this to "percentage used" (0 = new, 100 = worn) for consistency with NVMe `percentage_used` semantics. The raw value is vendor-specific (erase cycles, write counts, etc.) and is not used.
>
> **TODO:** SK Hynix SSDs are believed to use ID 177 for wear leveling (same as Samsung), but the exact attribute name string in `smartctl`'s drivedb has not been confirmed. Verification needed via `smartctl -a` output on an SK Hynix drive.

---

## NVMe Summary

NVMe drives use a completely different data structure: `nvme_smart_health_information_log` (a flat dict) rather than the ATA attribute table. The mappings are:

| Logical Key                    | NVMe JSON Field          | Notes                                  |
|--------------------------------|--------------------------|----------------------------------------|
| `temperature`                  | `temperature`            | Always present                         |
| `power_on_hours`               | `power_on_hours`         | Always present                         |
| `wear_leveling_count`          | `percentage_used`        | 0 = new, 100 = fully worn (inverted vs. some ATA SSDs) |
| `reported_uncorrectable_errors`| `media_errors`           | Cumulative unrecoverable media errors  |
| `reallocated_sector_count`     | N/A                      | Not applicable to NVMe — entity not created |
| `current_pending_sector_count` | N/A                      | Not applicable to NVMe — entity not created |

---

## Sources and References

- **smartmontools drivedb.h** — Primary source for attribute ID-to-name mappings: https://github.com/smartmontools/smartmontools/blob/master/smartmontools/drivedb.h
- **Backblaze Hard Drive Stats** — Real-world validation of attribute names across drive populations: https://www.backblaze.com/b2/hard-drive-test-data.html
- **Kingston SSD SMART Attribute Reference** — Internal document referenced in community forums; confirms `SSD_Life_Left` and `Remaining_Lifetime_Perc` for ID 231
- **Samsung SSD datasheet notes** — ID 177 (`Wear_Leveling_Count`) semantics confirmed via Samsung 870 EVO product documentation
- **Intel SSD product spec sheets** — ID 233 (`Media_Wearout_Indicator`) and ID 231 (`SSD_Life_Left`) confirmed for Intel 520/530 series
- **smartctl community reports (SmallNetBuilder, TrueNAS forums, r/DataHoarder)** — Supplemental validation for less common variants
- **Direct observation** — Samsung 870 EVO 500GB, Samsung 860 EVO 1TB on Proxmox test system (March 2026)

---

## Open Questions

1. **SK Hynix wear leveling:** Attribute ID 177 is expected but the exact name string in `smartctl` output has not been confirmed. A sample `smartctl -a /dev/sdX` from an SK Hynix SSD is needed.
2. **SAS/SCSI drives:** Not covered. These use an entirely different SMART data structure in `smartctl` JSON output — a future `sensor.py` extension will be needed.
3. **Western Digital Blue/Green SSDs:** Some models report via the ATA attribute table differently than WD Black/Red HDDs. `Drive_Life_Protection_Stat` (ID 230) is confirmed for WD Blue SSDs but coverage is not exhaustive.
4. **Seagate Barracuda SSDs:** Seagate's SSD lineup uses attribute names inherited from their HDD firmware team — confirmation that all HDD names listed here apply equally to their SSDs is pending.
