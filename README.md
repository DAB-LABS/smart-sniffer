# SMART Sniffer

Monitor disk SMART health data in Home Assistant — from local or remote machines.

SMART Sniffer is a two-part system: a lightweight **Go agent** that reads SMART data via `smartctl` and exposes it over a REST API, and a **Home Assistant custom integration** that polls the agent and surfaces drive health as HA devices and entities.

## Architecture

```
┌─────────────────────┐         HTTP (JSON)        ┌──────────────────────────┐
│   Machine A         │ ◄──────────────────────── │   Home Assistant          │
│   smartha-agent     │    GET /api/drives          │   smart_sniffer          │
│   (smartctl wrapper) │    GET /api/drives/{id}    │   integration            │
│   :9099             │    GET /api/health          │                          │
└─────────────────────┘                             │   ┌─ Device: Samsung 870 │
                                                    │   │  ├─ Temperature       │
┌─────────────────────┐         HTTP (JSON)        │   │  ├─ Power-On Hours    │
│   Machine B         │ ◄──────────────────────── │   │  ├─ Reallocated Sectors│
│   smartha-agent     │                             │   │  └─ Health (binary)   │
│   :9099             │                             │   │                       │
└─────────────────────┘                             │   ├─ Device: WD Black    │
                                                    │   │  └─ ...              │
                                                    └───┴──────────────────────┘
```

Each machine you want to monitor runs its own `smartha-agent`. The HA integration connects to one or more agents (added via the UI config flow) and creates devices + entities for every drive.

## Agent Setup

### Prerequisites

- **smartmontools** must be installed on the target machine:
  - Debian/Ubuntu: `sudo apt install smartmontools`
  - RHEL/Fedora: `sudo dnf install smartmontools`
  - macOS: `brew install smartmontools`
  - Windows: `choco install smartmontools`
- The agent needs **elevated privileges** (root/admin) since `smartctl` requires them to read drive data.

### Install from Binary

1. Download the appropriate binary from the [Releases](https://github.com/DAB-LABS/smart-sniffer/releases) page.
2. Copy it to a suitable location: `sudo cp smartha-agent /usr/local/bin/`
3. Copy and edit the example config:
   ```bash
   sudo mkdir -p /etc/smartha-agent
   sudo cp config.yaml.example /etc/smartha-agent/config.yaml
   ```
4. Run it: `sudo smartha-agent`

### Configuration

Create a `config.yaml` in the working directory or `/etc/smartha-agent/`:

```yaml
port: 9099
token: "your-secret-token"    # optional — omit to disable auth
scan_interval: 60s
```

All options can also be set via CLI flags: `--port`, `--token`, `--scan-interval`.

### Running as a Service

#### systemd (Linux)

```bash
sudo cp agent/systemd/smartha-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smartha-agent
```

#### launchd (macOS)

```bash
# Install the binary
make darwin-arm64   # or darwin-amd64 for Intel Macs
sudo cp build/smartha-agent-darwin-arm64 /usr/local/bin/smartha-agent

# Create config directory
sudo mkdir -p /etc/smartha-agent
sudo cp config.yaml.example /etc/smartha-agent/config.yaml

# Install and load the service
sudo cp agent/launchd/com.dablabs.smartha-agent.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.dablabs.smartha-agent.plist
sudo chmod 644 /Library/LaunchDaemons/com.dablabs.smartha-agent.plist
sudo launchctl load -w /Library/LaunchDaemons/com.dablabs.smartha-agent.plist
```

Useful launchd commands:

```bash
# Stop / start
sudo launchctl unload /Library/LaunchDaemons/com.dablabs.smartha-agent.plist
sudo launchctl load -w /Library/LaunchDaemons/com.dablabs.smartha-agent.plist

# Restart
sudo launchctl kickstart -k system/com.dablabs.smartha-agent

# Logs
tail -f /var/log/smartha-agent.log
```

#### Windows Service

Run from an elevated PowerShell prompt:

```powershell
# Build the binary first
make windows-amd64

# Install as a Windows service (starts automatically at boot)
.\agent\windows\install-service.ps1

# Optional: specify port and auth token
.\agent\windows\install-service.ps1 -Port 9099 -Token "mysecrettoken"

# Uninstall
.\agent\windows\uninstall-service.ps1 -RemoveFiles
```

The script copies the binary to `C:\Program Files\smartha-agent\`, writes a `config.yaml`, registers the service with auto-restart on failure, and starts it immediately. Logs go to the Windows Event Log (`Get-EventLog -LogName Application -Source SmarthaAgent`).

### Building from Source

Requires Go 1.22+.

```bash
cd agent
make            # build for current platform
make all        # cross-compile for all targets
```

Binaries are output to `agent/build/`.

## Integration Setup

### HACS Installation

1. Add this repository as a [custom repository in HACS](https://hacs.xyz/docs/faq/custom_repositories):
   - URL: `https://github.com/DAB-LABS/smart-sniffer`
   - Category: **Integration**
2. Search for "SMART Sniffer" in HACS and install it.
3. Restart Home Assistant.

### Manual Installation

Copy `integration/custom_components/smart_sniffer/` to your HA `custom_components/` directory and restart.

### Adding a Host

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for "SMART Sniffer".
3. Enter the agent's host, port, optional token, and polling interval.
4. Each drive detected by the agent will appear as a separate device with sensor entities.

### Entities

For each drive, the integration creates:

| Entity | Type | Description |
|--------|------|-------------|
| Temperature | `sensor` | Current drive temperature (°C) |
| Power-On Hours | `sensor` | Total hours the drive has been powered on |
| Reallocated Sector Count | `sensor` | Number of remapped sectors (diagnostic) |
| Current Pending Sector Count | `sensor` | Sectors waiting to be remapped |
| Reported Uncorrectable Errors | `sensor` | Uncorrectable read/write errors |
| Wear Leveling / % Used | `sensor` | SSD wear indicator |
| SMART Status | `sensor` | Overall PASSED/FAILED |
| Health | `binary_sensor` | Problem indicator — ON when unhealthy |

## Supported Platforms

| Platform | Agent | Notes |
|----------|-------|-------|
| Linux (amd64, arm64) | ✅ | Primary target |
| macOS (amd64, arm64) | ✅ | Homebrew for smartmontools |
| Windows (amd64) | ✅ | Needs admin privileges |

## Roadmap / TODO

- **Auto-discovery (mDNS/Zeroconf):** Agents advertise themselves on the network so HA can find them automatically.
- **MQTT agent mode:** Publish SMART data to an MQTT broker instead of (or in addition to) REST, for environments where direct HTTP isn't ideal.
- **Custom Lovelace card:** A purpose-built card showing drive health at a glance with gauges and status indicators.
- **Configurable thresholds:** Per-drive threshold configuration via the HA integration options flow (e.g., "warn me when reallocated sectors > 5").
- **SAS/SCSI support:** Full parsing of SAS/SCSI-specific SMART data structures.
- **Notifications:** Built-in automation blueprints for alerting on drive health changes.

## License

MIT — see [LICENSE](LICENSE).
