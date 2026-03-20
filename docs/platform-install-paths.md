# Platform Install Paths

The SMART Sniffer agent installer automatically detects the best install location for each platform. Some operating systems (particularly NAS-oriented distros) use immutable or read-only root filesystems that prevent writing to standard paths like `/usr/local/bin`. The installer probes for writable locations at runtime and adapts accordingly.

## How It Works

When the installer runs, it tries each candidate path in order, using the first one that is writable:

| Priority | Binary Path | Config Path | Platforms |
|----------|-------------|-------------|-----------|
| 1 | `/usr/local/bin/smartha-agent` | `/etc/smartha-agent/` | Most Linux distros, Proxmox, Debian, Ubuntu, macOS |
| 2 | `/DATA/smartha-agent/smartha-agent` | `/DATA/smartha-agent/` | ZimaOS, CasaOS |
| 3 | `/opt/smartha-agent/smartha-agent` | `/opt/smartha-agent/` | Generic fallback for any other restricted filesystem |

The probe runs as root (the installer requires `sudo`), so a writability failure means the filesystem itself is read-only, not a permissions issue.

## Immutable Root Filesystems

Some NAS and appliance distros lock down the root filesystem for reliability and update safety. The entire rootfs — including `/usr`, `/etc`, `/bin` — is mounted read-only or as an immutable image. User data lives on a separate writable partition.

### ZimaOS / CasaOS

ZimaOS (built on CasaOS by IceWhale) uses a RAUC A/B partition scheme with an immutable root filesystem. Key characteristics:

- `/usr/local/bin` — **read-only**, even with sudo
- `/etc/systemd/system` — **writable** with sudo (systemd services work normally)
- `/DATA` — **writable** with sudo, designated user/app data partition
- Package managers (apt) — **not available** on the immutable rootfs

The installer detects this by attempting to `mkdir -p /usr/local/bin`. When that fails and `/DATA` exists and is writable, it installs everything under `/DATA/smartha-agent/`.

### TrueNAS SCALE / Unraid (untested)

These NAS distros may have similar restrictions. The `/opt` fallback exists as a generic safety net. If you encounter issues on these platforms, please open a GitHub issue.

## How the Agent Finds Its Config

The Go agent (`smartha-agent`) searches for `config.yaml` in two locations, in order:

1. `config.yaml` — relative to the working directory
2. `/etc/smartha-agent/config.yaml` — hardcoded fallback

The systemd service unit sets `WorkingDirectory` to match `INSTALL_CFG`, so the relative path resolves correctly regardless of where the config was installed. No changes to the Go binary are needed for alternate install paths.

## Network Interface Filtering

Machines with Docker, VPNs (ZeroTier, Tailscale, WireGuard), or virtual bridges have multiple network interfaces. Without filtering, the agent advertises mDNS on all of them, causing Home Assistant to see duplicate discoveries at unreachable IPs.

### Smart Defaults

When no `advertise_interface` is configured, the agent automatically skips known virtual interface prefixes: `docker*`, `br-*`, `veth*`, `zt*`, `tailscale*`, `ts*`, `wg*`, `virbr*`, `vbox*`, `vmnet*`, `lo`. It advertises on all remaining interfaces.

### Explicit Interface

Set `advertise_interface` in `config.yaml` to restrict mDNS to a single interface:

```yaml
advertise_interface: eth0
```

The installer presents an interface picker during setup on machines with multiple interfaces. Users can also set this manually after install.

### Preferred IP TXT Record

The agent includes an `ip=` field in its mDNS TXT record containing its best LAN IP address. The HA integration trusts this over its own IP scoring when present. This ensures HA always connects to the right address, even when mDNS reflectors or multi-homed networks are involved.

### CLI Override

Use `--interface` to override the config file:

```bash
smartha-agent --interface eth0
```

## Uninstaller

The uninstaller checks all candidate locations automatically. It doesn't need to know where the agent was originally installed — it scans all three paths and removes any files it finds.

```bash
curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo UNINSTALL=1 bash
```

## Adding New Platforms

When a new platform with non-standard paths is encountered:

1. **Identify the writable data partition** — run `mount | grep -v "ro,"` to find read-write mounts
2. **Test writability** — `sudo mkdir -p /candidate/path && sudo touch /candidate/path/test && sudo rm /candidate/path/test`
3. **Test systemd** — `sudo touch /etc/systemd/system/test.service && sudo rm /etc/systemd/system/test.service`
4. **Add a new candidate** to `resolve_install_paths()` in `install.sh`, between the `/DATA` and `/opt` entries
5. **Update the uninstaller** `do_uninstall()` to scan the new path
6. **Update this doc** with the new platform details

When the number of platform-specific overrides grows beyond 3-4, consider factoring the path variables into per-platform config files (e.g., `install/platforms/zimaos.conf`) that the installer sources at runtime.
