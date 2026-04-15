# Changelog

All notable changes to SMART Sniffer are documented here.

## v0.5.1 — 2026-04-15

### Fixed
- **Windows: agent service fails to start with Error 1053 ([#13](https://github.com/DAB-LABS/smart-sniffer/issues/13))** — the Go binary was being registered as a Windows service but did not implement the Service Control Manager handshake, so SCM killed it after 30 seconds with the generic "service did not respond to the start or control request in a timely fashion" error. The agent now detects when it has been launched by the SCM, reports StartPending immediately, and reports Running the moment its HTTP listener binds. On boxes with many disks where the smartctl preflight scan takes longer than SCM's start window, a 20-second watchdog reports Running anyway so the scan can complete in the background without a 1053 timeout.
- **Linux: agent fails to start on older distros with "GLIBC_2.32 not found" ([#14](https://github.com/DAB-LABS/smart-sniffer/issues/14))** — Linux release binaries were dynamically linked against whichever glibc the GitHub Actions runner had installed (typically 2.34 or 2.32), making them incompatible with Debian 9, RHEL 7, and other long-term-support distros shipping older glibc. Linux builds are now statically linked via `CGO_ENABLED=0`, removing the glibc version dependency entirely. Also makes the binary work unmodified on musl-based distros like Alpine.

### Added
- **Windows: service startup and shutdown events in Event Log** — the agent now writes to the Windows Event Log under source `SmartHA-Agent` (installer registers the source automatically). Service start, stop, and failure events appear in Event Viewer → Windows Logs → Application with dedicated event IDs (1 started, 2 stopped, 100 startup failure, 101 runtime failure, 102 shutdown error). Operators debugging service issues no longer have to guess at causes from the Services panel alone.
- **Windows installer: config preservation on upgrade** — re-running `install.ps1` now detects an existing `config.yaml`, displays current settings, and asks to keep them (default yes). Matches the Unix installer behavior that had been shipped since v0.4.28 but missed on Windows. Upgraders no longer silently lose their bearer token, custom port, or scan interval when reinstalling for a version bump.
- **Windows installer: diagnostics on start failure** — if `Start-Service` fails during install, the installer now dumps the current service status and the last 10 Event Log entries from our source in-line before exiting, so the user has the context to diagnose without opening Event Viewer.

### Changed
- **Agent: bounded graceful shutdown budget** — shutdown phases (mDNS deregister, HTTP drain, coordinator close) now run under a single 8-second budget instead of an unconditional 5-second sleep. If a phase hangs, the installer logs which one stalled and how far along each phase got. Tuned to fire before Home Assistant Supervisor's 10-second stop timeout and well within Windows SCM's 20-second service-stop window, so the agent chooses what to drop rather than the OS killing it mid-cleanup.
- **Build: Makefile VERSION derived from git** — `make` now stamps binaries with `git describe --tags --always --dirty` by default so self-builders get an accurate version string in their binary. CI continues to set an explicit VERSION on the command line for releases.

### Upgrade Notes
- **Windows users on Error 1053:** this release is the fix. Re-run the installer; it will detect your existing config and preserve it, replace the binary, and re-register the service with the new SCM handler. No manual uninstall needed.
- **Debian 9 / RHEL 7 / Alpine users on "GLIBC not found":** this release is the fix. Re-run the installer — the new binary is statically linked and no longer depends on the host's glibc version.
- **Home Assistant integration:** no changes required. This is an agent-only release.
- **macOS and modern Linux distros:** nothing changes for you behaviorally; the agent gets the build-time and shutdown-budget improvements but nothing user-visible.

## v0.5.0 — 2026-03-31

### Added
- **Disk Usage monitoring** — agents running v0.5.0+ with filesystem monitoring configured now report disk usage data. The integration creates a **Disk Usage** device per host with a percentage sensor for each monitored mountpoint (e.g., "Disk Usage — Root (/)"). Attributes include total, used, and available space in GB, plus mountpoint, device, and filesystem type. Use automations to alert when a disk fills up (e.g., trigger at 90%).
- **Agent: `/api/filesystems` endpoint** — serves real-time disk usage for mountpoints selected during install. Refreshes on the same interval as SMART data.
- **Agent: filesystem picker in installer** — the install script now asks which mountpoints to monitor for disk usage. Writes selections to `config.yaml`. Skipping this step disables disk usage monitoring (the endpoint is not registered).

### Upgrade Notes
- **Integration**: update via HACS as usual. Fully backward compatible — older agents (pre-0.5.0) continue to work with no changes and no new entities.
- **Agent**: to enable disk usage monitoring, update your agents to v0.5.0 by re-running the installer. The installer will ask which disks to monitor. Existing agents that don't upgrade will continue to work — they just won't show disk usage.
- **New entities appear after reload**: after upgrading an agent to v0.5.0, go to the SMART Sniffer integration page, click the three-dot menu on the agent, and select **Reload**. The new Disk Usage device and sensors will appear.

## v0.4.31 — 2026-03-27

### Fixed
- **Power-On Hours showing astronomically wrong values on some SATA drives** — certain vendors (e.g., Seagate, HGST) pack additional counters (days, minutes) into the upper bytes of the 48-bit raw value for SMART attribute 9 (Power_On_Hours). The integration was displaying the full compound value (e.g., 165 trillion hours) instead of the actual hours stored in the lower 32 bits. Now parses the human-readable string first, falls back to masking. Same class of bug as the Command Timeout fix in v0.4.26 and the Wear Leveling fix in v0.4.30. ([#10](https://github.com/DAB-LABS/smart-sniffer/issues/10))

## v0.4.30 — 2026-03-26

### Fixed
- **Wear Leveling / Percentage Used showing raw write count instead of percentage** — ATA wear-related attributes (`Media_Wearout_Indicator`, `SSD_Life_Left`, `Percent_Lifetime_Remain`, etc.) were reading the `RAW_VALUE` column from smartctl, which contains a vendor-specific counter (e.g., 1569 total writes). Now reads the normalized `VALUE` column (0–100), which is the actual percentage remaining. Fixes drives incorrectly showing values like "1,568%" instead of "100%". ([#7](https://github.com/DAB-LABS/smart-sniffer/issues/7))

### Changed
- **Release workflow: auto-extract changelog** — GitHub Release body now pulls the current version's notes from CHANGELOG.md automatically, so HA update notifications show descriptive release info instead of a generic placeholder
- **README: scan interval documentation** — agent configuration section now documents Go duration syntax (`30s`, `5m`, `1h`, `24h`) and notes that each poll wakes spun-down drives
- **README: roadmap additions** — per-drive scan intervals, standby-aware polling, and YAML-based attribute definitions added to roadmap

## v0.4.28 — 2026-03-24

### Added
- **Agent: version in `/api/health`** — the health endpoint now returns `{"status":"ok","version":"0.4.28"}`, enabling the integration to detect outdated agents without relying solely on mDNS TXT records
- **Integration: agent version check + HA repair notifications** — the coordinator checks the agent version every poll cycle. If the agent is older than `MIN_AGENT_VERSION`, a repair card appears in Settings → Repairs with the agent hostname, current vs required version, and a one-liner upgrade command. The repair auto-clears once the agent is updated — no restart or user action needed inside HA
- **Integration: version warning at discovery** — when adding a new agent via zeroconf, the config flow shows a warning if the discovered agent is outdated, without blocking setup
- **Installer: config preservation on upgrade** — re-running the installer now detects an existing `config.yaml`, displays current settings (with masked token), and asks to keep them. Default is yes — press Enter to upgrade in place with no re-entry of port, token, or interval
- **Installer: interface picker on upgrade** — configs from pre-v0.4.25 without `advertise_interface` get a one-time interface prompt on multi-homed hosts, preventing mDNS from advertising on VPN/Docker interfaces
- **Installer: `utun` virtual interface detection** — macOS VPN tunnels (ZeroTier, Tailscale) using `utunX` interfaces are now correctly identified as virtual and excluded from the default mDNS interface list
- **Agent: `--mdns-name` flag / `mdns_name` config** — allows overriding the mDNS instance name (default: `smartha-<hostname>`). Fixes mDNS collisions when multiple HA instances run the SMART Sniffer add-on on the same network — container hostnames are typically identical, causing only one agent to be discoverable. The HA add-on passes `--mdns-name=smartha-<ha-hostname>` derived from the Supervisor API to ensure unique names per instance

## v0.4.27 — 2026-03-23

### Added
- **Agent: armv7 (Raspberry Pi) binary** — release workflow and Makefile now build `smartha-agent-linux-arm` (ARMv7 hard-float, GOARM=7) alongside existing platforms. Enables native Raspberry Pi 2/3/4 installs and unblocks the HA add-on armv7 architecture build.

### Changed
- **`.gitignore`: local docs folder** — `docs/internal/` excluded from version control for project-private documentation

## v0.4.26 — 2026-03-23

### Fixed
- **Seagate/OEM Command Timeout false alerts** — Seagate and OEM drives (e.g., OOS-series) pack three 16-bit counters into the 48-bit raw value for SMART attribute 188. The integration was comparing the full compound value against zero, triggering false MAYBE alerts on every affected drive. Now decodes to the lower 16 bits when raw value exceeds 0xFFFF. Applies to all vendors — detection is value-based, not vendor-based.
- **Command Timeout sensor display** — sensor entity was showing the raw compound value (e.g., 940 billion) instead of the decoded timeout count

### Changed
- **README: interface picker documentation** — install section, agent configuration, and auto-discovery paragraphs now document the mDNS interface picker, `advertise_interface` config, and multi-homed host guidance
- **README: updated install screenshot** — now shows the interface picker flow
- **README: documentation table** — added links to Platform Install Paths and Agent Version Repair docs

### Added
- **docs/agent-version-repair.md** — design doc for HA repair notifications when agent version is too old

## v0.4.25 — 2026-03-20

### Added
- **Agent: mDNS interface filtering** — auto-skips Docker, ZeroTier, Tailscale, WireGuard, and other virtual interfaces by default; only advertises on real LAN interfaces
- **Agent: `advertise_interface` config option** — restrict mDNS to a specific interface (e.g., `advertise_interface: eth0`)
- **Agent: `ip=` mDNS TXT record** — agent reports its preferred LAN IP so the HA integration doesn't have to guess
- **Agent: `--config` flag** — specify a custom config file path (`smartha-agent --config /path/to/config.yaml`)
- **Agent: `--interface` flag** — CLI override for mDNS interface (`smartha-agent --interface eth0`)
- **Installer: interface picker** — during install, presents detected interfaces with labels (Docker, ZeroTier, etc.) and lets the user choose which to advertise on
- **Integration: reads agent `ip=` TXT field** — trusts the agent's preferred IP over local scoring when available; falls back gracefully for older agents

### Fixed
- Duplicate mDNS discoveries from Docker bridges, VPNs, and mDNS reflectors surfacing the same agent at multiple IPs
- IPv6 addresses deprioritized in IP scoring (unreliable across VLANs in home networks)

## v0.4.24 — 2026-03-20

### Fixed
- Zeroconf discovery now correctly selects real LAN IPs (10.x, 192.168.x) over Docker bridge IPs (172.17.x) on container-based systems like ZimaOS/CasaOS
- Switched config entry unique IDs from IP-based to hostname-based to prevent duplicate discoveries when mDNS reflectors surface the same agent on multiple IPs/VLANs; existing entries are migrated automatically

### Added
- Installer now probes for writable paths on immutable-rootfs platforms (ZimaOS, CasaOS); falls back to `/DATA/smartha-agent/` or `/opt/smartha-agent/`
- New doc: `docs/platform-install-paths.md` — explains platform-specific install locations and how to add new ones

## v0.4.23 — 2026-03-19

### Fixed
- Re-cropped brand icons with tighter framing on magnifying glass + spies (eliminates letterboxing in HA UI)

## v0.4.22 — 2026-03-19

### Changed
- Zeroconf auto-discovery now requires user confirmation before adding an agent (previously no-auth agents were added silently)

### Fixed
- Updated `early-warning-attributes.md` — corrected stale `binary_sensor` references to enum sensor with proper state examples

### Improved
- New brand icons cropped from header art (spy + magnifying glass) for HA integrations page
- Documented v0.4.21 fixes in build journal
- Removed `brands-repo-pr/` directory (HA brands repo no longer accepts custom integration PRs)
- Removed legacy platform-specific install scripts (superseded by unified `install.sh` / `install.ps1`)
- Cleaned committed build artifacts and `__pycache__` from repo
- Added GitHub issue templates (bug report, feature request)

## v0.4.21 — 2026-03-19

### Fixed
- WD/HGST drives reporting ~214 billion °C temperature — packed 48-bit raw value now parsed correctly via `raw.string` with bitmask fallback
- Zeroconf discovery picking Tailscale VPN IP (100.x) over LAN IP — new `_pick_best_ip()` prefers RFC 1918 private addresses
- `asyncio.TimeoutError` not caught in config flow — connection timeouts now show "Unable to connect" instead of stack traces

## v0.4.20 — 2026-03-18

### Added
- Beta launch — new screenshots, CONTRIBUTING.md, SECURITY.md
- GitHub Sponsors and funding links

## v0.4.0 — 2026-03-17

### Added
- Mock agent for testing (`tools/mock-agent.py`) — simulates drives with controllable SMART attributes
- Attention Reasons diagnostic entity — shows exactly what triggered the attention state
- Dynamic icons for attention sensor (per-state MDI icons)
- Expanded NVMe sensor coverage

### Fixed
- SMART FAILED status not triggering `YES` attention state
- Health sensor correctly reports `Unknown` for USB drives with no SMART data

## v0.3.0 — 2026-03-16

### Added
- mDNS/Zeroconf auto-discovery — agents advertise `_smartha._tcp.local.`, HA discovers them automatically
- Zeroconf config flow with pre-filled host/port and conditional token prompt
- Skip blank confirmation form for no-auth discovered agents

### Fixed
- `ZeroconfServiceInfo` import path compatibility for HA 2025.x+
- mDNS instance name breaking with dotted hostnames on macOS
- `grandcat/zeroconf` pulling stale Go x/ dependencies

## v0.2.0 — 2026-03-15

### Added
- Uninstall support in `install.sh`
- Bearer token authentication for agent API

### Fixed
- Auth health check returning wrong status
- `install.sh` stdin handling for `curl | bash` piping
- Uninstall env var clobbering
- "Text file busy" error on reinstall
- Bare number scan interval not appending `s` suffix

## v0.1.0 — 2026-03-14

### Added
- Initial release
- Go agent (`smartha-agent`) wrapping `smartctl` with HTTP REST API
- Home Assistant custom integration with per-drive devices and sensors
- Early-warning attention system (Reallocated Sectors, Pending Sectors, Uncorrectable Errors)
- Persistent notifications on attention state changes
- Cross-platform installers (`install.sh` for Linux/macOS, `install.ps1` for Windows)
- GitHub Actions release workflow with cross-compilation and SHA256 checksums
