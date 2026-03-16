<p align="center">
  <img src="images/SMARTsniffer.png" alt="SMART Sniffer" width="100%">
</p>

**Proactive disk health monitoring for Home Assistant.**<br>
*Sniffing SMART circuits so your drives don't ghost you.* 👻☠️

<p align="center">
  <a href="https://github.com/DAB-LABS/smart-sniffer/releases/latest"><img src="https://img.shields.io/github/v/release/DAB-LABS/smart-sniffer?style=flat-square" alt="Release"></a>
  <a href="https://github.com/DAB-LABS/smart-sniffer/actions/workflows/release.yml"><img src="https://img.shields.io/github/actions/workflow/status/DAB-LABS/smart-sniffer/release.yml?style=flat-square&label=build" alt="Build"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/DAB-LABS/smart-sniffer?style=flat-square" alt="License"></a>
</p>

---

Reallocated sectors. Uncorrectable errors. Pending failures quietly piling up. 🧙‍♀️🙀

SMART drives say "PASSED" right up until the end. ☠️🪦

Learn to smell the decay. 🧟‍♂️

SMART Sniffer follows the trail, sniffing out the [early warning signs](https://www.backblaze.com/b2/hard-drive-test-data.html) your drives won't tell you about and reporting them to Home Assistant before the drive ghosts you for good. One agent per machine. Every drive gets a health score. Alerts fire automatically. No automations required. 👻

<br>

## Features

**Early warning alerts** — The Attention Needed sensor monitors leading indicators of failure across ATA, SATA, and NVMe drives. Four clear states: `NO` · `MAYBE` · `YES` · `UNSUPPORTED`.

**Zero-config notifications** — Persistent notifications fire automatically when drive health changes. No automations or blueprints to set up. Alerts escalate, de-escalate, and auto-dismiss.

**Multi-machine monitoring** — Install a lightweight Go agent on each machine. Each drive appears as its own HA device with full sensor entities and diagnostics.

**Secure by default** — Optional bearer token authentication between agent and integration. SHA256-verified binary downloads.

<br>

## Quick Start

**Requires:** `smartmontools` on each monitored machine — the installer handles this automatically (Homebrew on macOS, apt/dnf/yum on Linux).

### 1. Install the agent

Run on each machine you want to monitor:

```bash
curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
```

<details>
<summary>Windows (PowerShell as Admin)</summary>

```powershell
irm https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.ps1 | iex
```

> **Note:** The Windows agent builds but has not been validated yet. [Let us know how it goes.](https://github.com/DAB-LABS/smart-sniffer/issues)

</details>

<details>
<summary>Pin a specific version</summary>

```bash
VERSION=0.1.0 curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
```

</details>

The installer detects your OS and architecture, downloads the correct binary from [GitHub Releases](https://github.com/DAB-LABS/smart-sniffer/releases), verifies the SHA256 checksum, installs `smartmontools` if missing, prompts for configuration, and sets up a system service.

### 2. Add the integration to Home Assistant

**Via HACS (recommended):**

1. Open HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/DAB-LABS/smart-sniffer` · Category: **Integration**
3. Download **SMART Sniffer** → Restart HA

**Manual:** Copy `custom_components/smart_sniffer/` into your HA `custom_components/` directory and restart.

### 3. Connect to the agent

**Settings → Devices & Services → Add Integration → SMART Sniffer**

Enter the agent's host, port, optional token, and polling interval. Every drive appears as its own device.

<br>

## Screenshots

<table>
  <tr>
    <td align="center"><strong>SATA SSD — Sensors</strong></td>
    <td align="center"><strong>Apple NVMe — Sensors</strong></td>
  </tr>
  <tr>
    <td><img src="images/sensors-samsung-screenshot.png" width="320"></td>
    <td><img src="images/sensors-apple-screenshot.png" width="320"></td>
  </tr>
  <tr>
    <td align="center"><strong>SATA SSD — Diagnostics</strong></td>
    <td align="center"><strong>Apple NVMe — Diagnostics</strong></td>
  </tr>
  <tr>
    <td><img src="images/diag-samsung-screenshot.png" width="320"></td>
    <td><img src="images/diag-apple-screenshot.png" width="320"></td>
  </tr>
</table>

<details>
<summary><strong>🔌 What about USB drives?</strong></summary>

<br>

External drives connected via USB enclosures typically block SMART passthrough. SMART Sniffer detects this and marks the drive as `UNSUPPORTED` with `Health: Unknown`.

<img src="images/unsupported-screenshot.png" width="280">

This is the most common "why isn't my drive showing data?" scenario. It's a hardware limitation of the USB bridge chip, not a bug.

</details>

<br>

## How It Works

<p align="center">
  <img src="images/architecture.svg" alt="Architecture diagram" width="100%">
</p>

Each machine runs a lightweight `smartha-agent` binary that wraps `smartctl` and serves SMART data over HTTP. The HA integration polls each agent and creates devices, sensors, and health alerts for every drive it finds.

<br>

<details>
<summary><strong>Entities per drive</strong></summary>

<br>

Each drive gets its own HA device. Entities are created dynamically — if a drive doesn't report an attribute, the sensor is simply not created.

**Sensors:**

| Entity | Description |
|--------|-------------|
| Attention Needed | Proactive health alert — `NO` / `MAYBE` / `YES` / `UNSUPPORTED` |
| Health | SMART pass/fail — OK, Problem, or Unknown |
| Temperature | Current drive temp (°C) |
| Power-On Hours | Total hours powered on |
| SMART Status | Raw SMART verdict (PASSED / FAILED) |

**Diagnostic sensors** (conditional):

| Entity | Description |
|--------|-------------|
| Reallocated Sector Count | Bad sectors remapped to spares (ATA) |
| Reported Uncorrectable Errors | Unrecoverable read/write errors (ATA) |
| Wear Leveling / Percentage Used | SSD endurance indicator |
| Power Cycle Count | Total power on/off cycles |
| Reallocated Event Count | Individual reallocation events (ATA) |
| Spin Retry Count | Motor spin-up retries — HDD only |
| Command Timeout | Internal command timeouts |
| Available Spare | NVMe reserve block pool (%) |
| Available Spare Threshold | Manufacturer-set minimum spare (%) |
| Current Pending Sector Count | Sectors waiting for reallocation (ATA) |

</details>

<details>
<summary><strong>Attention Needed — how it classifies drives</strong></summary>

<br>

The Attention Needed sensor evaluates individual SMART attributes every poll cycle:

| State | Meaning | Action |
|-------|---------|--------|
| **NO** | All monitored indicators clear | None required |
| **MAYBE** | Early degradation signals detected | Monitor closely, plan replacement |
| **YES** | Data integrity at risk | **Back up immediately** |
| **UNSUPPORTED** | No usable SMART data | Common with USB enclosures |

**Critical triggers (→ YES):** Reallocated sectors, pending sectors, uncorrectable errors, NVMe critical_warning, NVMe media_errors, spare depletion below threshold.

**Warning triggers (→ MAYBE):** Reallocated events, spin retry count, command timeouts, NVMe spare < 20%, NVMe percentage_used ≥ 90%.

When a drive's state changes, a persistent notification fires in HA automatically. Notifications escalate on worsening conditions and dismiss when resolved. See [attention-severity-logic.md](docs/attention-severity-logic.md) for the full specification.

</details>

<details>
<summary><strong>Agent configuration</strong></summary>

<br>

Create `config.yaml` in the working directory or `/etc/smartha-agent/`:

```yaml
port: 9099
token: "your-secret-token"    # optional — omit to disable auth
scan_interval: 60s
```

All options can also be set via CLI flags: `--port`, `--token`, `--scan-interval`.

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

Binaries output to `agent/build/`.

| Platform | Architecture | Binary | Status |
|----------|-------------|--------|--------|
| Linux | amd64, arm64 | `smartha-agent-linux-amd64`, `-arm64` | Tested |
| macOS | amd64 (Intel), arm64 (Apple Silicon) | `smartha-agent-darwin-amd64`, `-arm64` | Tested |
| Windows | amd64 | `smartha-agent-windows-amd64.exe` | Not yet tested |

</details>

<br>

## Documentation

| Doc | Description |
|-----|-------------|
| [Attention Severity Logic](docs/attention-severity-logic.md) | State machine, classification rules, notification lifecycle |
| [Early Warning Attributes](docs/early-warning-attributes.md) | Which SMART attributes predict failure and why |
| [Attribute Name Variants](docs/smart-attribute-name-variants.md) | Manufacturer-specific `smartctl` name mapping research |
| [Build Journal](docs/build-journal.md) | Design decisions, iteration history, known issues |

## Roadmap

- [ ] HAOS add-on — run the agent directly on Home Assistant OS
- [ ] Integration icons for HA integrations page
- [ ] Auto-discovery via mDNS/Zeroconf
- [ ] MQTT agent mode
- [ ] Custom Lovelace card
- [ ] Configurable alert thresholds via options flow
- [ ] SAS/SCSI drive support

## Contributing

Found a bug? Have a drive that isn't mapping correctly? [Open an issue.](https://github.com/DAB-LABS/smart-sniffer/issues) Drive-specific `smartctl -a --json` output samples are especially welcome — they help us catch manufacturer name variants we haven't seen yet.

---

<p align="center">
  MIT License · Built by <a href="https://github.com/DAB-LABS">DAB-LABS</a>
</p>
