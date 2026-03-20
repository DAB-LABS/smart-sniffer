# Changelog

All notable changes to SMART Sniffer are documented here.

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
