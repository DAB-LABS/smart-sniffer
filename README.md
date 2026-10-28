<p align="center">
  <img src="images/SMARTsniffer.png" alt="SMART Sniffer" width="100%">
</p>

**Proactive disk health monitoring for Home Assistant.**

[![Release](https://img.shields.io/github/v/release/DAB-LABS/smart-sniffer?style=flat-square)](https://github.com/DAB-LABS/smart-sniffer/releases/latest)
![Beta](https://img.shields.io/badge/status-beta-orange?style=flat-square)
[![Build](https://img.shields.io/github/actions/workflow/status/DAB-LABS/smart-sniffer/release.yml?style=flat-square&label=build)](https://github.com/DAB-LABS/smart-sniffer/actions/workflows/release.yml)
[![License](https://img.shields.io/github/license/DAB-LABS/smart-sniffer?style=flat-square)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5?style=flat-square)](https://hacs.xyz)

---

SMART drives report "PASSED" right up until the day they fail. SMART Sniffer watches the [early warning signs](https://www.backblaze.com/b2/hard-drive-test-data.html) a pass/fail check misses and reports them to Home Assistant before a drive actually goes. One agent per machine, a health score per drive, and a notification the moment something changes. No automations required.

<br>

## What you get

A lightweight Go agent on each machine reads `smartctl` and serves the result over HTTP; the Home Assistant integration polls every agent you add and turns each drive, ZFS pool and filesystem into its own device with sensors and diagnostics.

<p align="center">
  <img src="images/smartsniffer-architecture.png" alt="One smartha-agent per machine (Proxmox host, NAS, Mac, and the SMART Sniffer App inside Home Assistant OS) reporting over JSON/HTTP to the smart_sniffer integration" width="100%">
</p>

- **Attention Needed**, the proactive health sensor, watches leading indicators of failure across ATA, SATA, NVMe and SAS drives and reports `NO`, `MAYBE`, `YES` or `UNSUPPORTED`. A persistent notification fires the moment a drive's state changes, escalates on worsening readings, and clears itself when the drive recovers. No automations or blueprints needed.
- **The Control Center**, a SMART Sniffer page in the Home Assistant sidebar (admins only), shows every drive, ZFS pool and filesystem on every machine as a card, with the reason behind its health. See [The Control Center](#the-control-center).
- **Replicas** let you try notifications and automations against test drives and pools with readings you choose, no failing hardware required.
- **ZFS pools** show up as their own device with state, error counts and last scrub; a notification names the pool and the failing disks when one degrades.
- **Data Written** tracks how much each drive has had written to it over its life, with a daily and monthly rate, read from the drive's own statistics log (needs agent v0.8.0+).
- **Disk usage** monitoring is opt-in per mountpoint: total, used, available and percentage.
- Agents find themselves and Home Assistant automatically over mDNS, no manual IP entry.
- A service call hands any AI assistant the full SMART picture for a health report, with zero parsing. See [AI & Automation](#ai--automation).
- An optional bearer token authenticates each agent, and every binary download is SHA256-verified.

<details>
<summary><strong>Entities per drive and per agent</strong></summary>

<br>

Each drive gets its own HA device, and entities are created only for what that drive actually reports, so nothing claims a feature a drive does not have. A drive that was asleep at setup, a pool created later, or a newly configured mountpoint is picked up the next time the agent reports it; no reload needed.

**Sensors:** Attention Needed, Health (OK / Problem / Unknown), Standby, Temperature, Power-On Hours, SMART Status, Data Written, Data Read (disabled by default).

**Diagnostic sensors** (conditional on what the drive reports): Reallocated Sector Count, Reported Uncorrectable Errors, Wear Leveling / Percentage Used, Power Cycle Count, Reallocated Event Count, Spin Retry Count, Command Timeout, Available Spare, Available Spare Threshold, Current Pending Sector Count.

**Vendor-specific attributes** for any drive smartctl's database recognizes: every named SMART attribute not already covered above, created disabled by default. Enable what you need from the device page.

**Disk usage** (per agent host, needs filesystems configured): one percentage-used sensor per mountpoint, with `mountpoint`, `device`, `fstype` and the raw byte counts as attributes.

**Per agent:** Agent Status (connectivity, enabled by default), Agent Version, and OS, plus disabled-by-default diagnostics for last-seen time, IP, port, HA poll interval and whether a bearer token is set.

</details>

<details>
<summary><strong>Attention Needed: how it classifies drives</strong></summary>

<br>

| State | Meaning | Action |
|-------|---------|--------|
| **NO** | All monitored indicators clear | None required |
| **MAYBE** | Early degradation signals detected | Monitor closely, plan replacement |
| **YES** | Data integrity at risk | **Back up immediately** |
| **UNSUPPORTED** | No usable SMART data | Common with USB enclosures |

**Triggers YES:** reallocated sectors, pending sectors, uncorrectable errors, NVMe critical_warning or media_errors, spare depletion below threshold.

**Triggers MAYBE:** reallocated events, spin retry count, command timeouts, NVMe spare under 20%, NVMe percentage_used at 90% or above.

If a drive carries old, stable damage you have decided to live with, raise its limit instead of ignoring the alert: **Settings > Devices & Services > SMART Sniffer > Configure > Alert thresholds**, then pick the drive. Each field shows its current reading and the built-in default; type the default back in to clear your override. Full specification: [attention-severity-logic.md](docs/attention-severity-logic.md).

</details>

<details>
<summary><strong>Removing drives and devices</strong></summary>

<br>

A drive you have taken out can be removed with **Remove device** on its device page, once the agent is online and no longer reports it. To stop monitoring a drive that is still installed, add it to `exclude_devices` in the agent's config instead. ZFS pool devices work the same way once the agent no longer reports the pool. To remove an agent and everything under it, delete its entry under **Settings > Devices & Services > SMART Sniffer**.

</details>

<details>
<summary><strong>Agent configuration</strong></summary>

<br>

Create `config.yaml` in the working directory or `/etc/smartha-agent/`:

```yaml
port: 9099
token: "your-secret-token"    # optional -- omit to disable auth
scan_interval: 60s
standby_mode: standby          # optional -- never, standby, sleep, or idle
advertise_interface: eth0      # optional -- restrict mDNS to this interface
exclude_devices:               # optional -- set by installer's drive picker
  - /dev/sdb
filesystems:                   # optional -- set by installer's disk usage picker
  - path: /
    device: /dev/sda1
```

| Option | Default | What it does |
|---|---|---|
| `port` | 9099 | Port the agent serves on. |
| `token` | none | Bearer token required on every request. Leave unset to serve openly. |
| `scan_interval` | 60s | How often the agent reads `smartctl` (Go duration syntax: `30s`, `5m`, `1h`). |
| `standby_mode` | never | `standby` / `sleep` / `idle` skip waking a spun-down drive during a poll and serve its last cached reading instead; `never` wakes every drive on every poll. The very first poll after startup always wakes everything once, to collect a baseline. |
| `advertise_interface` | auto | Restricts mDNS to one interface. Set this if auto-discovery picks the wrong address on a machine with Docker, a VPN, or several NICs. |
| `agent_id` | random | Pins the agent's identity (8-64 characters, a-z/0-9/-) instead of the random one generated on first start. |
| `exclude_devices` | none | Device paths skipped on every scan; symlinks resolve at startup, so `/dev/disk/by-id/...` and `/dev/sdX` both match. |
| `smartctl_path` | auto | Full path to `smartctl`, when it is not on PATH or in the usual install locations. |
| `zfs_pool_status` | true | Reports ZFS pool health when `zpool` is found. Set false to turn it off. |
| `device_statistics` | true | Reads each SATA drive's Device Statistics log for Data Written and some error counts. Always off on macOS. |
| `filesystems` | none | Mountpoints to report disk usage for; set by the installer's picker. |
| `mount_prefix` | none | For an agent running in a container: where the host root is mounted, so disk usage reflects the host rather than the container. |
| `verbose` | false | Restores per-cycle logging. By default a repeated message logs once, then at most hourly. |

All options can also be set via CLI flags: `--port`, `--token`, `--scan-interval`, `--interface`, `--config`, `--verbose`, `--no-device-statistics`.

**Running in a container**, to report the host's disk usage rather than the container's own filesystem:

```bash
docker run -v /:/host:ro -e SMARTHA_MOUNT_PREFIX=/host ...
```

**smartctl location:** the agent checks PATH, then the usual install locations for the platform (Synology, QNAP, Unraid, Homebrew, MacPorts, or on Windows, Program Files, Chocolatey, winget and Scoop). Set `smartctl_path` if yours lives somewhere else, or if the agent needs smartctl 7.0+ and the one on PATH is older.

**API endpoints**, served over HTTP on the configured port:

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Agent status, version, agent ID, drive/filesystem/pool counts |
| `GET /api/drives` | Summary list of all discovered drives |
| `GET /api/drives/{id}` | Full SMART data for a single drive |
| `GET /api/filesystems` | Disk usage for all configured mountpoints |
| `GET /api/pools` | ZFS pool health: state, error counts, problem disks, last scrub |

**Service management:**

| Platform | Status | Logs | Restart |
|----------|--------|------|---------|
| Linux | `systemctl status smartha-agent` | `journalctl -u smartha-agent -f` | `systemctl restart smartha-agent` |
| macOS | `launchctl list \| grep smartha` | `tail -f /var/log/smartha-agent.log` | `sudo launchctl kickstart -k system/com.dablabs.smartha-agent` |
| Windows | `Get-Service SmartHA-Agent` | Event Viewer | `Restart-Service SmartHA-Agent` |

</details>

<details>
<summary><strong>Building from source</strong></summary>

<br>

Requires Go 1.22+.

```bash
cd agent
make              # build for current platform
make all          # cross-compile all targets
make release      # build all + generate SHA256 checksums
make clean        # remove build artifacts
```

Binaries output to `agent/build/`, for Linux (amd64, arm64), macOS (amd64, arm64) and Windows (amd64).

</details>

<br>

## App or integration?

Same Control Center either way. The App adds the drives of the box Home Assistant itself runs on; the integration covers everything else.

| Your setup | What to install |
|---|---|
| Drives on a NAS, Proxmox host, PC or Mac | The agent on each of those machines, plus this integration |
| Home Assistant OS or Supervised, and you want that box's own drives too | This integration, plus the [SMART Sniffer App](https://github.com/DAB-LABS/smart-sniffer-app) |
| Home Assistant Container or Core | This integration. For the drives of the HA machine itself, install the agent there directly |
| You want to try alerts and automations without a failing drive | This integration: **+ > Replica** |

The integration owns every machine and is where the Control Center, thresholds, replicas and Agentic access live; it needs at least one machine added before Home Assistant will load it. The App runs the agent for the HA box itself, shows those drives on its own page, and hands them to the integration automatically once both are installed.

<br>

## Quick start

**Requires:** `smartmontools` 7.0+ on each monitored machine, for JSON support. The installer handles this on macOS (Homebrew) and most Linux (apt/dnf/yum); run `smartctl --version` to check if you are unsure, and grab a newer build from the [smartmontools releases page](https://www.smartmontools.org/wiki/Download) if yours is older.

### 1. Install the agent

Run on each machine you want to monitor:

```bash
curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
```

The installer detects your OS and architecture, downloads and verifies the right binary, installs `smartmontools` if missing, prompts for configuration, and sets up a system service. Give it up to 90 seconds for the first pass over your drives.

<details>
<summary>Installer options, Windows, macOS Gatekeeper, pinning a version, uninstalling</summary>

<br>

**Custom install location**, for a read-only system area (TrueNAS and similar appliance OSes fall back to `/root/smartha-agent` automatically, which does not survive a major upgrade; install to a pool dataset instead):

```bash
curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash -s -- --install-dir=/mnt/tank/apps/smartha-agent
```

**Windows (PowerShell as Admin, v0.5.1+):**

```powershell
irm https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.ps1 | iex
```

Run this from PowerShell, not Command Prompt. If the agent runs as a service and smartctl was installed via Scoop or winget under your own user account, the installer writes its path into `config.yaml` for you, since the service account cannot see your user PATH. The installer also opens the agent's port (9099 by default) in Windows Firewall.

**macOS "unidentified developer" warning:** the binary is not notarized, so Gatekeeper blocks it on first run. Remove the quarantine flag and restart the service:

```bash
sudo xattr -d com.apple.quarantine /usr/local/bin/smartha-agent
sudo launchctl kickstart -k system/com.dablabs.smartha-agent
```

Or go to **System Settings > Privacy & Security**, find the blocked app, and click **Open Anyway**.

**Pin a version:**

```bash
VERSION=0.1.0 curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
```

**Uninstall** (stops the service, removes the binary, config and service files):

```bash
curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo UNINSTALL=1 bash
```

</details>

During install, say which mountpoints to track for disk usage (optional), and pick your LAN interface if the installer offers a choice of several, so mDNS advertises on the right network.

<p align="center">
  <img src="images/agent_install_uninstall_screenshot.png" alt="Agent install on Linux" width="520">
</p>

### 2. Add the integration to Home Assistant

Requires Home Assistant 2024.4 or newer.

[![Open your Home Assistant instance and open the SMART Sniffer repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DAB-LABS&repository=smart-sniffer&category=integration)

Click the button above, then **Download**, then restart Home Assistant. Or open HACS, search for **SMART Sniffer**, then **Download** and restart.

**Manual:** copy `custom_components/smart_sniffer/` into your HA `custom_components/` directory and restart.

### 3. Connect to the agent

**Auto-discovery:** after a few seconds Home Assistant shows a discovery notification. Click **Add**, enter the bearer token if the agent has one, and you are done. From v0.9 you can also add an agent from the **+** card in the Control Center.

**Manual:** **Settings > Devices & Services > Add Integration > SMART Sniffer**, then enter the agent's host, port, optional token, and polling interval.

Every drive on the machine appears as its own HA device.

## The Control Center

<p align="center">
  <img src="images/control-center-v09.png" alt="The SMART Sniffer Control Center in the Home Assistant sidebar: one card per drive, filesystem and machine" width="800">
</p>

<p align="center"><em>Sidebar > SMART Sniffer</em> (admins only)</p>

One grid of cards: one per drive, one per ZFS pool, one for each filesystem that belongs to no drive or pool. A card shows where the drive is, its temperature, power-on hours, data written, disk space, and its health word (**Attention**, **Maybe**, **Health OK** or **Health not known**) with the reason underneath. The chevron opens **Details** with every counter and attribute.

- **Arrange** cards by dragging, **hide** one with the eye, **remove** one with the trash.
- **Rename** a card by clicking its title; a drive or pool takes the new name in Home Assistant too.
- The sliders icon in Details edits a drive's alert thresholds, same as **Configure > Alert thresholds**.
- The page is shared by every admin: names, hidden cards and order are kept by the integration, not the browser.

### Adding machines

The last card, **+**, adds a machine three ways: **Network** lists agents already found on your LAN and can scan an IP block for more (never asking for a password just to scan); **Add by hand** checks one host and port directly; **Replica** adds test drives.

### Replicas

Replicas are test drives and pools with readings you choose, behaving exactly like real ones for sensors, notifications and automations, so you can see how Home Assistant reacts to a failing drive without having one. **+ > Replica** lists the available files (a set that ships with SMART Sniffer, plus your own saved ones) and adds whichever you tick. The save icon on any real drive or pool card saves a replica of it, with identifying details replaced. Full format: [Mock Agent](docs/mock-agent.md).

### Agentic access

**Agentic access**, in the page footer, opens everything the Control Center knows (every machine, drive, pool and filesystem, with a field guide at the end) as one JSON document, ready to copy, download, or hand to an AI assistant. Scripts can fetch the same document with a long-lived access token:

```bash
curl -H "Authorization: Bearer YOUR_LONG_LIVED_TOKEN" \
  "http://homeassistant.local:8123/api/smart_sniffer/agentic.json"
```

The document includes serial numbers, host names and addresses, so share it only with people and tools you trust. It never includes agent passwords.

## NAS & RAID setup

Most Linux, macOS and Windows machines work out of the box. NAS devices and RAID controllers sometimes need an extra step, since their storage controllers present drives differently than plain SATA or NVMe. See the [platform guides](docs/guides/) for Synology, QNAP, TrueNAS SCALE, Unraid, Proxmox, Docker, virtual machines and hardware RAID.

If the agent starts but reports no drives, or fewer than you expect, run `smartha-agent --discover`. It probes every drive the OS exposes, tests protocol detection, and offers to write the config for you. Paste its output into a GitHub issue if you need help. Full details: [Drive Discovery guide](docs/discover.md).

### Raw SMART data

For the full SMART dump of every drive on an agent: **Settings > Devices & Services > SMART Sniffer > three-dot menu > Download Diagnostics**. Useful for debugging, bug reports, or pasting into an AI chat. The agent also serves this directly at `http://<agent-ip>:9099/api/drives/{id}`.

<br>

## AI & automation

The `smart_sniffer.get_drive_data` service call returns the full cached SMART dataset for one agent: raw attributes, the Attention evaluation with its reasons, filesystem usage and agent metadata, everything an LLM needs for a real assessment.

One example: run it weekly, call `smart_sniffer.get_drive_data` for each agent, pass the result to a conversation agent (Google AI, OpenAI, Claude, Ollama) with a prompt like "analyze these SMART readings and flag anything concerning," and email yourself the answer. An AI can catch patterns, like a rising reallocated-sector count, that the raw numbers alone do not make obvious.

For every machine at once, use [Agentic access](#agentic-access) in the Control Center. For one-off analysis, use **Download Diagnostics** (see [Raw SMART data](#raw-smart-data) above) and paste the file into any AI chat.

<br>

## Screenshots

<table>
  <tr>
    <td align="center"><strong>NVMe SSD -- Sensors</strong></td>
    <td align="center"><strong>NVMe SSD -- Diagnostics</strong></td>
  </tr>
  <tr>
    <td><img src="images/nvme-sensors.png" width="300"></td>
    <td><img src="images/nvme-diagnostics.png" width="300"></td>
  </tr>
  <tr>
    <td align="center"><strong>Attention: YES (Critical)</strong></td>
    <td align="center"><strong>Trigger reason in Diagnostics</strong></td>
  </tr>
  <tr>
    <td><img src="images/attention-yes-sensors.png" width="300"></td>
    <td><img src="images/attention-yes-diagnostics.png" width="300"></td>
  </tr>
</table>

<details>
<summary><strong>What about USB drives?</strong></summary>

<br>

External drives behind a USB enclosure typically block SMART passthrough entirely. SMART Sniffer marks these `UNSUPPORTED` with Health: Unknown; this is the most common "why isn't my drive showing data" case, and it is a limitation of the USB bridge chip, not a bug.

<img src="images/unsupported-screenshot.png" width="280">

If a USB drive gave SMART data before and stops, Attention Needed shows unknown with the reason "SMART data not available right now" rather than `UNSUPPORTED`, since the drive has proven it can report. On macOS, USB drives only report SMART through a third-party SAT driver; reinstalling and re-authorizing it has restored data when this happens.

</details>

<br>

## Documentation

| Doc | Description |
|-----|-------------|
| [Attention Severity Logic](docs/attention-severity-logic.md) | State machine, classification rules, notification lifecycle |
| [Trigger to Entity Map](docs/attention-trigger-entity-map.md) | Every attention trigger mapped to its sensor entity and icon |
| [Early Warning Attributes](docs/early-warning-attributes.md) | Which SMART attributes predict failure and why |
| [Attribute Name Variants](docs/smart-attribute-name-variants.md) | Manufacturer-specific `smartctl` name mapping research |
| [Drive Discovery](docs/discover.md) | Probe drives, detect protocols, auto-generate config |
| [Mock Agent](docs/mock-agent.md) | Testing tool: a fake agent with controllable drives |
| [Platform Install Paths](docs/platform-install-paths.md) | Install locations, immutable rootfs support, network interface filtering |
| [Examples](examples/) | Community-contributed automations: copy, paste, adapt |

### Platform guides

| Platform | What's covered |
|----------|---------------|
| [Proxmox + HA](docs/guides/proxmox.md) | Agent on host, integration in VM, mDNS, firewall |
| [Synology DSM](docs/guides/synology.md) | SynoCli smartmontools, `/dev/sataX` paths, `--discover` |
| [QNAP QTS](docs/guides/qnap.md) | SAT fallback, lxcbr0 exclusion, interface selection |
| [TrueNAS SCALE](docs/guides/truenas-scale.md) | ZFS pool health and usage, install path that survives upgrades |
| [Unraid](docs/guides/unraid.md) | br0 bridge, Docker deployment |
| [Docker](docs/guides/docker.md) | Build your own container, plus a ready prompt for AI coding assistants |
| [Virtual Machines](docs/guides/virtual-machines.md) | ESXi, Hyper-V, VirtualBox: why SMART needs the host |
| [Hardware RAID Controllers](docs/guides/raid-controllers.md) | MegaRAID, HP SmartArray, 3ware, Areca: manual `device_overrides` |

## Testing

`pip install -r requirements_test.txt && pytest tests/` for the integration (no Home Assistant needed), and `cd agent && go test ./...` for the agent. Both also run against the included [Mock Agent](docs/mock-agent.md), a standalone Python tool that simulates `smartha-agent` with fully controllable fake drives and a web dashboard for changing SMART attributes in real time:

```bash
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,nvme,usb_blocked
```

Point the HA integration at `localhost:9100` and you are testing.

## Contributing

Found a bug, or a drive that is not mapping correctly? See [CONTRIBUTING.md](CONTRIBUTING.md). Drive-specific `smartctl -a --json` output samples are especially welcome; they help catch manufacturer name variants nobody has seen yet.

## Roadmap

- [ ] MQTT agent mode
- [ ] Per-drive scan intervals
- [ ] YAML-based SMART attribute definitions (vendor field mapping, transforms, units)
- [ ] SAS/SCSI full attribute parsing (grown defect list, error counters, endurance indicators)
- [ ] Agent: runtime interface detection, replacing the static prefix list with OS-level physical NIC detection
- [ ] Optional area-on-setup: choose an area once and apply it to all of an agent's drives

Shipped work lives in [CHANGELOG.md](CHANGELOG.md).

---

<p align="center">
  MIT License · Built by <a href="https://github.com/DAB-LABS">DAB-LABS</a>
</p>
