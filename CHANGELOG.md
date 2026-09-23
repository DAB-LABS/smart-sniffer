# Changelog

All notable changes to SMART Sniffer are documented here.

## v0.6.5 -- 2026-09-23

**Integration only. No agent update needed.** Update through HACS and reload.

Two changes to how SMART Sniffer's devices behave in Home Assistant.

### Fixed
- **No more "via_device" deprecation warnings in the log.** Home Assistant 2026.9
  started warning that SMART Sniffer links drives to their agent the old way, and
  that the old way stops working in 2027.8. Drives now use the new link on
  Home Assistant versions that support it and the old one on earlier versions.
  Nothing changes in how drives are grouped. Reported by @hole222 in
  [#53](https://github.com/DAB-LABS/smart-sniffer/issues/53).

### Added
- **You can now remove drives the agent no longer reports.** A drive that has
  been taken out, or a leftover "ghost" device from before v0.6.2, can be removed
  with **Remove device** on its page. SMART Sniffer allows it only when the agent
  is online and no longer sees that drive.
- **Clear reasons when a device can't be removed.** Removing a drive that is still
  installed, or the agent itself, now explains why and what to do instead.

### Upgrade notes
- **Correction to the v0.6.2 notes.** They said a leftover ghost device could be
  deleted in Settings. It could not, because SMART Sniffer did not allow device
  removal until this release. Now it does.
- **Remove device appears on every SMART Sniffer device.** Home Assistant shows it
  per integration, not per device, so live drives and agents have it too. On those
  it explains why it can't remove them and leaves them in place.
- If you remove from the **Devices** page rather than from the SMART Sniffer
  integration page, Home Assistant may show `[object Object]` instead of the
  reason. That is a Home Assistant display issue; the reason is still written to
  the log.
- The agent is unchanged since v0.6.2. Nothing to re-run on your monitored
  machines.

## v0.6.4 -- 2026-09-22

**Integration only. No agent update needed.** Update through HACS and reload.

A housekeeping release with nothing new to see. It exists so SMART Sniffer can
apply to the HACS default store, which asks for a release made after the
repository passes HACS's and Home Assistant's own validators.

### Changed
- **The "update your agent" repair notice reads exactly as before.** Its install
  link is now filled in by the integration instead of being written into the
  notice text, which Home Assistant's validator requires.

### Behind the scenes
- **The declared minimum Home Assistant version is now tested.** Every push
  installs the version `hacs.json` declares and checks the integration loads on
  it. This is the check that would have caught the 2024.4 problem corrected in
  v0.6.3.
- **HACS and Home Assistant validation run on every push**, and weekly, so a
  rule change upstream shows up here before it reaches a release.

## v0.6.3 -- 2026-09-21

**Integration only. No agent update needed.** Update through HACS and reload.

The headline is configurable thresholds: a drive with old, stable damage can stop
nagging without you losing the ability to hear about new damage.

### Added
- **Per-drive alert thresholds** -- every drive gets its own page under Configure,
  listing each monitored attribute with what the drive currently reads and the
  built-in default underneath. Type a threshold and attention is raised only
  when the reading passes it. Type the default back in to clear one. Requested
  by @nsleigh in [#36](https://github.com/DAB-LABS/smart-sniffer/issues/36).

### Fixed
- **A drive with no readable SMART data could stop the integration from
  loading** -- an empty payload raised an error during setup, which took out
  every drive and filesystem on that agent rather than the one drive. Most likely
  to be hit by anyone still running an agent older than v0.6.2, where an
  unreadable drive was published with an empty payload. Unreadable data is now
  treated as "no data", which is what it is.

### Changed
- **A drive that reports no health status is no longer shown as FAILED** -- the
  SMART Status sensor previously read FAILED when the drive had sent nothing at
  all, which is alarming and wrong. It is now blank in that case. A drive that
  genuinely reports a failing status still reads FAILED. Missing data and bad
  data are different things and now look different.

### Upgrade notes
- **The declared minimum Home Assistant version is now 2024.4**, up from 2024.1.
  This is a correction, not a new requirement: the integration has needed 2024.4
  since v0.3.0 and could not be added or configured on anything older. If you
  are on 2024.1 through 2024.3, nothing changes for you, because it was not
  working there already.
- Thresholds start empty, so behaviour is identical to v0.6.2 until you set one.
  Nothing to do if you do not want the feature.
- A threshold equal to the default is not stored. Setting wear to 90 when the
  default is 90 is the same as leaving it alone, which is why the form shows the
  default under every field.
- The agent is unchanged since v0.6.2. Nothing to re-run on your monitored
  machines.

## v0.6.2 -- 2026-09-18

**Agent update. Re-run the installer on each monitored machine.** The integration is unchanged apart from its version number; take the HACS update so the versions match, but there is nothing new on that side.

Three agent bugs, all about what happens when a drive cannot be read. If you updated to v0.6.1 yesterday, sorry for the quick follow-up. These were found during that release and are worth fixing now rather than holding.

### Fixed
- **A stuck drive could make every drive on the agent disappear** -- if smartctl timed out or failed to run against one device, the agent published a broken record for it, and Home Assistant treated the whole agent as unreachable. One hung disk blanked all of your drives at exactly the moment you would want to see them. The agent now skips a drive it could not read and keeps serving the others.
- **A drive the agent could not read appeared as a second, fake drive** -- when smartctl was blocked from opening a device, usually by Protection Mode in the Home Assistant App, the agent invented a name for it such as `dev-nvme0`. Home Assistant registered that as a new device while the real one went unavailable, and cleaning up meant hand-editing the entity registry. Unreadable drives are now skipped and the real drive keeps its identity and last-known data. Reported by @Patrick384600 in [smart-sniffer-app#7](https://github.com/DAB-LABS/smart-sniffer-app/issues/7).
- **The 30-second hang protection from v0.6.0 could silently not fire** -- it stopped smartctl itself but kept waiting if smartctl was a wrapper script or had started a helper process. The wait is now bounded regardless of what smartctl spawned.
- **A sleeping drive and an unreadable drive are no longer confused** -- both produce the same smartctl exit code, so the agent now checks what smartctl actually reported instead of guessing.

### Added
- **`readable` field on `/api/drives` and `/api/drives/{id}`** -- `false` when the entry is last-known data for a drive the agent could not read this cycle.

### Upgrade notes
- Re-run the installer on each machine running the agent. The Home Assistant App will pick the new agent up on its next rebuild.
- **If you have a ghost drive device from the second bug**, it will stop being recreated after the update. Delete it in Settings, Devices and Services once the real drive is reporting again.
- Take the HACS update too so the integration version matches. It is a version bump only.
- **Running the agent inside a VM?** A virtual disk that reports no SMART data used to appear as a drive with an empty serial and no readings. It no longer appears at all. That is the fix working, not a lost drive; disk usage monitoring for that VM is unaffected. If a stale device is left behind in Home Assistant, delete it.

## v0.6.1 -- 2026-09-17

**Integration only. No agent update needed.** Update through HACS and reload.

A small follow-up to v0.6.0: a monitoring gap on some SK Hynix SSDs, and tidier device organisation in Home Assistant.

### Fixed
- **Reallocated sectors were not detected on some SK Hynix SSDs** -- those drives name the attribute `Retired_Block_Count` rather than the usual name, so the most important health indicator we track reported nothing on them. If you have an SK Hynix SATA SSD, this is worth updating for. Found in a drive dump from @jackeichen in [#27](https://github.com/DAB-LABS/smart-sniffer/issues/27).
- **Used reserved block count is recorded as a number on SK Hynix drives too** -- the same attribute is spelled differently by different vendors, and one spelling was missed.

### Added
- **Drives now nest under their agent** -- each drive and the Disk Usage device show "Connected via" their agent, and the agent's page lists them under Connected devices, so a NAS with six drives reads as one machine instead of seven loose devices. Requested by @wkearney99 in [#24](https://github.com/DAB-LABS/smart-sniffer/issues/24). Note this groups the devices; it does not set their area. To assign an area to all drives at once, tick them on the Devices page and use Move to area.
- **`--help` for the installer**, documenting `--install-dir` and the uninstall options.

### Upgrade notes
- Update through HACS and reload the integration. The agent is unchanged, so there is nothing to re-run.
- Existing drive devices keep their entity IDs. Nesting them under the agent does not rename anything.
- **SK Hynix owners:** a drive that previously looked healthy may now report attention if it has retired blocks. That is the fix working, not a new fault.

## v0.6.0 -- 2026-09-16

**Update both the agent and the integration.** Several fixes below need both sides current.

The first release since June, covering three months of work. Disk usage figures are now correct (they read low before, and ZFS pools showed 0%), the agent's logs are quiet instead of repeating every minute, and the installer works on TrueNAS.

Some figures will visibly change after you update. That is expected -- see Upgrade notes.

### Added
- **ZFS pools show real usage** -- a pool path such as `/rpool` reported near 0% no matter how full it actually was. Fixes #31, reported by @nsleigh.
- **Disk usage from inside a container** -- set `mount_prefix` in config.yaml, or `SMARTHA_MOUNT_PREFIX`, and the agent reads the host's filesystems instead of the container's own.
- **Unbroken graphs for time-series users** -- a new option writes a value on every poll, so InfluxDB, Prometheus and Grafana stop showing gaps. Off by default. Implements #40, requested by @xxxarmitagexxx.
- **Diagnostic attributes are numbers now** -- CRC errors, LBAs written, start/stop counts and similar keep statistics and can be graphed. Fixes #47, reported by @spry-salt.
- **The installer works on TrueNAS** -- plus a new `--install-dir` option for any system with a read-only filesystem. Fixes #46, reported by @cgtechuk.
- **The Windows installer opens the firewall port** -- this was the most common cause of "Unable to connect" on Windows.
- **A `--verbose` flag** -- brings back per-cycle logging when you are diagnosing something.

### Fixed
- **Disk usage percentage read too low** -- it counted reserved space as usable, so it disagreed with `df`. It now matches. Fixes #39, reported by @xxxarmitagexxx.
- **`device_overrides` was ignored for scsi, ata and nvme** -- the setting was accepted but never actually applied, so drives that needed it kept failing to report. Fixes #43, fixed by @KruseLuds in PR #48.
- **Blank model names on SAS and USB drives** -- also from @KruseLuds in PR #48.
- **Wrong values on some SSDs** -- certain drives pack several counters into one field, so a temperature could arrive as 244813987870. These are now decoded everywhere, including diagnostic entities. Fixes #44, reported by @mark4068-dotcom with diagnostics from @Vict20.
- **The agent logged the same line every minute** -- thousands of identical warnings a day, even on healthy drives. A message now logs once, again if it changes, and otherwise hourly at most. Addresses [smart-sniffer-app#3](https://github.com/DAB-LABS/smart-sniffer-app/issues/3), reported by @gbravery.
- **One stuck drive could freeze all monitoring** -- each check now gives up after 30 seconds.
- **Windows drive paths displayed backwards in the card** -- `C:\` appeared as `\:C`. Fixed by @nsleigh in PR #41.

### Upgrade notes
- Update the agent (re-run the installer) **and** the integration (via HACS), then reload the integration.
- **Figures will move.** Disk usage reads slightly higher on Linux, and ZFS pools jump from near 0% to their real value. Both are corrections, not faults.
- **TrueNAS users:** re-run the installer and it will find a writable location. To survive a major TrueNAS upgrade, use `--install-dir=/mnt/<pool>/<dataset>`.
- **If you use `device_overrides`:** it now takes effect. Check the protocol you set is the one you want.
- **For InfluxDB, Prometheus or Grafana:** turn on "Write a value on every poll" in the integration's options.

## v0.5.16 -- 2026-06-10

Agent-only release. No integration or installer changes. Fixes [smart-sniffer-app#6](https://github.com/DAB-LABS/smart-sniffer-app/issues/6).

### Fixed
- **Health endpoint no longer blocked by bearer token auth** -- when a bearer token is configured, `/api/health` was behind the auth middleware. External systems polling the health endpoint without a token (Supervisor watchdog, load balancers, monitoring tools) received 401 instead of 200, which caused the HAOS App's Supervisor watchdog to restart the container in a loop (~every 4 minutes). The health endpoint is now exempt from auth. It only exposes operational status (version, uptime, drive count) -- no SMART data or drive identifiers. Reported by @circa1665.

### Upgrade Notes
- **Agent-only update.** Rebuild or download the updated agent binary. No integration or installer changes needed.
- **HAOS App users with a bearer token:** update the agent to stop the watchdog restart loop. The App will also ship a workaround (TCP watchdog) for users who haven't updated the agent yet.
- **Standalone agent users:** this bug was invisible to you (nothing externally health-checks the standalone agent), but updating is still recommended.

## v0.5.15 -- 2026-06-09

Integration-only release. No agent or installer changes. Community contribution by @nsleigh.

### Added
- **`smart_sniffer.get_drive_data` service call** -- returns the full cached SMART dataset for any agent, including raw attributes, our attention evaluation (state, severity, reasons), filesystem usage, and agent metadata. Designed for automation workflows that pipe drive data to an AI for analysis. Call it from an automation, pass the JSON to a conversation agent (Google AI, OpenAI, Claude, Ollama), and get back a plain-English health report. Also works from Developer Tools > Services for manual inspection. Implements #37, contributed by @nsleigh in PR #38.

### Upgrade Notes
- **Integration-only update.** Update via HACS or manually replace `custom_components/smart_sniffer/`. No agent update needed.
- **New service:** after updating, the `smart_sniffer.get_drive_data` service appears in Developer Tools > Services. Select your agent from the dropdown and call it to see the full response.

## v0.5.14 -- 2026-05-30

Integration-only release. No agent or installer changes.

### Fixed
- **Multi-variant SMART attributes no longer silently hidden** -- drives that report multiple attributes from the same consolidated sensor group (e.g. both Wear_Leveling_Count and Remaining_Lifetime_Perc) previously only showed the first match. The other variants were suppressed by the diagnostic entity loop because they were considered "already covered." Now only the winning variant is suppressed -- the rest surface as disabled-by-default diagnostic entities like any other uncovered attribute. Most drives report a single variant and see no change. Drives with multiple variants (common on Silicon Motion controller SSDs, some WD/HGST enterprise drives) now expose the full picture.

### Upgrade Notes
- **Integration-only update.** Update via HACS or manually replace `custom_components/smart_sniffer/`. No agent update needed.
- **Multi-variant drive users:** after updating, check your drive's device page -- you may see new disabled diagnostic entities for attributes that were previously hidden. Enable the ones you care about.

## v0.5.13 -- 2026-05-20

Agent-only release. No integration or installer changes.

### Fixed
- **Custom protocols in `device_overrides` now work** -- the agent previously only passed `-d sat` to smartctl, silently dropping any other protocol. Protocols like `jmb39x-q,N` (JMicron USB RAID bridges), `usbcypress`, `megaraid,N`, and others were accepted in the config but never sent to smartctl, causing "command line parse error" failures. The agent now passes any custom protocol through to smartctl via `-d`. Standard scan-detected protocols (ata, scsi, nvme) are still handled automatically. Fixes #29.

### Added
- **v0.5.8 through v0.5.12** (installer-only, not individually changelogged):
  - **Drive picker** -- the installer now shows all detected drives and lets you choose which ones to monitor. Remote transports (iSCSI, Fibre Channel) and unknown transports are flagged so you can exclude them. Useful for Proxmox hosts with iSCSI LUNs, NAS boxes with USB backup drives, or any setup where you don't want every block device polled.
  - **`exclude_devices` config field** -- YAML list of device paths the agent skips during scan. Symlink-aware. Set automatically by the drive picker or manually in config.yaml.
  - **`--discover` exclude annotation** -- excluded devices show `[excluded by config]` in discovery output.
  - **NVMe namespace path fallback** -- the drive picker now correctly maps NVMe controller paths (e.g. `/dev/nvme0`) to namespace paths (`nvme0n1`) for enrichment from lsblk.
  - **Robust lsblk parsing** -- switched from positional column parsing to `lsblk -P` key-value pairs, eliminating column-alignment bugs when fields are empty.

### Upgrade Notes
- **Agent-only update.** Re-run the installer or replace the binary. No integration changes needed.
- **If you use `device_overrides` with non-SAT protocols:** update to v0.5.13 and your overrides will start working. Re-run `smartha-agent --discover` to verify.

## v0.5.7 -- 2026-05-16

Integration-only release. No agent or installer changes.

### Added
- **SAS/SCSI drives now get basic monitoring** -- temperature, power-on hours, and power cycle count now work for SAS drives (protocol "SCSI"). Previously these sensors returned nothing because the integration only had ATA and NVMe extraction paths. The data was already in the smartctl JSON output at the top level -- we just weren't reading it. SAS drives still report SMART health status as before; full SCSI attribute parsing (grown defect list, error counters, endurance indicators) is future work.
- **Vendor-specific SMART attributes as diagnostic entities** -- for ATA/SATA drives in the smartctl database, every named SMART attribute that isn't already a dedicated sensor now appears as a disabled-by-default diagnostic entity. Enable the ones you care about from your drive's device page. Covers attributes like erase counts, host writes, program/erase failures, available reserved space, and anything else your drive reports with a recognized name. Drives not in the smartctl database (where attribute names show as "Unknown") don't get junk entities.

### Changed
- **Internal refactor:** ATA attribute name mapping moved to module level for reuse. No behavior change.

### Upgrade Notes
- **Integration-only update.** Update via HACS or manually replace `custom_components/smart_sniffer/`. No agent update needed.
- **SAS users:** reload the integration after updating to pick up the new sensors. Your SAS drives should immediately show temperature and power-on hours.
- **ATA/SATA users:** check your drive's device page after updating -- you'll see new disabled diagnostic entities in the entity list. Nothing changes on your dashboard unless you choose to enable them.

## v0.5.6.1 -- 2026-05-04

Installer-only patch. No agent, integration, or config changes.

### Fixed
- **macOS installer crash during filesystem picker** -- the bind-mount deduplication logic (added in v0.5.6) used bash 4+ associative arrays (`local -A`), which crash on macOS's built-in bash 3.2. The grouping code is now gated behind a `mountinfo` check so it only runs on Linux, where bind mounts exist and bash 4+ is standard. On macOS and BSD, every filesystem entry is treated as canonical with no bind-mount grouping. No functional change on Linux.
- **macOS installer summary showed grep error** -- the post-install IP detection used `grep -P` (Perl regex), which is not available on macOS. Replaced with portable `grep -oE` and reordered to try `ifconfig` before `ip` so macOS hits the working path first.

## v0.5.6 -- 2026-05-02

Agent + integration release. Both components updated.

### Fixed
- **Wear level sensor now reports consistent "percentage used" across ATA and NVMe drives** -- ATA SSDs report a normalized SMART value where 100 means "new" and 0 means "worn." NVMe drives report `percentage_used` where 0 means "new" and 100 means "worn" -- the opposite scale. Previously, the integration passed both values through as-is, so the same sensor meant opposite things depending on drive protocol. Now ATA values are inverted to match NVMe: 0% = new drive, 100% = fully worn. **Breaking change:** if you have automations based on the ATA wear sensor, your values will invert (e.g., a new Samsung 870 EVO previously showed 99, now shows 1).
- **Installer filesystem picker: bind mount deduplication** -- the picker now parses `/proc/self/mountinfo` (when available) and deduplicates entries by `(source, fstype, root)`. Previously, bind mounts of the same filesystem appeared as separate entries, tripling the list on systems like ZimaOS. Falls back to `/proc/mounts` and then `mount` on systems where mountinfo is not available.
- **Installer filesystem picker: path unescaping** -- mount paths containing spaces, tabs, newlines, or backslashes are now displayed correctly. The kernel escapes these characters in mountinfo/proc output (`\040` for space, etc.) and the installer previously showed the raw escaped strings.
- **Installer summary now shows the correct IP** -- the post-install summary previously showed the first IP from `hostname -I`, which on systems with Docker bridges or virtual interfaces was often an unreachable internal IP (e.g., `172.18.0.1`). It now shows the IP of the mDNS-advertised interface you selected during setup.

### Added
- **ATA SSD wear now triggers attention warnings** -- ATA SSDs with wear level at 90% or higher (after inversion to "percentage used") now fire a WARNING in the Attention Needed sensor, matching the existing NVMe threshold. Previously only NVMe drives got wear-based attention warnings.
- **btrfs filesystem fallback** -- when `statvfs` returns zero for a btrfs mount (a known quirk on some multi-device or DUP-profile configurations), the agent falls back to `btrfs filesystem usage --raw` for accurate size/usage data. Requires `btrfs-progs` to be installed (most btrfs systems have it). Without it, the mount reports as `(unknown size)` in the picker and zero-byte usage from the API.
- **Installer: bind mount hiding** -- bind mounts of subdirectories are hidden by default behind a `[+N bind mounts hidden]` tag with a y/N prompt to reveal them. Reduces noise on systems with many bind mounts.

### Changed
- **Sensor name:** "Wear Leveling / Percentage Used" renamed to "Wear Level (% Used)" for clarity.

### Upgrade Notes
- **Both agent and integration should be updated.** Replace the agent binary or re-run the installer. Update the integration via HACS or manually.
- **Wear sensor breaking change:** ATA SSD wear values are inverted. If you have automations checking wear level, review your thresholds. The sensor now consistently means "percentage of rated life consumed" for both ATA and NVMe. A new drive reads ~0-1%, a heavily worn drive reads 90%+.
- **btrfs users:** install `btrfs-progs` if not already present for accurate filesystem reporting. The agent works without it but btrfs mounts will show zero usage.

## v0.5.5.5 -- 2026-04-27

Installer-only patch. No agent, integration, or config changes.

### Fixed
- **macOS: installer now removes Gatekeeper quarantine flag** -- files downloaded via curl get a `com.apple.quarantine` extended attribute that blocks execution with an "unidentified developer" dialog. The installer now strips this automatically. Previously, macOS users had to run `xattr -d` manually or navigate System Settings after install.
- **Installer interface picker labels updated** -- the network interface picker now correctly tags LXC/LXD bridges, Proxmox interfaces, Podman, HA OS supervisor bridges, and tunnel/VPN interfaces. Previously only Docker, ZeroTier, Tailscale, WireGuard, libvirt, VirtualBox, and VMware were labeled.

## v0.5.5.4 -- 2026-04-25

Agent-only patch. No integration, installer, or config changes.

Fixes the mDNS agent advertising an unreachable container bridge IP on QNAP (and similar platforms), reported by @gbravery in [#19](https://github.com/DAB-LABS/smart-sniffer/issues/19).

### Fixed
- **mDNS interface filter expanded from 10 to 51 prefixes** -- the auto-filter now covers LXC/LXD bridges, Kubernetes CNI (Flannel, Calico, Cilium, Weave), Proxmox firewall interfaces, macOS system interfaces, Podman, Open vSwitch, and more. Previously, QNAP's LXC bridge (`lxcbr0`) slipped through and the agent advertised its container IP instead of the LAN IP. Dangerous/ambiguous prefixes (`bond`, bare `br`, `vlan`, `qvs`) are intentionally excluded since they can be primary LAN interfaces on NAS platforms.
- **IP scoring now prefers 192.168.x over 10.x** -- when multiple candidate IPs survive filtering, `192.168.x` (almost always a home LAN) now scores higher than `10.x` (commonly used for containers, VPNs, and internal networks). Previously they scored equally and enumeration order broke the tie, which often picked the wrong one.

## v0.5.5.3 -- 2026-04-25

Agent-only patch. No integration, installer, or config changes.

Fixes drives being silently dropped when sleeping on agent startup, reported by @rolandg-reflow in [#18](https://github.com/DAB-LABS/smart-sniffer/issues/18).

### Fixed
- **Drives no longer vanish when sleeping on startup** -- the agent's first poll after starting now wakes all drives to collect a full SMART baseline (serial, model, attributes). This ensures every drive is registered with its stable serial-based ID immediately. Subsequent polls honor `standby_mode` normally -- sleeping drives are skipped and cached data is served. Previously, a drive that was asleep on the first poll was silently dropped from the API because there was no cached data to serve.

## v0.5.5.2 -- 2026-04-25

Agent-only patch. No integration, installer, or config changes.

Fixes the SAT fallback not triggering on QNAP hardware reported by @gbravery in [#16](https://github.com/DAB-LABS/smart-sniffer/issues/16).

### Fixed
- **SAT fallback now triggers on all smartctl execution failures** -- the fallback previously only activated when smartctl reported "device open failed" (exit code bit 1). On QNAP, drives open fine under SCSI but return "command failed" (exit code bit 2) because the SMART commands don't work over the SCSI translation layer. The agent now retries with SAT on any of the three execution failure bits, covering both the Synology and QNAP failure modes.
- **`--discover` no longer reports false positives** -- the diagnostic tool previously reported "SMART data: Yes" for drives that returned execution errors other than "device open failed". It now correctly flags any execution failure as a problem and attempts SAT fallback.

## v0.5.5.1 -- 2026-04-24

Agent-only patch. No integration, installer, or config changes.

Addresses the PATH resolution issue reported by @EagleDTW in [#17](https://github.com/DAB-LABS/smart-sniffer/issues/17) -- Synology DSM (and other NAS platforms) ship an outdated smartctl in `/usr/bin` that shadows newer versions installed via package managers.

### Fixed
- **smartctl path auto-resolution** -- the agent no longer relies solely on `PATH` to find `smartctl`. If the version in `PATH` is missing or older than 7.0, the agent searches 13 known platform-specific install locations (SynoCommunity, Entware, Homebrew, MacPorts, NixOS, Unraid NerdTools, QNAP QPKG, standard Linux/BSD paths). The first 7.0+ binary found is used automatically. Zero new flags, zero config -- the agent finds the right binary itself. Logged on startup: `using smartctl: /path/to/smartctl (version X.Y)`.

## v0.5.5 -- 2026-04-24

Agent-only release. No integration or installer changes required.

Addresses issues [#16](https://github.com/DAB-LABS/smart-sniffer/issues/16) (QNAP SATA drives misreported as SCSI) and [#17](https://github.com/DAB-LABS/smart-sniffer/issues/17) (Synology `/dev/sata*` paths not discovered by scan).

### Added
- **NAS protocol auto-detection on startup** -- the agent now uses `smartctl --scan-open` on its first scan cycle instead of `--scan`. This opens device handles and gives more accurate protocol detection, which fixes QNAP HBAs that report SATA drives as SCSI. Subsequent cycles revert to regular `--scan` to avoid waking sleeping drives.
- **SAT fallback for misreported SCSI drives** -- if a drive is reported as SCSI but smartctl cannot read it, the agent automatically retries with `-d sat` (SCSI-to-ATA Translation). If that works, the agent uses SAT for that drive going forward and logs it once. No config required -- QNAP users affected by #16 should see this pick up their drives automatically.
- **`device_overrides` config option** -- for drives that need an explicit protocol the agent cannot auto-detect (Synology `/dev/sata*` paths, RAID controllers with custom `-d` syntax), you can now list them in `config.yaml`. Overridden devices are treated as first-class drives even if they are not found by `--scan`.
- **`--discover` diagnostic flag** -- run `smartha-agent --discover` to probe every drive, test protocol detection, check SAT fallback, and print a clear summary of what the agent will see at runtime. On Synology, it also probes `/dev/sata1` through `/dev/sata8`. If any drives need `device_overrides`, it offers to write the config for you. Useful for support: paste the output into a GitHub issue and we can diagnose protocol problems without asking for manual smartctl runs.

### Upgrade Notes
- **Agent update only.** Replace the binary or re-run the installer. No integration update, config migration, or installer re-run is required.
- **QNAP users affected by #16:** update the agent -- no config change needed. The SAT fallback handles this automatically.
- **Synology users affected by #17:** update the agent and run `smartha-agent --discover` to generate the `device_overrides` block for your config. Restart the agent after writing the config.

## v0.5.4 -- 2026-04-21

Integration-only release. No agent changes required.

### Added
- **Drive Standby binary sensor** -- each drive now has a dedicated Standby entity that reports on when the drive is spun down and off when it is active. When on, the sensor exposes a `data_as_of` attribute showing when the cached SMART readings were last refreshed. Makes it straightforward to automate on standby state directly rather than reading an attribute off the Health sensor.
- **Agent OS diagnostic sensor** -- the agent device now exposes an OS entity reporting the agent host as linux, darwin, or windows. Visible by default; useful for at-a-glance inventory across mixed deployments.

### Changed
- **Scan Interval renamed to HA Poll Interval** -- the existing agent diagnostic entity has been renamed to clarify that it represents how often Home Assistant polls the agent, not how often the agent itself reads SMART data from drives. The underlying entity is preserved, so existing automations and templates referencing the entity ID continue to work.

### Deprecated
- **`binary_sensor.*_health` attributes `in_standby` and `data_as_of`** -- superseded by the new `binary_sensor.*_standby` entity (state and `data_as_of` attribute). The attributes remain on the Health sensor in this release for backward compatibility with v0.5.3 automations and will be removed in a future release. Tracked in `docs/internal/process/deprecations.md`.

### Upgrade Notes
- **Integration update only.** Update via HACS and reload. No agent update, installer re-run, or configuration change is required.

## v0.5.3 -- 2026-04-19

### Fixed
- **Noisy agent logs from drives with error history** -- drives with old error log entries (common on aged HDDs) caused the agent to log a warning every scan cycle, roughly 1,440 lines per day per drive. The agent now decodes the smartctl exit code properly: actual failures still warn, but informational flags about drive history are logged once and then suppressed.
- **OS detection on Windows agents** -- the health endpoint reported "unknown" for the OS field on Windows because it relied on a Unix-only command. Now uses Go's built-in platform detection and correctly reports "windows", "linux", "darwin", etc.

### Added
- **Standby-aware polling for NAS users** -- new `standby_mode` config option prevents the agent from waking sleeping HDDs during scans. When a drive is in standby, the agent serves cached data instead of spinning the disk up. The installer detects spinning drives and offers to enable this automatically. Keeps your NAS quiet and your drives resting when they should be.
- **Agent connectivity sensor in Home Assistant** -- a new binary sensor per agent shows Connected or Disconnected in real time. Unlike drive sensors that go "Unavailable" when the agent is offline, this sensor stays available and explicitly shows the connection state, making it easy to build automations around agent outages.
- **Agent diagnostic entities** -- version, last seen, IP, port, scan interval, and auth status are now available as entities under each agent device. Version is enabled by default; the rest are hidden by default and can be enabled in entity settings.
- **Minimum smartctl version check** -- the agent now verifies smartctl 7.0+ is installed before starting, with clear upgrade instructions if not. Older versions lack the JSON output the agent depends on, and previously failed silently.
- **OS and uptime in health endpoint** -- the agent's `/api/health` response now includes host OS and uptime, used by the new diagnostic entities.
- **Standby indicators on drive sensors** -- when a drive is sleeping and being served from cache, its sensors gain `in_standby` and `data_as_of` attributes so you can tell the data is stale and how old it is. *(Deprecated in v0.5.4 -- replaced by a dedicated Standby binary sensor. The v0.5.3 attributes remain for backward compatibility.)*

### Upgrade Notes
- **Agent update required.** Re-run the installer or replace the binary to get the new features. Existing configs work without changes -- all new options default to current behavior.
- **Integration update required.** Update via HACS and reload to pick up the new connectivity sensor and diagnostic entities.
- **NAS users with spinning drives:** after updating the agent, re-run the installer to enable standby-aware polling, or manually add `standby_mode: standby` to your config.yaml.

## v0.5.2.1 -- 2026-04-16

### Fixed
- **macOS/BSD: installer prints grep errors during agent summary** -- three uses of `grep -P` (Perl regex) in install.sh are not supported on macOS or BSD systems, which ship BSD grep. Replaced with portable `grep -oE` and `sed` equivalents. The drive name display in the post-install summary was the visible failure; two others in the interface picker were latent (only reachable on Linux with busybox grep).

## v0.5.2 -- 2026-04-16

### Fixed
- **macOS: filesystem picker writes "on" instead of actual mountpoint** -- the install.sh parser assumed Linux `/proc/mounts` field positions, but macOS `mount` output uses a different format (`/dev/disk3s1s1 on / (apfs, ...)`). Field $2 is "on", not the mountpoint. Now detects macOS via `$OSTYPE` and parses correctly with sed. Also filters macOS virtual volumes (Preboot, Recovery, VM, xarts, iSCPreboot, Hardware, devfs, autofs) and uses `df -k` instead of the GNU-only `df -B1`. (HA Forum Post #43, reported by spry-salt)
- **Command Timeout false positives on USB drives** -- SMART attribute 188 (Command_Timeout) was triggering MAYBE on any non-zero decoded value. Low counts (1-100) are normal from USB sleep/wake cycles, SATA power management (ALPM), and NCQ reordering. Backblaze data shows 84% of drives accumulate non-zero Command_Timeout over their lifetime -- the correlation is with elevated counts, not merely non-zero. Threshold raised from >0 to >100. (HA Forum Post #43, reported by spry-salt)
- **Windows: winget source picker appears during smartmontools install** -- added `--disable-interactivity --source winget` to suppress the interactive source selection prompt.

### Added
- **Windows: interface picker in installer** -- multi-homed Windows hosts can now select the mDNS advertise interface during install, matching the Unix installer. Virtual adapters (Hyper-V, TAP, Tailscale, WireGuard, WSL, Loopback) are labeled and shown separately. Single-adapter machines auto-select without prompting.
- **Windows: filesystem picker in installer** -- Windows installer now offers disk usage monitoring setup, matching the Unix installer. Enumerates fixed volumes via Get-Volume with human-readable sizes and percent used. Supports comma-separated selection, "all", or "none".
- **Windows: upgrade-aware field migration** -- upgrading from older configs now detects missing `advertise_interface` (prompts only on multi-NIC) and `filesystems` fields, offering to add them. Append-only -- existing config is never rewritten.
- **Windows: real filesystem monitoring in agent** -- replaced the stub `filesystem_windows.go` with a real implementation using Win32 `GetDiskFreeSpaceEx`. Windows agents with filesystems configured now report actual disk usage data via `/api/filesystems`.

### Security
- **Agent: timing-safe token comparison** -- bearer token authentication now uses a constant-time comparison that is resistant to timing side-channel attacks. The previous implementation leaked information about the token with every failed request, making it theoretically possible to recover the full token one character at a time. The new approach reveals nothing about the token regardless of the input, matching the standard used by web frameworks and authentication libraries across the industry.

### Upgrade Notes
- **macOS users seeing "statfs on failed":** this release fixes the installer. Re-run the installer to regenerate config.yaml with correct mountpoints, then restart the agent.
- **USB drive users seeing false MAYBE alerts:** the Command Timeout threshold is now >100 instead of >0. Update the integration via HACS and reload to clear stale alerts.
- **Windows users:** significant update. Re-run the installer to get the interface picker, filesystem picker, and upgrade-aware field migration. The agent now supports real disk usage monitoring -- the installer will offer to set it up.

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
