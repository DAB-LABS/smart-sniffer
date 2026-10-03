# SMART Sniffer Mock Agent

A fake `smartha-agent` for testing the Home Assistant integration without waiting for real drives to degrade. Serves the same REST API as the real Go agent at v0.8.0 (drives, Device Statistics, Data Written and Data Read, ZFS pools) but with fully controllable fake data and a built-in web dashboard.

**Location:** `tools/mock-agent.py`
**Requirements:** Python 3.9+ (stdlib only, no pip install needed)
**Optional:** `pip install zeroconf` for mDNS auto-discovery advertisement

The SMART Sniffer app's Test Lab runs this same file. Its Dockerfile downloads `tools/mock-agent.py` from the smart-sniffer release, so a change here reaches the app with the next release.

---

## Quick Start

```bash
# Basic: dashboard at http://localhost:9099
python3 tools/mock-agent.py --preload sata_hdd,nvme,usb_blocked

# Different port (run alongside the real agent on 9099)
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,nvme

# With bearer token auth
python3 tools/mock-agent.py --port 9100 --token mysecrettoken123 --preload sata_ssd

# Every preset, the ZFS pool included
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,sata_hdd_devstat,sata_ssd,nvme,nvme_usb,usb_blocked,virtual_disk,sas_enterprise,zfs_pool

# Keep drives, pools and your edits across restarts
python3 tools/mock-agent.py --port 9100 --data-dir ./mock-data --preload sata_hdd_devstat,zfs_pool

# Disable mDNS (useful if the real agent is already advertising)
python3 tools/mock-agent.py --port 9100 --no-mdns --preload sata_hdd,nvme
```

Open the dashboard in your browser at `http://localhost:<port>/` to add, remove, and modify drives and pools in real time.

---

## Running Alongside the Real Agent

The mock agent can run side by side with the real `smartha-agent` on the same machine. Just use a different port: the real agent stays on 9099, the mock on 9100 (or whatever you choose).

In Home Assistant, add the mock as a separate device:

**Settings → Devices & Services → Add Integration → SMART Sniffer** → enter `<your-mac-ip>`, port `9100`, and the token if you set one.

Or if mDNS is enabled and you have the `zeroconf` Python package installed, HA will auto-discover the mock agent as a second instance. The mock advertises itself as `smartha-mock-<hostname>`, never `smartha-<hostname>`: that one is the real agent's name and the integration's discovery unique id, and taking it would hide one of the two.

Both agents appear independently in HA. Your real drives keep reporting normally while you manipulate the fake ones.

---

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `9099` | Port to listen on |
| `--host` | `0.0.0.0` | Address to bind |
| `--token` | *(none)* | Bearer token. When set, every `GET /api/*` request needs `Authorization: Bearer <token>` |
| `--no-mdns` | *(off)* | Disable mDNS/Zeroconf advertisement |
| `--preload` | *(none)* | Comma-separated preset keys (drives and pools) to load on startup |
| `--data-dir` | *(none)* | Directory for `mock-drives.json`, which keeps drives, pools and every edit across restarts. When the file has anything in it, `--preload` is skipped. Without the flag the mock keeps everything in memory |

---

## Drive Presets

Each preset simulates a realistic drive with appropriate SMART attributes. All drives start in a healthy state; you degrade them manually via the dashboard or the control API.

