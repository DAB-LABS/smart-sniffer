# SMART Sniffer — Build Journal

**Started:** March 2026
**Authors:** David Bailey, with Claude (Anthropic)
**Repo:** `github.com/DAB-LABS/smart-sniffer`

This document captures the full story of SMART Sniffer from concept through v0.1.0 — the goals, the design decisions, the things that broke, and what's left. Written for future-us to look back on and remember *why* things are the way they are.

---

## The Problem

Home Assistant has no built-in way to monitor disk SMART health data across machines. If you're running HA on a Proxmox host with multiple drives, or you have NAS boxes and workstations on the network, you can't see drive health from the HA dashboard without stitching together shell commands, MQTT scripts, or third-party tools that each solve only a piece of the puzzle.

The bigger problem: SMART's official pass/fail status is a *lagging* indicator. Drives can report "PASSED" right up until catastrophic failure. Backblaze's data shows that individual attributes — reallocated sectors, pending sectors, uncorrectable errors — are far more predictive. We wanted a system that watches those attributes and tells you *before* SMART officially fails.

---

## What We Built

A two-part system:

**Part 1 — `smartha-agent` (Go):** A lightweight HTTP server that wraps `smartctl --json`, caches scan results, and serves them over REST. Runs on any machine (Linux, macOS, Windows) with `smartmontools` installed. One agent per machine.

**Part 2 — `smart_sniffer` integration (Python/HA):** A HACS-compatible Home Assistant custom integration that polls one or more agents and creates HA devices and entities per drive. Each agent host is added via the HA config flow.

The architecture diagram in the README covers the high-level picture. This document covers everything behind it.

---

## Repo Structure (v0.3.0)

```
smart-sniffer/
├── agent/                          # Go agent
│   ├── main.go                     # HTTP server, smartctl execution, caching, mDNS registration
│   ├── config.go                   # Config loading: config.yaml → CLI flags, mDNS toggle, interface filtering
│   ├── config.yaml.example         # Template config (port, token, scan_interval, mdns)
│   ├── Makefile                    # Cross-compilation + checksums
│   ├── go.mod / go.sum             # Go module
│   ├── systemd/                    # Linux service file
│   ├── launchd/                    # macOS plist
│   ├── windows/                    # Windows service scripts (legacy)
│   ├── install-linux.sh            # Legacy platform-specific installer
│   └── install-macos.sh            # Legacy platform-specific installer
│
├── custom_components/               # HA custom integration (HACS-compatible location)
│   └── smart_sniffer/
│       ├── __init__.py             # Platform setup + update listener
│       ├── config_flow.py          # Config flow + options flow + Zeroconf discovery
│       ├── coordinator.py          # DataUpdateCoordinator + notifications
│       ├── sensor.py               # All sensor entities + attention enum
│       ├── binary_sensor.py        # Health binary sensor
│       ├── attention.py            # Shared severity logic (imported by 3 modules)
│       ├── diagnostics.py          # HA diagnostics with redaction
│       ├── const.py                # Constants (DOMAIN, CONF_*)
│       ├── manifest.json           # HA integration metadata
│       ├── strings.json            # UI strings (config + options + zeroconf flows)
│       ├── translations/en.json
│       └── brand/                  # HACS-required integration icons
│           ├── icon.png / icon@2x.png
│           └── logo.png / logo@2x.png
│
├── .github/workflows/
│   └── release.yml                 # GitHub Actions: build + release on v* tag
│
├── install.sh                      # Unified Linux/macOS installer (curl|bash)
├── install.ps1                     # Windows PowerShell installer (irm|iex)
│
├── docs/
│   ├── build-journal.md            # ← You are here
│   ├── attention-severity-logic.md # Full attention state/notification docs
│   ├── early-warning-attributes.md # Which SMART attributes predict failure
│   └── smart-attribute-name-variants.md  # Manufacturer name mapping research
│
├── brands-repo-pr/                 # Prepared icons for HA brands repo PR
│   └── custom_integrations/smart_sniffer/
│       ├── icon.png / icon@2x.png
│       └── logo.png / logo@2x.png
│
├── images/SMARTsniffer.png         # GitHub header image
├── hacs.json                       # HACS repo metadata
├── README.md
└── LICENSE (MIT)
```

