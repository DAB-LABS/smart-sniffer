# SMART Sniffer Mock Agent

A fake `smartha-agent` for testing the Home Assistant integration without waiting for real drives to degrade. Serves the same REST API as the real Go agent but with fully controllable fake drive data and a built-in web dashboard.

**Location:** `tools/mock-agent.py`
**Requirements:** Python 3.9+ (stdlib only — no pip install needed)
**Optional:** `pip install zeroconf` for mDNS auto-discovery advertisement

---

## Quick Start

```bash
# Basic — dashboard at http://localhost:9099
python3 tools/mock-agent.py --preload sata_hdd,nvme,usb_blocked

# Different port (run alongside the real agent on 9099)
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,nvme

# With bearer token auth
python3 tools/mock-agent.py --port 9100 --token mysecrettoken123 --preload sata_ssd

# All 7 drive presets
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,sata_ssd,nvme,nvme_usb,usb_blocked,virtual_disk,sas_enterprise

# Disable mDNS (useful if real agent is already advertising)
python3 tools/mock-agent.py --port 9100 --no-mdns --preload sata_hdd,nvme
```

Open the dashboard in your browser at `http://localhost:<port>/` to add, remove, and modify drives in real time.

---

## Running Alongside the Real Agent

The mock agent can run side-by-side with the real `smartha-agent` on the same machine — just use a different port. The real agent stays on 9099, the mock on 9100 (or whatever you choose).

In Home Assistant, add the mock as a separate device:

**Settings → Devices & Services → Add Integration → SMART Sniffer** → enter `<your-mac-ip>`, port `9100`, and the token if you set one.

Or if mDNS is enabled and you have the `zeroconf` Python package installed, HA will auto-discover the mock agent as a second instance.

Both agents appear independently in HA. Your real drives keep reporting normally while you manipulate the fake ones.

---

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `9099` | Port to listen on |
| `--token` | *(none)* | Bearer token — when set, all `/api/*` requests require `Authorization: Bearer <token>` |
| `--no-mdns` | *(off)* | Disable mDNS/Zeroconf advertisement |
| `--preload` | *(none)* | Comma-separated preset keys to load on startup |

---

## Drive Presets

Each preset simulates a realistic drive with appropriate SMART attributes. All drives start in a healthy state — you degrade them manually via the dashboard.

