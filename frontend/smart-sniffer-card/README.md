# SMART Sniffer Card

Drive health dashboard card for the [SMART Sniffer integration](https://github.com/DAB-LABS/smart-sniffer). Lives on a Lovelace dashboard. Surfaces every drive's status at a glance, flags drives that need attention with plain-English explanations, and provides click-through diagnostics.

## What it shows

- One chip per drive: name, status dot, temperature, the host agent, and (for drives that need attention) a one-line explanation.
- Stats strip with the Drives / Healthy / Watch / Critical / No data totals. Math always sums.
- Alert banner when any drive is in a Watch or Critical state.
- Click-through detail view with full diagnostics, split into Common and protocol-specific sections (ATA or NVMe).
- Optional disk usage tiles inside each agent's group (read-only, no alerts) when filesystem monitoring entities are present.
- Drives grouped by host agent, with each agent's section showing a per-host severity summary. Agents with problem drives float above agents with all-healthy drives.
- Stale-data treatments for drives in standby and drives on offline agents.

## Install

1. Download `smart-sniffer-card.js` and copy it to `/config/www/` on your Home Assistant server.

2. Register it as a Lovelace resource. **Settings -> Dashboards -> Resources -> + Add Resource**:
   - **URL:** `/local/smart-sniffer-card.js`
   - **Resource type:** JavaScript module

3. Hard-refresh your browser (`Ctrl+Shift+R` on Windows / Linux, `Cmd+Shift+R` on Mac). Home Assistant aggressively caches custom card JS, so a normal refresh is not enough.

4. On any dashboard, edit it and add a new card. Search for "SMART Sniffer Card" and add it.

The card uses the SMART Sniffer integration's existing entities. You do not need to configure any entity IDs by hand.

## Configuration

Most users can drop the card on a dashboard with no configuration. The defaults are sensible.

### YAML

```yaml
type: custom:smart-sniffer-card
title: Drive Health
columns: 2
show_ok: true
```

### Full config schema

| Option | Type | Default | What it does |
|---|---|---|---|
| `title` | string | `"Drive Health"` | The card's title in the header. |
| `columns` | int (1-4) | `2` | Drives per row on wide viewports. Forced to 1 below 600px wide. |
| `show_ok` | bool | `true` | When `false`, hides healthy and standby drives. Watch / critical / unsupported / stale always render. |
| `drives` | list of device IDs | `[]` | When non-empty, only the listed drives appear. Use the visual editor's "Filter Drives" picker to select them. |
| `agents` | list of config-entry IDs | `[]` | When non-empty, only drives from the listed agents appear. Visible in the editor when more than one agent is configured. |
| `usage_warn` | int (0-100) | `90` | Disk Usage section: percentage at which a mountpoint's bar turns amber. |
| `usage_crit` | int (0-100) | `95` | Disk Usage section: percentage at which a mountpoint's bar turns red. |
| `show_storage` | bool | `true` | When `false`, hides the Disk Usage section even if filesystem entities exist. |

### Visual editor

The card has a built-in visual editor that Lovelace exposes when you click the card and choose "Edit". You can change every option above without writing YAML, including ticking individual drives or agents to include.

## Examples

See the `examples/` directory:

- `basic.yaml`: minimal card with defaults.
- `multi-agent.yaml`: filter to one specific agent.
- `filtered.yaml`: hide healthy drives, show only the ones that need attention.

## Severity logic

The card never invents severity thresholds. It reads the integration's `Attention Needed` enum sensor (`NO` / `MAYBE` / `YES` / `UNSUPPORTED`) and the matching `Attention Reasons` text sensor. If you want to change what counts as critical or watch, change `attention.py` in the integration. The card will follow.

The visible severity ramp:

- **Healthy:** small green dot, lighter grey stripe. Quiet.
- **Watch:** amber dot, amber stripe, amber count in the stats strip. Alert banner if any drive is in this state and none is critical.
- **Critical:** red dot, red stripe, red count. Critical alert banner takes precedence over watch when both are present.
- **No data / Unsupported / Stale:** hollow grey dot, lighter grey stripe.

The brand color HA blue (`#41BDF5`) appears only as chrome (the magnifier mark, focus rings, the active-chip outline). It is never used to signal severity.

## Stale data

The card recognizes two stale-data conditions and treats them differently:

- **Drive in standby (`Standby` sensor `on`):** the integration is serving cached SMART data because the drive is spun down. The chip keeps the drive's true severity color (so you know whether to worry on next wake), but the temperature is replaced with `cached 3h 02m` and the subline appends `· in standby`.
- **Agent offline (`Agent Status` connectivity sensor `off`):** the agent has stopped reporting. Every drive on that agent goes to a stale grey state regardless of last-known severity, and shows `agent offline · last seen Xh ago`. The data is no longer trustworthy, so we don't display it as if it were.

## Disk usage tiles

When the integration has filesystem entities (which exist when an agent is running with mount-point monitoring configured), each agent's group includes one full-width disk usage tile per mountpoint, rendered below that agent's drive chips. The tiles are read-only. They show a usage bar colored at the `usage_warn` and `usage_crit` thresholds. They do NOT contribute to the alert counts at the top of the card and do NOT trigger the alert banner. If you want disk-usage alerts, build automations from the existing `Disk Usage (mountpoint)` sensors directly in HA.

Filesystem-only agents (a host that ships disk usage but no SMART data) render with the agent header summary reading "disks only" and the tiles directly below.

## Theme support

The card defers to your Home Assistant theme via CSS variables (`--primary-text-color`, `--success-color`, `--warning-color`, `--error-color`, etc.). It works on light and dark themes out of the box. The brand blue is hardcoded; everything else inherits.

## Troubleshooting

**The card doesn't appear in the "add card" search.**

Check that you registered the resource (Settings -> Dashboards -> Resources) AND hard-refreshed your browser. The console should log `SMART-SNIFFER-CARD v1.0.0` on a fresh page load.

**The card renders "No drives yet" but I have drives in the integration.**

Either the integration's first poll has not completed yet (give it a minute), or the entities have been disabled. Open Settings -> Devices & Services -> SMART Sniffer to check that drives are listed and entities are enabled.

**One of my drives shows as "Unidentified drive".**

The drive's HA device record has no model or name. This usually happens with USB enclosures that don't pass through enough drive identification. The chip's reasons line and detail view will still show whatever the integration learned about it.

**A chip's stripe is grey instead of red even though the drive has a problem.**

Check that the drive's agent is online (Settings -> Devices & Services -> SMART Sniffer -> agent device -> Agent Status). Agent-offline outranks attention. If the agent is offline, the drive shows stale grey, because we cannot trust any current reading.

**The card shows old data after I updated.**

Hard-refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`). HA caches custom card JavaScript aggressively.

## Credits

The original Lovelace card prototype was contributed by [@bangadrum](https://github.com/bangadrum) in PR #4 against `smart-sniffer-app`. The redesigned v1 card in this folder is a fresh implementation that retains his core architectural choices (shadow DOM, regex-based entity classification, visual config editor) and rebuilds the visual layer. Thanks to bangadrum for the head start.

## License

Same as the parent SMART Sniffer project.