---

## Design Decisions

### Why a separate Go agent instead of running smartctl from HA directly?

Three reasons:

1. **Remote machines.** HA runs on one box. Your drives are on many. The agent model means you install a lightweight binary on each machine and HA discovers them all.
2. **Privilege isolation.** `smartctl` needs root. Running it from HA means giving HA root (or complex sudoers rules). The agent runs as root on the target and exposes read-only data over HTTP — HA only needs network access.
3. **Language fit.** Go produces a single static binary with no runtime dependencies — ideal for a system daemon you `scp` onto a Proxmox host and forget about.

### Why an enum sensor for Attention Needed instead of a binary sensor?

We iterated on this. The progression was:

1. **Binary sensor** (device_class: problem) — on/off, simple. But it conflated "drive is fine" and "we have no data" into the same "off" state.
2. **Binary sensor + severity attribute** — added `severity: critical/warning/none` as an attribute. Better, but HA doesn't let you easily automate on attribute values.
3. **Enum sensor** with four states: `YES` / `MAYBE` / `NO` / `UNSUPPORTED` — this is what shipped. Each state maps to a severity level and gets its own icon. HA can trigger automations directly on state values.

The key insight was the `UNSUPPORTED` state. USB enclosures often block SMART passthrough, so the drive appears in the API but returns no usable data. Before we added this state, those drives showed "OK" — which is a lie. Now they show "Unsupported" with an informational icon, which is accurate.

An earlier name for this state was `PENDING`, but we realized that implies "waiting for data" — which only happens before the first poll. Since HA entities don't exist until after the first poll, no user would ever see `PENDING`. `UNSUPPORTED` accurately describes the permanent condition of USB bridges blocking SMART.

### Why persistent notifications instead of automations/blueprints?

David's take: "I'm not a fan of blueprints." Fair enough.

We considered three approaches for alerting users when a drive needs attention:

1. **Blueprint automation** — user imports a blueprint and configures it. Requires setup.
2. **Service calls from the sensor** — the sensor itself fires notifications. Couples presentation to data.
3. **Persistent notifications from the coordinator** — zero user setup. The coordinator evaluates attention state after every poll and manages notifications with stable per-drive IDs.

We went with option 3. The coordinator tracks previous states to handle transitions correctly: first poll is a silent baseline (avoids notification spam on HA restart), escalation/de-escalation overwrites the existing notification (same ID), and resolution dismisses it. Full transition rules are documented in `docs/attention-severity-logic.md`.

### Why a shared `attention.py` module?

