# Changelog

All notable changes to SMART Sniffer are documented here.

## v0.4.29 — 2026-03-25

### Fixed
- **Wear Leveling / Percentage Used showing raw write count instead of percentage** — ATA wear-related attributes (`Media_Wearout_Indicator`, `SSD_Life_Left`, `Percent_Lifetime_Remain`, etc.) were reading the `RAW_VALUE` column from smartctl, which contains a vendor-specific counter (e.g., 1569 total writes). Now reads the normalized `VALUE` column (0–100), which is the actual percentage remaining. Fixes drives incorrectly showing values like "1,568%" instead of "100%". ([#7](https://github.com/DAB-LABS/smart-sniffer/issues/7))

### Changed
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