| Preset Key | Drive | Protocol | Device Statistics | Data Written / Read | Notes |
|------------|-------|----------|-------------------|---------------------|-------|
| `sata_hdd` | Seagate Barracuda ST2000DM008 (2TB) | ATA | `absent` | attributes 241 / 242 (12 TB / 24 TB) | Spinning rust. Has Spin Retry Count (HDD only). Most likely real-world source of reallocated sectors. |
| `sata_hdd_devstat` | WDC Ultrastar DC HC530 14TB (`WUH721414ALE604`) | ATA | `present`, pages 1, 3, 4, 5 | Device Statistics page 1 (107.8 TB written) | The v0.8.0 gap-fill drive. No attribute 187 and no wear row, so the integration reads Reported Uncorrectable Errors from page 4 instead. The count starts at 0. Built from the corpus fixture `ata_devstat_wdc_unc` with a `FIXTURE-` serial. |
| `sata_ssd` | Samsung SSD 870 EVO 500GB | ATA | `absent` | attribute 241 (9.6 TB); read omitted, `no_source` | SATA SSD. Has Wear Leveling Count. No Spin Retry (SSDs don't spin). |
| `nvme` | Samsung 980 PRO 1TB | NVMe | `not_applicable` | data units (4.67 TB / 9.34 TB) | NVMe SSD. Completely different attribute set: Available Spare, Critical Warning, Media Errors, Percentage Used. |
| `nvme_usb` | Sabrent Rocket NVMe 500GB (USB-C) | NVMe | `not_applicable` | data units (1.2 TB / 2 TB) | NVMe in a USB-C enclosure where SMART passthrough works. Same attributes as `nvme`. |
| `usb_blocked` | WD Elements 2TB (USB) | ATA | `unavailable`, reason `failed` | omitted, `device_statistics_unavailable` | USB enclosure blocks SMART passthrough. Returns empty `smart_data`, so it shows as **UNSUPPORTED** in HA. |
| `virtual_disk` | QEMU HARDDISK | ATA | `absent` | omitted, `no_source` | Virtual disk (KVM/QEMU/VMware). No SMART data, so **UNSUPPORTED**. |
| `sas_enterprise` | Seagate Exos 10E2400 (SAS 10K RPM) | SCSI | `not_applicable` | omitted, `no_source` | Enterprise SAS. Uses SCSI log pages instead of ATA attributes. |

`device_statistics` and `derived` are computed the way the agent computes them (`agent/devstat.go`): NVMe volumes are data units × 512,000; an ATA drive with Device Statistics present uses Logical Sectors Written / Read × the logical block size; an ATA drive without them falls back to an allowlisted vendor attribute (with the same 512-byte, drive database, 1 % agreement and rate checks) or is omitted with the agent's reason. Edit the underlying numbers and `derived` follows.

The real agent reports `off` with reason `os` on macOS and `off` with reason `config` when Device Statistics are turned off in its config. To see either in the lab, set it on any drive with `PATCH /api/drives/{id}/devstat {"status": "off", "reason": "os"}`; it is saved with the drive.

## Pool Preset

| Preset Key | Pool | Notes |
|------------|------|-------|
| `zfs_pool` | `tank`, a mirror (`mirror-0`) of `sda` and `sdb` | ONLINE, zero errors, last scrub two days before it was added. Served on `/api/pools` in the agent's shape. A second one is named `tank2`, and so on. |

A pool is not a drive: it is kept in its own collection (saved with `--data-dir` too), and it only ever appears on `/api/pools`. Once a pool exists, `/api/health` lists `/api/pools` in `endpoints` and reports `pools` and `pools_status: "ok"`, as an agent with pool status on does. With no pools at all, the mock is an agent with pool status off: no endpoint, no keys, `/api/pools` answers 404.

**Vanish** takes a pool out of `/api/pools` but remembers it, so the endpoint stays advertised and the list is simply missing `tank`. That is what the integration reads as an exported pool: its State goes to MISSING, its Problem sensor turns on with "Not reported by zpool". **Restore** brings it back.

---

## Dashboard

The web dashboard is at `http://localhost:<port>/`. No login required: auth only applies to the `GET /api/*` endpoints that HA polls.

Each drive appears as a card with:

- **Model, serial, protocol, device path**, as HA sees them.
- **Predicted attention state.** A rough client-side copy of the rules, for the badge only. Home Assistant is the final word.
- **Device Statistics, Data Written and Data Read** in one line: the status (and reason), and each volume in TB with its source, or the reason it was left out.
- **SMART Status dropdown** to toggle PASSED/FAILED.
- **Attribute input fields** for every SMART attribute in the drive's table, with threshold hints next to each field.
- **Devstat Reported Uncorrectable**, on drives with Device Statistics pages.
- **Data Written (TB)**, on drives that have a source for it.

Each pool appears as a card with its state, a Vanish / Restore button, a State dropdown, and per-disk state and read, write and checksum error fields.

The status bar at the top shows the drive and pool counts, the HA poll count and the timestamp of the last poll, so you can confirm the integration is actively talking to the mock.

---

## Testing Workflows

### Test 1: Healthy → MAYBE → YES → NO (ATA drive)

1. Start with `--preload sata_hdd`. Drive shows **NO** in HA.
2. In the dashboard, set **Spin Retry Count** to `1`. Wait one poll.
3. HA sensor flips to **MAYBE**. Persistent notification fires (warning).
4. Set **Reallocated Sector Ct** to `5`. Wait one poll.
5. HA sensor escalates to **YES**. Notification updates to critical.
6. Set both values back to `0`. Wait one poll.
7. HA sensor returns to **NO**. Notification auto-dismisses.

### Test 2: NVMe spare depletion

1. Start with `--preload nvme`. Drive shows **NO**.
2. Set **Available Spare** to `15` (below the 20% warning level). Wait one poll.
3. HA shows **MAYBE**.
4. Set **Available Spare** to `8` (at or below the drive's 10% threshold). Wait one poll.
5. HA escalates to **YES**.
6. Set **Available Spare** back to `100`. Sensor returns to **NO**.

### Test 3: NVMe critical warning and media errors

1. Start with `--preload nvme`. Drive shows **NO**.
2. Set **Critical Warning** to `1`. Wait one poll.
3. HA shows **YES** with "NVMe critical warning flag set (0x01)".
4. Set **Critical Warning** back to `0`, set **Media Errors** to `3`. Wait one poll.
5. Still **YES**, with "NVMe media errors: 3 (expected 0)".
6. Set **Media Errors** back to `0`. Returns to **NO**.

### Test 4: USB / Unsupported drives

1. Start with `--preload usb_blocked,virtual_disk`. Both show **UNSUPPORTED**.
2. Verify HA creates devices but shows "Unsupported" attention state.
3. Remove the USB drive from the dashboard. Verify HA marks entity as unavailable.

### Test 5: SMART status FAILED

1. Start with `--preload sata_ssd`. SMART Status shows "PASSED" in HA.
2. In the dashboard, change **SMART Status** dropdown to **FAILED**.
3. HA sensor updates to "FAILED". Attention state depends on attribute values.

### Test 6: Bearer token auth

1. Start with `--token testtoken123 --preload sata_hdd`.
2. Add to HA with the matching token. Data flows normally.
3. Change the token in HA to something wrong. HA shows "cannot connect" / unavailable.
4. Fix the token. Data resumes.

### Test 7: Multiple drives, mixed states

1. Start with `--preload sata_hdd,sata_ssd,nvme,usb_blocked`.
2. Leave HDD healthy (**NO**), degrade SSD (**MAYBE**), fail NVMe (**YES**), USB stays **UNSUPPORTED**.
3. Verify each drive shows the correct independent attention state in HA.
4. Verify notifications: one warning (SSD), one critical (NVMe), one informational (USB). No notification for the healthy HDD.

### Test 8: Auto-discovery (mDNS)

1. Install `zeroconf`: `pip install zeroconf`
2. Start the mock without `--no-mdns` on a port HA can reach.
3. HA should show a discovery notification for `smartha-mock-<hostname>`.
4. Click **Add**. If a token is set, you'll be prompted for it.

### Test 9: Gap-fill from Device Statistics

1. Start with `--preload sata_hdd_devstat`. Drive shows **NO**. Its Device Statistics read `present` and Data Written shows 107.83 TB from `ata_device_statistics`.
2. Set **Devstat Reported Uncorrectable** to `18` (or `PATCH /api/drives/{id}/devstat {"reported_uncorrectable": 18}`). Wait one poll.
3. HA goes to **YES** with "Reported Uncorrectable Errors: 18 (expected 0; from device statistics)", and the notification ends with the one-time line "First reading from this drive's Device Statistics."
4. Simulate a failed read with `PATCH /api/drives/{id}/devstat {"status": "unavailable", "reason": "timeout"}`. HA keeps the held count, so the drive stays **YES**, and Data Written and Data Read keep their last values.
5. `{"status": "default"}` puts the drive back to `present`. Set the count to `0` and the drive returns to **NO**.

### Test 10: ZFS pool

1. Start with `--preload zfs_pool`. HA creates a `tank` device: State ONLINE, Problem off.
2. Set `sda` to **FAULTED** with `12` read errors (or `PATCH /api/pools/tank {"state": "DEGRADED", "devices": [{"name": "sda", "state": "FAULTED", "read": 12}]}`). Wait one poll.
3. HA shows State DEGRADED, Problem on, and a notification listing "State: DEGRADED", "Read errors: 12", "mirror-0: DEGRADED" and "sda: FAULTED, 12 read errors".
4. Press **Vanish**. HA shows State MISSING with "Not reported by zpool".
5. Press **Restore**, set `sda` back to ONLINE with `0` read errors. The notification dismisses.

### Test 11: Data Written

1. Start with `--preload nvme`. Data Written shows 4.67 TB, source `nvme`.
2. Set **Data Written (TB)** to `12.5` (or `PATCH /api/drives/{id}/derived {"host_writes_tb": 12.5}`). Wait one poll.
3. HA shows 12.5 TB. The source does not change.

---

## Attribute Reference

The default thresholds. Every one of them can be changed in the integration's options, and the integration (`attention.py`) is the authority; this table is a guide for driving the mock.

### ATA Attributes (SATA HDD / SSD)

| Attribute | Threshold | Attention State | Notes |
|-----------|-----------|-----------------|-------|
| Reallocated_Sector_Ct | ≥ 1 | **YES** (critical) | Bad sectors remapped to spares. Any count means physical damage. |
| Current_Pending_Sector | ≥ 1 | **YES** (critical) | Sectors waiting for reallocation. Active data integrity risk. |
| Offline_Uncorrectable | ≥ 1 | **YES** (critical) | Unrecoverable read/write errors found during offline testing. |
| Reported_Uncorrect (187) | ≥ 1 | **YES** (critical) | Errors the drive could not correct and reported to the host. |
| Reallocated_Event_Count | ≥ 1 | **MAYBE** (warning) | Number of reallocation events. Early warning of developing issues. |
| Spin_Retry_Count | ≥ 1 | **MAYBE** (warning) | Motor spin-up retries. HDD only, a sign of mechanical stress. |
| Command_Timeout | > 100 | **MAYBE** (warning) | Low counts are common on healthy drives (sleep and wake, link power management). |
| Wear_Leveling_Count | ≥ 90 % used | **MAYBE** (warning) | Read from the normalized VALUE (100 minus VALUE is percent used), not the raw count. The dashboard edits raw values, so use the Device Statistics endurance count below to try the wear rule. |
| Temperature_Celsius | n/a | Info only | Current drive temperature in °C. |
| Power_On_Hours | n/a | Info only | Total hours powered on. |
| Power_Cycle_Count | n/a | Info only | Total power on/off cycles. |

### Device Statistics (gap-fill, ATA with `present` pages)

A Device Statistics reading counts only when the attribute table has a gap for it. Reasons from it end in "from device statistics".

| `PATCH .../devstat` key | Page, offset | Counts when | Attention State |
|-------------------------|--------------|-------------|-----------------|
| `reported_uncorrectable` | 4, 0x008 | the table has no attribute 187 | **YES** at ≥ 1 |
| `reallocated_logical_sectors` | 3, 0x020 | the table has no attribute 5 | **YES** at ≥ 1 |
| `percentage_used_endurance` | 7, 0x008 | the table has no usable wear row | **MAYBE** at ≥ 90 |

On `sata_hdd_devstat` the first and third count; attribute 5 is in its table, so the second only shows as an attribute on the Reallocated sensor.

### NVMe Attributes

| Attribute | Threshold | Attention State | Notes |
|-----------|-----------|-----------------|-------|
| critical_warning | ≠ 0 | **YES** (critical) | Bitmask. Any bit set means a critical condition. |
| media_errors | ≥ 1 | **YES** (critical) | Unrecoverable media read/write errors. A value at or above 2^64 is unreadable, not a count: the sensor shows unknown and it never triggers Attention. |
| available_spare | ≤ threshold | **YES** (critical) | Spare block pool at or below the drive's own threshold: end of life. |
| available_spare | < 20 | **MAYBE** (warning) | Spare blocks running low. Plan a replacement. |
| percentage_used | ≥ 90 | **MAYBE** (warning) | Approaching rated write endurance limit. |
| available_spare_threshold | n/a | Info only | Manufacturer-set minimum spare (%). |
| temperature | n/a | Info only | Current drive temperature in °C. |
| power_on_hours | n/a | Info only | Total hours powered on. |
| power_cycles | n/a | Info only | Total power on/off cycles. |

---

## API Endpoints

The mock serves the same API as the real agent. Point the HA integration at it identically.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | Yes | The agent's health payload, see below |
| `/api/drives` | GET | Yes | List all drives: `id`, `device_path`, `model`, `serial`, `protocol`, `readable` |
| `/api/drives/{id}` | GET | Yes | One drive: the list fields plus `last_updated`, `smart_data`, `device_statistics` and `derived` |
| `/api/pools` | GET | Yes | The pools that are not vanished, in the agent's shape. 404 while the mock has no pools |
| `/` | GET | No | Web dashboard |

### `/api/health`

```json
{
  "status": "ok", "version": "0.8.0-mock", "os": "mock", "uptime_seconds": 42,
  "endpoints": ["/api/health", "/api/drives", "/api/drives/{id}", "/api/pools"],
  "drives": 4, "filesystems": 0, "pools": 1, "pools_status": "ok",
  "mock": true, "hostname": "labhost", "port": 9100, "auth_enabled": false, "drive_count": 4
}
```

The keys up to `pools_status` are the real agent's (`healthResponse` in `agent/main.go`). `/api/pools`, `pools` and `pools_status` are there only while the mock has at least one pool; `pools` counts the ones served. The last five keys are not in the real agent: they are for the app's Control Center and the dashboard.

### `device_statistics` and `derived`

```json
"device_statistics": {
  "status": "present", "complete": true, "exit_status": 0, "logical_block_size": 512,
  "pages": [{"number": 4, "name": "General Errors Statistics", "revision": 1, "table": [
    {"offset": 8, "name": "Number of Reported Uncorrectable Errors", "size": 4, "value": 0,
     "flags": {"value": 192, "string": "V--- ", "valid": true, "normalized": false,
               "supports_dsn": false, "monitored_condition_met": false}}]}]
},
"derived": {
  "host_writes": {"bytes": 107830377472000, "source": "ata_device_statistics"},
  "host_reads": {"bytes": 791804967834112, "source": "ata_device_statistics"}
}
```

`status` is `present`, `absent`, `unavailable` (with `reason` `failed`, `timeout`, `standby`, `mismatch` or `stopped`), `not_applicable` or `off` (with `reason` `config` or `os`). Only `present` carries `complete`, `exit_status`, `logical_block_size` and `pages` (smartctl's raw `ata_device_statistics.pages`).

Each of `host_writes` and `host_reads` is either `{"bytes", "source"}` (plus `attribute_id` and `attribute_name` when `source` is `ata_attribute`) or replaced by `host_writes_omitted` / `host_reads_omitted` with the agent's reason: `no_source`, `device_statistics_unavailable`, `entry_invalid`, `block_size`, `not_in_database`, `excluded_model`, `power_on_hours_unknown`, `rate_bound`, `candidates_disagree` or `overflow`.

### `/api/pools`

```json
[{
  "name": "tank", "state": "ONLINE", "status": null, "action": null,
  "read_errors": 0, "write_errors": 0, "checksum_errors": 0, "data_errors": 0,
  "scan_function": "SCRUB", "scan_state": "FINISHED", "scrub_in_progress": false,
  "last_scrub_end": "2026-10-01T16:48:48Z", "last_scrub_repaired": 0, "last_scrub_errors": 0,
  "problem_vdevs": []
}]
```

The agent's `PoolInfo` (`agent/zpool_status.go`). The error totals are the sums over the pool's disks. `problem_vdevs` lists `mirror-0` (type `mirror`) when a disk is not ONLINE, and each disk that is not ONLINE or has errors, each as `{"name", "type", "state", "read_errors", "write_errors", "checksum_errors"}`.

### Mock Control API

Every control route answers under both `/api/...` and `/mock/...`. The dashboard uses `/mock/...`; the SMART Sniffer app's proxy rewrites `/mock/` to `/api/` on the way in, so the mock sees `/api/...`. The control routes never ask for the token.

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/mock/state` (or `/api/state`, `/api/lab`) | GET | | Full state: `version`, `drives` and `pools` (each the served payload plus a `lab` block), `presets`, `poll_count`, `last_poll`, `port`, `auth`. Only `/mock/state` skips auth |
| `/api/drives` | POST | `{"preset": "sata_hdd"}` | Add a drive, or a pool when the preset is `zfs_pool`. Answers `{"id", "kind"}` (and `"name"` for a pool) |
| `/api/drives/{id}` | DELETE | | Remove a drive |
| `/api/drives/{id}/smart` (or `/api/drives/{id}`) | PATCH | `{"Reallocated_Sector_Ct": 5, "smart_passed": false}` | Set ATA raw values by attribute name, NVMe log fields by key, and `smart_passed`. Answers `{"ok": true}` |
| `/api/drives/{id}/devstat` | PATCH | `{"reported_uncorrectable": 18}` | Set the Device Statistics counts `reported_uncorrectable`, `reallocated_logical_sectors`, `percentage_used_endurance` (the entry is created if missing). Also `{"status": "unavailable", "reason": "timeout"}` to simulate a failed read, `{"status": "default"}` to go back to the preset's own status, and `"complete": false` for a partial read. Answers `{"ok", "device_statistics", "derived"}` |
| `/api/drives/{id}/derived` | PATCH | `{"host_writes_tb": 4.67}` | Set Data Written and/or Data Read (`host_reads_tb`) in decimal TB (or `host_writes_bytes` / `host_reads_bytes`). The value is written into whatever the volume comes from, so the source stays the same. 409 when the drive has no source. Answers `{"ok", "derived"}` |
| `/api/pools` | POST | `{"preset": "zfs_pool", "name": "tank"}` | Add a pool; `name` is optional |
| `/api/pools/{name}` | GET | | One pool, vanished or not, with its `lab` block |
| `/api/pools/{name}` | PATCH | `{"state": "DEGRADED", "devices": [{"name": "sda", "state": "FAULTED", "read": 12, "write": 0, "cksum": 0}], "errors": "No known data errors"}` | Change a pool. Any of `state`, `devices` (by disk name), `errors` (the zpool "errors:" text or a count), `data_errors`, `status`, `action` and the scrub fields. A device change without `state` recomputes it. Answers `{"ok", "pool"}` |
| `/api/pools/{name}/vanish` | POST | | Take the pool out of `/api/pools`, keeping it in the store |
| `/api/pools/{name}/restore` | POST | | Bring a vanished pool back |
| `/api/pools/{name}` | DELETE | | Remove the pool for good. With no pools left, pool status is off again |

Bad bodies get a 400 with an `error` text; an unknown drive or pool gets a 404.

---

## Troubleshooting

**"Address already in use":** another process is on that port. Use a different `--port` or kill the previous mock instance.

**HA not seeing changes:** changes take effect on the next poll cycle. Check the dashboard status bar for "Last poll" to confirm HA is polling. Default poll interval is 60 seconds; you can lower it in the integration's options flow.

**mDNS not working:** make sure `pip install zeroconf` succeeded and you didn't pass `--no-mdns`. If discovery still does not show the mock, add it manually in HA.

**Auth mismatch:** if you started with `--token`, the same token must be entered in the HA integration config. The dashboard status bar shows whether auth is on or off.

**Old drives after a restart:** with `--data-dir`, the saved drives come back and `--preload` is skipped. Delete `mock-drives.json` in that directory to start fresh.