Three modules need attention logic: `sensor.py` (the enum sensor), `binary_sensor.py` (the health sensor's no-data check), and `coordinator.py` (notification decisions). Putting the logic in any one of them would create circular imports. The shared module pattern solves this cleanly — `attention.py` has no imports from the other modules.

### Why `SKIP_IF_NOT_PRESENT` for some sensors?

Samsung SSDs (specifically the 870 EVO, our test drive) don't implement ATA attribute ID 197 (Current Pending Sector Count). Without the skip gate, the sensor would show "Unknown" permanently — which is confusing because it looks like something is wrong.

The fix: at entity creation time, `sensor.py` calls `_extract_attribute()` for attributes in the `SKIP_IF_NOT_PRESENT` set. If the value is `None`, the entity is simply never created. The drive's device page only shows sensors for attributes it actually reports. Same logic applies to `Spin_Retry_Count` (HDD-only) and `Command_Timeout`.

### Why `integration_type: device` instead of `hub`?

Originally used `hub`, which shows "Add hub" in the HA UI. But SMART Sniffer doesn't monitor a hub — each config entry represents a machine running the agent, and the drives are the real devices. Changed to `device` so the UI says "Add device" instead, which better matches the mental model.

### Why mDNS/Zeroconf for auto-discovery?

HA has built-in Zeroconf support — integrations declare a service type in `manifest.json` and HA listens for it automatically. No custom scanning, no SSDP, no MQTT discovery needed. The agent registers `_smartha._tcp.local.` with TXT records (version, hostname, OS, auth status, drive count) using the `grandcat/zeroconf` Go library. HA picks it up and routes to `async_step_zeroconf()` in the config flow.

The instance name is `smartha-<hostname>`, which naturally deduplicates across machines. Each agent gets a unique mDNS identity, and the config flow uses `host:port` as the HA unique ID to prevent duplicate entries. As of v0.4.28, the `--mdns-name` flag (or `mdns_name` in config.yaml) allows overriding the instance name — needed for the HA add-on where `os.Hostname()` returns a generic container hostname like `homeassistant` on every instance, causing mDNS collisions on multi-HA networks.

One limitation: mDNS is link-local (multicast on the LAN segment). Agents on different VLANs won't be discovered without an mDNS reflector like Avahi or a router-level relay. The README documents this.

### Config resolution: file → flags

The Go agent loads config from `config.yaml` (working directory, then `/etc/smartha-agent/`), then CLI flags override. CLI always wins. This means the installers can write `config.yaml` and the agent picks it up automatically, but a user can always override with `--port 8080` for testing.

---

## Things That Broke (and How We Fixed Them)

### "Current Pending Sector Count: Unknown" on Samsung 870 EVO

Samsung SSDs don't report ATA ID 197. The sensor was being created regardless and showing "Unknown" permanently. Fixed with the `SKIP_IF_NOT_PRESENT` gate described above.

### Health showing "OK" for USB drives with no data

The health binary sensor defaulted to `False` (OK) when evaluation found no failures. A USB drive that returned zero SMART data would show "OK" — dangerously misleading. Fixed by adding `_has_usable_smart_data()` to `attention.py`. When no data is present, health returns `None` (HA renders as "Unknown") and attention returns `UNSUPPORTED`.

### `mdi:harddisk-alert` icon doesn't exist

Used this for the Attention Needed sensor — the icon just didn't render. Replaced with a `_STATE_ICONS` dict that maps each enum state to a valid MDI icon: `check-circle-outline` (NO), `alert-circle-outline` (MAYBE), `alert-octagon` (YES), `help-circle-outline` (UNSUPPORTED).

### Integration icon "not available" on HA integrations page

We generated placeholder PNG icons (dark charcoal circle with a hard disk + magnifying glass motif) and placed them in the integration directory and at the repo root. They don't work for the HA integrations page — that requires a PR to the `home-assistant/brands` repository. The placeholder images are saved in `brands-repo-pr/` for when David designs final icons.

### install.sh function ordering bug

The bash script called `install_linux_service` and `install_macos_service` at line ~192 before the function definitions at line ~199. Bash requires functions to be defined before they're called. Fixed by moving the function definitions to the top of the script, right after the helper functions.

### install.sh config corruption via `curl | bash`

When piped through `curl | bash`, stdin is the script itself — not the terminal. The `read` prompts consumed script text as input, writing literal shell expressions like `PORT="${PORT:-9099}"` into `config.yaml`. Fixed by redirecting `read` from `/dev/tty` when stdin isn't a terminal.

### install.sh "Text file busy" on reinstall

Overwriting the binary while the agent was still running caused `cp: cannot create regular file: Text file busy`. Fixed by stopping the systemd/launchd service before copying the new binary.

### install.sh uninstall env var clobbering

The uninstall check logic set `UNINSTALL=false` at the top, which overwrote the `UNINSTALL=1` environment variable passed by the caller. Fixed by saving the env var into `_UNINSTALL_ENV` before any reassignment. Also corrected the uninstall one-liner syntax — `UNINSTALL=1` must be on the `bash` side of the pipe (`curl ... | sudo UNINSTALL=1 bash`), not the `curl` side.

### install.sh bare number scan interval

Entering `30` instead of `30s` for the scan interval wrote `scan_interval: 30` to config.yaml. Go's `time.Duration` can't unmarshal a bare integer — it needs a duration string like `30s`. Fixed by detecting bare numbers and appending `s` automatically.

### mDNS instance name breaks with dotted hostnames

The `grandcat/zeroconf` library registers an mDNS service instance using the machine hostname. On macOS, `os.Hostname()` can return `MacBook-Air.localdomain` — the dots violate DNS label rules and cause `dns-sd -B` to show no instances. Fixed by stripping the domain suffix: `if idx := strings.IndexByte(hostname, '.'); idx != -1 { hostname = hostname[:idx] }`.

### `ZeroconfServiceInfo` import path changed in HA 2025.x

Adding `async_step_zeroconf()` to the config flow required importing `ZeroconfServiceInfo`. In older HA it lived at `homeassistant.components.zeroconf`; newer versions moved it to `homeassistant.helpers.service_info.zeroconf`. Using the old path caused `Failed to set up: Import error` on Production HA. Fixed with a try/except fallback.

### Blank confirmation form for no-auth discovered agents

When an agent without bearer token auth was discovered via Zeroconf, the confirm step showed an empty form with just a "Submit" button — confusing UX. Initially fixed by auto-confirming (setting `user_input = {}` to skip the form entirely). Later revised in v0.4.22 — auto-confirm was too aggressive, silently adding agents without user consent. Now all discoveries (auth and no-auth) show a confirmation form with agent details (hostname, IP, port, drive count). No-auth agents get an empty schema (description + Submit button), auth agents get the token field. Users always confirm before an agent is added.

### WD/HGST packed temperature value (v0.4.21)

Western Digital and HGST drives pack min/max/current temperatures into a single 48-bit raw value for ATA attribute 194 (Temperature_Celsius). A WD Ultrastar WD140EDGZ reported `raw.value: 214749675563` instead of the actual temperature (43°C). The raw string field contained the correct reading: `"43 (Min/Max 20/50)"`. Fixed `_extract_attribute()` in `sensor.py` to parse `raw.string` first when the raw value exceeds 300, with a bitmask fallback (`raw_value & 0xFFFF`) for drives that don't populate the string field. First community-reported issue.

### Tailscale IP selected during Zeroconf discovery (v0.4.21)

The mDNS agent advertises on all network interfaces, including Tailscale's virtual interface (100.x.x.x). HA's Zeroconf discovery grabbed the Tailscale IP, couldn't reach it from the local network, and timed out. Fixed by adding `_pick_best_ip()` to the config flow — prefers RFC 1918 private addresses (192.168.x, 10.x, 172.16-31.x) over VPN/tunnel IPs. Tailscale's 100.x CGNAT range is explicitly deprioritized.

### `asyncio.TimeoutError` not caught in config flow (v0.4.21)

Connection timeouts during config flow and options flow raised `asyncio.TimeoutError`, which wasn't in the `except` clause — it fell through to the generic "Unexpected error" handler and logged a full stack trace instead of showing the clean "Unable to connect" message. Fixed by adding `asyncio.TimeoutError` and `TimeoutError` to all three exception handlers (user step, zeroconf confirm, options flow).

### `grandcat/zeroconf` pinned stale Go x/ deps

Adding `github.com/grandcat/zeroconf v1.0.0` pulled in 2020-era `golang.org/x/{net,crypto,sys}` versions. Building with Go 1.25 failed with `invalid reference to syscall.recvmsg`. Fixed by running `go get golang.org/x/net@latest golang.org/x/crypto@latest golang.org/x/sys@latest` to pull current versions.

### `__pycache__` and build binaries in the repo

The `.gitignore` covers `__pycache__/` and `agent/build/`, but these were committed before the gitignore existed. They're harmless but should be cleaned up with `git rm -r --cached` in a future commit.

---

## The Attention System — How It Works

This is the most complex piece. Rather than duplicate the full docs here, see `docs/attention-severity-logic.md` for the complete specification. The short version:

The system evaluates every drive after each poll and classifies it into one of four states based on SMART attribute values. Critical indicators (reallocated sectors, pending sectors, uncorrectable errors, NVMe critical_warning/media_errors/spare depletion) → `YES`. Warning indicators (reallocated events, spin retry, command timeout, NVMe low spare/high percentage_used) → `MAYBE`. All clear → `NO`. No usable data → `UNSUPPORTED`.

The coordinator tracks state transitions and fires/updates/dismisses persistent notifications automatically. The first poll after HA starts is always a silent baseline — no notifications fire until the state *changes*.

See also: `docs/early-warning-attributes.md` for the research behind which attributes we monitor and why, and `docs/smart-attribute-name-variants.md` for the manufacturer-specific attribute name mapping.

---

## Build & Release Pipeline

### GitHub Actions (`.github/workflows/release.yml`)

Push a `v*` tag to trigger:

1. Checks out the code
2. Sets up Go from `agent/go.mod`
3. Runs `make all VERSION=x.y.z` in `agent/` — produces 6 binaries (linux-amd64, linux-arm64, linux-arm, darwin-amd64, darwin-arm64, windows-amd64)
4. Generates SHA256 checksums
5. Creates a GitHub Release with all artifacts + one-liner install commands in the release notes

### Makefile targets

- `make` — build for current platform
- `make all` — cross-compile all 6 targets
- `make checksums` — SHA256 for all binaries in `build/`
- `make release` — `all` + `checksums` (CI entrypoint)
- `make clean` — remove build directory

### Installers

**`install.sh` (Linux + macOS)** — `curl -sSL ... | sudo bash`

Detects OS and architecture, downloads the correct binary from GitHub Releases, verifies SHA256 checksum, installs smartmontools if missing, prompts for port/token/interval, presents a network interface picker for mDNS advertisement (labels Docker/ZeroTier/Tailscale/WireGuard/utun interfaces), writes config, installs as systemd service (Linux) or launchd daemon (macOS), runs a health check. As of v0.4.24, probes for writable install paths on immutable-rootfs platforms (ZimaOS, CasaOS) — see `docs/platform-install-paths.md`. As of v0.4.28, detects existing config on upgrade and offers to keep it (default yes) — no re-entry of settings needed. Old configs without `advertise_interface` get a one-time interface prompt on multi-homed hosts.

**`install.ps1` (Windows)** — `irm ... | iex`

Same flow adapted for PowerShell: downloads binary, verifies checksum, checks for smartmontools (offers winget/choco install), prompts for config, installs as a Windows service with automatic restart on failure.

Both installers support pinning a version via environment variable (`VERSION=0.1.0`).

---

## Release Process

HACS detects integration updates via **GitHub Release tags**, not commits on main. Bumping `manifest.json` alone does not trigger an update in HA.

**To ship an integration update:**

1. Bump `version` in `custom_components/smart_sniffer/manifest.json`
2. Add a CHANGELOG entry
3. Commit and push to main
4. Create and push the tag:
   ```bash
   git tag v0.4.XX && git push origin v0.4.XX
   ```
5. The `release.yml` GitHub Action builds agent binaries, generates checksums, and creates the GitHub Release automatically

**Release body template (v0.4.27+):** The `release.yml` body now includes three sections: **What's New** (links to CHANGELOG — replace the HTML comments in the template with version-specific bullet points before tagging for best results), **Update Your Agents** (the reinstall one-liner + guidance), and **Fresh Install** (for new users). This body is displayed directly in the HA update screen when users click "Read release announcement" — it's the primary way existing users learn they need to update their agents too.

**Checklist for every release:**
1. Write the CHANGELOG entry
2. Optionally edit the `body:` HTML comments in `release.yml` to inline the highlights (or rely on the CHANGELOG link)
3. Bump manifest.json version
4. Commit, push, tag, push tag
5. Verify the release page on GitHub — the body should make sense to a user seeing it in HA

**Node.js 20 deprecation warning (June 2026):** GitHub Actions will force Node.js 24 starting June 2, 2026. Our workflow uses `actions/checkout@v4`, `actions/setup-go@v5`, and `softprops/action-gh-release@v2` which are all on Node.js 20. Check for updated versions before June or set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` to opt in early.

---

## Brand Icon Locations

As of HA 2026.3 ([Frenck's blog post, Feb 2026](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)), custom integrations ship their own brand images locally. HA serves them through a proxy API (`/api/brands/integration/{domain}/{image}`), and local images take priority over the CDN.

There are **two separate icon locations** that serve different purposes:

| Location | Purpose | Required files |
|----------|---------|----------------|
| `custom_components/smart_sniffer/brand/` | HA 2026.3+ local brand proxy (integration settings, device pages) | `icon.png`, `logo.png` (+ `@2x` variants) |
| `brand/` (repo root) | HACS update entity card, HACS dashboard | `icon.png`, `logo.png` (+ `@2x` variants) |

**HA 2026.3 also supports dark mode variants:** `dark_icon.png`, `dark_icon@2x.png`, `dark_logo.png`, `dark_logo@2x.png`. These are placed alongside the existing files in the same `brand/` directories. Not yet implemented — on the to-do list.

The old approach of PRing to `home-assistant/brands` is no longer needed for custom integrations. The legacy `/brands/` directory (a fork of that repo used during initial development) is excluded via `.gitignore`.

---

## Known Issues & Tech Debt

1. ~~**`--config` flag not implemented in Go agent.**~~ Resolved in v0.4.25. The agent now accepts `--config /path/to/config.yaml` to specify an explicit config file path. Auto-detection (CWD then `/etc/smartha-agent/`) still works as the default.

2. ~~**Committed build artifacts.**~~ Cleaned in v0.4.22. Removed `agent/agent`, `agent/build/`, and `__pycache__/` from tracking via `git rm -r --cached`.

3. ~~**Integration icons.**~~ Resolved in v0.4.22. Final icons cropped from header art are now in `custom_components/smart_sniffer/brand/`. The `home-assistant/brands` repo no longer accepts custom integration PRs — as of HA 2026.3, custom integrations ship their own icons via the `brand/` directory. The `brands-repo-pr/` directory has been removed.

4. ~~**Legacy install scripts.**~~ Removed in v0.4.22. The platform-specific scripts (`agent/install-linux.sh`, `agent/install-macos.sh`, `agent/windows/`) have been deleted — superseded by the unified `install.sh` and `install.ps1` at the repo root.

5. ~~**`early-warning-attributes.md` references binary sensor for attention.**~~ Fixed in v0.4.22 — updated to enum sensor references with correct states and YAML examples.

6. ~~**README entity table is stale.**~~ Reviewed in v0.4.24 — table is now current with enum states and all sensors listed.

7. **SK Hynix wear leveling name unverified.** Noted in `smart-attribute-name-variants.md`. Need a `smartctl -a` dump from an SK Hynix SSD to confirm the attribute name.

8. **No tests.** Neither the Go agent nor the Python integration have automated tests. The agent has been tested manually against real drives on Proxmox. The integration has been tested on a HA Test instance (LXC container).

---

## Test Environment

- **Proxmox VE host** running the Go agent, with:
  - Samsung 870 EVO 500GB (SATA SSD) — primary test drive for ATA path
  - Samsung 860 EVO 1TB (SATA SSD) — second ATA drive
  - External USB drive (via USB enclosure) — tests the UNSUPPORTED/no-data path
  - NVMe drive(s) for NVMe path testing
- **Home Assistant Test instance** — LXC container on the same Proxmox host, running the `smart_sniffer` integration pointed at the local agent
- **Home Assistant Production instance** — separate, used for final validation
- **Kali Linux VM** (QEMU on Proxmox) — used for install.sh end-to-end testing. QEMU virtual disk correctly reports as UNSUPPORTED (no SMART data). Verified install, reinstall, uninstall, and bearer token auth.
- **MacBook Air** (Apple Silicon) — used for macOS install.sh testing. Verified install, uninstall, and HA data flow (entities go unavailable on uninstall, recover on reinstall).
- **ZimaOS** (CasaOS/IceWhale, x86_64) — immutable rootfs NAS. Verified writable path probing (`/DATA/smartha-agent/`), interface filtering (skips docker0, zt*, veth*, br-*, virbr0), preferred IP TXT record, and mDNS discovery at correct LAN IP. 3 NVMe drives detected.

---

## What's Next

Immediate:

- [ ] Fix `--config` flag in Go agent (or fix Windows installer to use working directory)
- [ ] Clean committed build artifacts from git history
- [x] Update README entity table with all current sensors -- ✅ shipped v0.5.4 (added per-agent entity table covering v0.5.3 diagnostics + v0.5.4 Drive Standby and Agent OS)
- [x] Update `early-warning-attributes.md` YAML examples (binary → enum) — ✅ fixed in v0.4.22
- [x] Validate release workflow (push `v0.1.0` tag, watch Actions) — ✅ working
- [x] Test `install.sh` on Linux — ✅ tested on Kali VM (QEMU). Install, reinstall, uninstall all working. Auth token verified.
- [x] Test `install.sh` on macOS — ✅ tested on MacBook Air. Install, uninstall working. HA integration confirmed receiving data.
- [ ] Test `install.ps1` on Windows

v0.3.0 (shipped):

- [x] **Auto-discovery via mDNS/Zeroconf** — agents advertise `_smartha._tcp.local.`, HA discovers them automatically via built-in Zeroconf. Agent uses `grandcat/zeroconf` Go library. Config flow has `async_step_zeroconf()` with pre-filled host/port and conditional token prompt. Tested on macOS (MacBook Air) with both auth and no-auth flows.

Future:

- [ ] **HAOS App** — Docker-based HA App (formerly "add-on") packaging the Go agent + smartmontools for direct HAOS installs (no separate machine needed). Likely a separate repo (`smart-sniffer-addon`). Needs privileged device access for `smartctl`.
- [x] ~~Design final integration icons and PR to `home-assistant/brands`~~ — No longer needed. HA 2026.3 supports local `brand/` directory. Icons shipped in integration.
- [x] ~~Add `--config` CLI flag to Go agent for explicit config file path~~ — ✅ Resolved in v0.4.25.
- [ ] **Dark mode brand icons** — HA 2026.3 supports `dark_icon.png` / `dark_logo.png` variants. Need to create inverted/dark-background versions of current crops.
- [x] ~~**Agent version repair notifications**~~ — ✅ Implemented in v0.4.28. Coordinator checks `/api/health` version every poll, raises per-host HA repair card when outdated, auto-clears on upgrade. Config flow warns at zeroconf discovery. Design doc at `docs/agent-version-repair.md`.
- [x] ~~**Improved release descriptions**~~ — ✅ Release body template now includes What's New, Update Your Agents, and Fresh Install sections (v0.4.27).
- [ ] **GitHub Actions Node.js 24 migration** — Update workflow actions before June 2, 2026 deadline.
- [ ] MQTT agent mode for environments where direct HTTP isn't ideal
- [ ] Custom Lovelace card for drive health at a glance
- [ ] **Temperature-based attention triggers** — absolute threshold (e.g., > 55°C = MAYBE) and trend-over-time detection (sustained rise). Requires storing historical temperature readings in the coordinator.
- [ ] Configurable thresholds via HA options flow
- [ ] SAS/SCSI drive support
- [ ] Automated tests for both agent and integration

---

## Reference Docs

These companion documents cover specific subsystems in depth. Prefer reading them over this journal for implementation details:

- **[attention-severity-logic.md](attention-severity-logic.md)** — Complete attention state machine, severity rules, notification lifecycle, transition table
- **[early-warning-attributes.md](early-warning-attributes.md)** — Which SMART attributes predict failure and why we monitor them
- **[smart-attribute-name-variants.md](smart-attribute-name-variants.md)** — Manufacturer-specific attribute name research for the `ata_name_map`
- **[platform-install-paths.md](platform-install-paths.md)** — Install locations, immutable rootfs support, network interface filtering
- **[agent-version-repair.md](agent-version-repair.md)** — Design doc for HA repair notifications when agent is outdated