| Preset Key | Drive | Protocol | Notes |
|------------|-------|----------|-------|
| `sata_hdd` | Seagate Barracuda ST2000DM008 (2TB) | ATA | Spinning rust. Has Spin Retry Count (HDD-only attribute). Most likely real-world source of reallocated sectors. |
| `sata_ssd` | Samsung SSD 870 EVO 500GB | ATA | SATA SSD. Has Wear Leveling Count. No Spin Retry (SSDs don't spin). |
| `nvme` | Samsung 980 PRO 1TB | NVMe | NVMe SSD. Completely different attribute set: Available Spare, Critical Warning, Media Errors, Percentage Used. |
| `nvme_usb` | Sabrent Rocket NVMe 500GB (USB-C) | NVMe | NVMe in USB-C enclosure where SMART passthrough works. Same attributes as `nvme`. |
| `usb_blocked` | WD Elements 2TB (USB) | ATA | USB enclosure blocks SMART passthrough. Returns empty `smart_data` → shows as **UNSUPPORTED** in HA. |
| `virtual_disk` | QEMU HARDDISK | ATA | Virtual disk (KVM/QEMU/VMware). No SMART data → **UNSUPPORTED**. |
| `sas_enterprise` | Seagate Exos 10E2400 (SAS 10K RPM) | SCSI | Enterprise SAS. Uses SCSI log pages instead of ATA attributes. Currently shows as **UNSUPPORTED** until SAS support is added. |

---

## Dashboard

The web dashboard is at `http://localhost:<port>/`. No login required — auth only applies to the `/api/*` endpoints that HA polls.

Each drive appears as a card with:

- **Model, serial, protocol, device path** — matches what HA sees.
- **Predicted attention state** — the dashboard runs the same classification logic client-side so you can see what HA will show (NO, MAYBE, YES, UNSUPPORTED) before the next poll.
- **SMART Status dropdown** — toggle PASSED/FAILED.
- **Attribute input fields** — every SMART attribute the integration monitors, with threshold hints next to each field.

The status bar at the top shows HA poll count and the timestamp of the last poll, so you can confirm the integration is actively talking to the mock.

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
2. Set **Available Spare** to `15` (below 20% threshold). Wait one poll.
3. HA shows **MAYBE** — "NVMe available spare low: 15% remaining".
4. Set **Available Spare** to `8` (at or below the 10% drive threshold). Wait one poll.
5. HA escalates to **YES** — "NVMe available spare at or below drive threshold".
6. Set **Available Spare** back to `100`. Sensor returns to **NO**.

### Test 3: NVMe critical warning and media errors

1. Start with `--preload nvme`. Drive shows **NO**.
2. Set **Critical Warning** to `1`. Wait one poll.
3. HA shows **YES** — "NVMe critical warning flag set (0x01)".
4. Set **Critical Warning** back to `0`, set **Media Errors** to `3`. Wait one poll.
5. Still **YES** — "NVMe media errors: 3 (expected 0)".
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
2. Add to HA with the matching token — data flows normally.
3. Change the token in HA to something wrong — HA shows "cannot connect" / unavailable.
4. Fix the token — data resumes.

### Test 7: Multiple drives, mixed states

1. Start with `--preload sata_hdd,sata_ssd,nvme,usb_blocked`.
2. Leave HDD healthy (**NO**), degrade SSD (**MAYBE**), fail NVMe (**YES**), USB stays **UNSUPPORTED**.
3. Verify each drive shows the correct independent attention state in HA.
4. Verify notifications: one warning (SSD), one critical (NVMe), one informational (USB). No notification for the healthy HDD.

### Test 8: Auto-discovery (mDNS)

1. Install `zeroconf`: `pip install zeroconf`
2. Start mock without `--no-mdns` on a port HA can reach.
3. HA should show a discovery notification for the mock agent.
4. Click **Add** — if token is set, you'll be prompted for it.

---

## Attribute Reference

### ATA Attributes (SATA HDD / SSD)

| Attribute | Threshold | Attention State | Notes |
|-----------|-----------|-----------------|-------|
| Reallocated_Sector_Ct | ≥ 1 | **YES** (critical) | Bad sectors remapped to spares. Any count means physical damage. |
| Current_Pending_Sector | ≥ 1 | **YES** (critical) | Sectors waiting for reallocation. Active data integrity risk. |
| Offline_Uncorrectable | ≥ 1 | **YES** (critical) | Unrecoverable read/write errors found during offline testing. |
| Reallocated_Event_Count | ≥ 1 | **MAYBE** (warning) | Number of reallocation events. Early warning of developing issues. |
| Spin_Retry_Count | ≥ 1 | **MAYBE** (warning) | Motor spin-up retries. HDD only — indicates mechanical stress. |
| Command_Timeout | ≥ 1 | **MAYBE** (warning) | Internal command timeouts. Can indicate controller or interface issues. |
| Wear_Leveling_Count | — | Info only | SSD endurance indicator (percentage). Not a direct trigger. |
| Temperature_Celsius | — | Info only | Current drive temperature in °C. |
| Power_On_Hours | — | Info only | Total hours powered on. |
| Power_Cycle_Count | — | Info only | Total power on/off cycles. |

### NVMe Attributes

| Attribute | Threshold | Attention State | Notes |
|-----------|-----------|-----------------|-------|
| critical_warning | ≠ 0 | **YES** (critical) | Bitmask — any bit set means a critical condition. |
| media_errors | ≥ 1 | **YES** (critical) | Unrecoverable media read/write errors. |
| available_spare | ≤ threshold | **YES** (critical) | Spare block pool at or below drive's threshold — end of life. |
| available_spare | < 20 | **MAYBE** (warning) | Spare blocks running low — plan replacement. |
| percentage_used | ≥ 90 | **MAYBE** (warning) | Approaching rated write endurance limit. |
| available_spare_threshold | — | Info only | Manufacturer-set minimum spare (%). |
| temperature | — | Info only | Current drive temperature in °C. |
| power_on_hours | — | Info only | Total hours powered on. |
| power_cycles | — | Info only | Total power on/off cycles. |

---

## API Endpoints

The mock serves the same API as the real agent. Point the HA integration at it identically.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | Yes | Health check — returns `{"status": "ok"}` |
| `/api/drives` | GET | Yes | List all drives (summary: id, model, serial, protocol, device_path) |
| `/api/drives/{id}` | GET | Yes | Full drive detail including `smart_data` |
| `/` | GET | No | Web dashboard |

### Mock Control API (dashboard uses these internally)

| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/mock/state` | GET | — | Full state dump (drives, poll count, config) |
| `/mock/drives` | POST | `{"preset": "sata_hdd"}` | Add a new drive from a preset |
| `/mock/drives/{id}` | PATCH | `{"Reallocated_Sector_Ct": 5}` | Update SMART attributes |
| `/mock/drives/{id}` | DELETE | — | Remove a drive |

---

## Troubleshooting

**"Address already in use"** — another process is on that port. Use a different `--port` or kill the previous mock instance.

**HA not seeing changes** — changes take effect on the next poll cycle. Check the dashboard status bar for "Last poll" to confirm HA is polling. Default poll interval is 60 seconds — you can lower it in the integration's options flow.

**mDNS not working** — make sure `pip install zeroconf` succeeded and you didn't pass `--no-mdns`. The real agent on port 9099 may also be holding the mDNS service name — try `--no-mdns` on the mock and add it manually in HA.

**Auth mismatch** — if you started with `--token`, the same token must be entered in the HA integration config. The dashboard status bar shows whether auth is on or off.
