# Changelog

All notable changes to the SMART Sniffer Card.

## [1.0.16] - 2026-05-02

### Changed

- Agent section header name (BROOKDALE, KALI-VM, etc.) bumped from 10px to 11px so the agent name reads more prominently as a section divider while still staying within the type scale.
- Unsupported chip metric copy changed from "no data" to "Unsupported" so it matches the integration's `STATE_UNSUPPORTED` enum and tells the user *why* there's no data (the drive doesn't expose readable SMART) rather than implying transient absence. Stats strip umbrella label "No data" remains, since that bucket combines unsupported and stale drives.

## [1.0.15] - 2026-05-02

### Changed

- Card body in light mode now carries a faint HA-blue tint (`rgba(65, 189, 245, 0.08)`) so the brand color shows on the dashboard surface itself, not just on the click-to-reveal detail panel. Chip and disk-tile backgrounds stay white so they pop as "cards on a tinted surface."
- Detail panel in light mode bumped to a stronger HA-blue tint (`rgba(65, 189, 245, 0.16)`) so it still reads as an elevated layer above the now-tinted card body.
- Dark mode unchanged. Card body stays at the standard dark surface (blue tints don't show on dark surfaces); detail panel keeps its `#2D353F` "unsupported tone" treatment.

## [1.0.14] - 2026-05-02

### Changed

- Light mode detail panel HA-blue tint bumped from 0.04 alpha to 0.08 alpha so the brand wash is actually visible (the previous value was below perception threshold against most light HA themes).
- Dark mode detail panel now uses `#2D353F` (the same color as the unsupported / stale stripe). The panel and the "no signal" chips share a tone; the dark palette stays coherent without introducing a new surface color.
- Dark mode healthy stripe dimmed from `#9099A4` to `#5A6678`. Still has the subtle HA-blue cast, still distinctly above the unsupported `#2D353F`, but quieter against the dark surface so the chip's data leads instead of the stripe.

## [1.0.13] - 2026-05-02

### Changed

- Detail panel polished. Metric tile corner radius now matches drive chips and disk tiles (`var(--ss-radius-chip)`, was 6px). Visual rhythm across the card.
- Metric tile severity now shows up as a colored 1px stroke around the tile (amber for warn, red for critical) rather than colored value text. Default tiles get a neutral grey stroke. The label and value text stay neutral so the tile reads as "this metric is the reason," not "this metric is celebrating."
- S.M.A.R.T. Status `PASSED` now renders neutral instead of green. Color is reserved for problems; the absence of color says "no concern here." `FAILED` still renders with a red stroke.
- Detail panel background. Light mode uses a faint HA-blue tint (`rgba(65, 189, 245, 0.04)`) so the panel reads as part of the brand chrome family. Dark mode uses a slightly-elevated grey (`#252A33`) instead, since blue tints disappear against very dark surfaces; the elevation cue is the dark-interface convention for "this layer sits above."

## [1.0.12] - 2026-05-02

### Added

- The card now reads `hass.themes.darkMode` to detect when Home Assistant's actual theme is dark, and applies an `is-ha-dark` class on the card host. CSS targets that class instead of the OS-level `prefers-color-scheme` media query, so the card respects HA's own theme decision (which itself respects the user's "Auto" setting if they chose it). This is the single source of truth for theme state.
- Dark-mode stripe values use subtle HA-blue-tinted greys (`#9099A4` healthy, `#2D353F` unidentified) so they harmonize with the brand chrome (header magnifier, agent section labels) on dark surfaces. Light mode keeps pure greys.

### Fixed

- Stripe relationship is no longer inverted on Mac users with their OS in dark mode but HA in a light theme, or vice-versa. The fix from 1.0.11 (single set of values for all themes) is replaced with proper theme-aware tokens that flip when HA actually changes theme.

## [1.0.11] - 2026-05-02

### Fixed

- The chip stripe colors were rendering inverted on Mac users with their OS in dark mode but HA in a light theme. Root cause: the `@media (prefers-color-scheme: dark)` query fires on OS-level preference, not HA's actual theme, so a light-themed HA on a dark-OS Mac was getting dark-theme stripe values, which inverted the visual relationship (dark stripe = unsupported instead of healthy). Fix: removed the dark-mode stripe overrides. The light-theme values (`#9E9E9E` healthy, `#D6D6D6` unidentified) have enough contrast on both light and dark HA surfaces and now render consistently regardless of OS preference.

## [1.0.10] - 2026-05-02

### Changed

- Unsupported / stale chip stripe restored to a real grey (`#D6D6D6` light, `#44494F` dark) instead of fully transparent. The transparent variant in 1.0.9 was too quiet -- the user wanted the chip to show "something less than healthy but more than nothing." Stripe is now visible enough to register as a stripe, but distinctly less assertive than the medium grey used for healthy.
- Stale disk tile bar fill returns to using the unidentified-stripe token (now that the token is no longer transparent), keeping disk and chip stripe semantics in sync.

## [1.0.9] - 2026-05-02

### Changed

- The unidentified / stale chip stripe is now `transparent` instead of a very-light grey. Chips for unsupported drives, drives on offline agents, and drives with empty names now have NO visible left stripe at all. The chip's outer border gives the card its shape; the stripe color is now used purely as a "data exists" indicator. Healthy drives keep their clearly-visible medium-grey stripe. The semantic story is now simply: stripe = signal, no stripe = no signal.
- Stale disk tiles use `--ss-ink-4` for the bar fill instead of the stripe token (which is now transparent and would make the bar disappear).

## [1.0.8] - 2026-05-02

### Changed

- Agent section headers (BROOKDALE, KALI-VM, etc.) now render in brand blue (`#41BDF5`) with slightly looser letter-spacing. Headers read clearly as section dividers instead of getting lost between the larger drive-name text below.
- Pushed the healthy and unidentified stripe greys further apart for clearer visual signal at 3px stripe width. Healthy is now darker (`#9E9E9E` light theme, `#707782` dark) so it's unambiguously present. Unidentified is now much lighter (`#ECECEC` light, `#2D3138` dark) so it nearly disappears into the chip surface, reinforcing "no signal here" semantically.

## [1.0.7] - 2026-05-02

### Changed

- Disk tiles now sit on the same background as drive chips (transparent on the card surface) instead of a muted secondary surface. Visually consistent with the chips above them.
- Disk usage bar fill is now greyscale at all usage levels. Severity (warn / critical) shows up only in the percentage number's color. Keeps the card visually quiet and reserves color for moments that truly demand the eye. Stale (agent-offline) disks still use the lighter unidentified-grey for the bar fill so the "no current reading" signal carries.

## [1.0.6] - 2026-05-02

### Changed

- Disk usage now renders as full-width tiles inside each agent's group, directly below that agent's drive chips, instead of as a standalone section at the bottom of the card. Each agent's section is now a single visual unit covering both drive health and disk usage for that host.
- Disk tiles are visually distinguished from drive chips: no severity stripe (disks aren't part of the integration's Attention aggregate), a small leading disk glyph instead of a status dot, and a lower-key surface so they read as a different data type.
- Mountpoint paths now truncate from the LEFT when too long to fit, so the recognizable tail of the path stays visible (`/var/lib/casaos_data/data` truncates as `…aos_data/data` rather than `/var/lib/casao…`).
- Filesystem-only agents (a host that ships disk usage but no SMART data) get a clean rendering: agent header summary reads "disks only" and the tiles render below.
- Agent header summary still counts drives only. Disk tile coloring carries the disk-side severity signal independently.

### Removed

- Standalone "Disk Usage" section at the bottom of the card. The `show_storage` config option still controls whether disk tiles render at all; only their location moved.

## [1.0.5] - 2026-05-02

### Changed

- Disk Usage section: each agent's mountpoints are now wrapped in a visual group block so the "Disk Usage · agentname" label clearly belongs to the rows beneath it. Tightened label-to-bar spacing, indented the rows under their label, and added a thin divider under each label. Multi-mountpoint groups (e.g., an agent with both Root and /mnt/data) now read as one cohesive group.

## [1.0.4] - 2026-05-02

### Changed

- Bumped contrast between healthy stripe (`#B8B8B8`, standard grey) and unidentified stripe (`#E8E8E8`, very light grey) so unsupported and stale drives clearly retreat into the chip background while healthy drives remain present and intentional.
- Added an explicit `.ss-chip.is-unsupported::before` CSS rule rather than relying on the default `::before` rule, so the styling is unambiguous in source.
- Disk Usage section now follows the same agent ordering as the drives section (severity-first, then case-insensitive alphabetical). Agents that appear only in storage (no drives, just filesystem entities) are appended at the end. Both halves of the card now render the same hierarchy.

## [1.0.3] - 2026-05-02

### Fixed

- Healthy and unidentified chip stripes were rendering darker than designed because the chip's outer border (which defers to HA's `--divider-color`) was visually merging with the stripe. Removed the chip's left border entirely so the stripe is the only thing on the chip's left edge. Light grey stripes now read as light grey.

### Removed

- Redundant "Drives / X shown" section header inside the drives area. The card title and stats strip already convey the same information, and the per-agent group headers (added in 1.0.1) provide local context. When `show_ok: false` filters drives out, a small "X of Y shown · healthy hidden" hint replaces the header so the user still knows drives are being filtered.

## [1.0.2] - 2026-05-02

### Fixed

- Agent name resolution: every chip was rendering the literal "on agent" instead of the agent's hostname. Root cause was reading from `hass.config_entries`, which is not reliably exposed to custom cards via the browser-side hass object. The card now extracts the agent name from the agent device's own name field ("SMART Sniffer (hostname)" → "hostname"), which is always available. A fallback to a short slice of the config-entry ID is shown when the agent device cannot be resolved at all, so chips never collapse to identical labels.

## [1.0.1] - 2026-05-02

### Changed

- The "on <agent>" context label now always appears on every chip, rather than only when more than one agent is configured. Single-agent users see consistent chip layouts; multi-agent users get the disambiguator they need.
- Drive grid is now grouped by agent. Each agent gets its own section header that summarizes the agent's drive states (e.g. "1 critical" / "3 healthy"). Within each agent's section, drives still sort worst-first.
- Agent sections are sorted by their worst drive's severity, then alphabetically. An agent with a critical drive floats above an agent with all-healthy drives, even if it would alphabetize later. Problems still surface to the top of the card without losing per-host grouping.

## [1.0.0] - 2026-05-02

Initial release.

The SMART Sniffer Card is a Lovelace dashboard card for the SMART Sniffer integration. It surfaces drive health at a glance, flags drives that need attention with plain-English explanations, and provides click-through diagnostics.

### Features

- One chip per drive with severity dot, name, temperature, and a plain-English reasons line for any drive in a watch or critical state.
- Stats strip totals: Drives, Healthy, Watch, Critical, No data. Math sums to total drives.
- Alert banner when any drive is in a watch or critical state.
- Click-through detail view with full diagnostics, split into Common and protocol-specific sections (ATA or NVMe).
- Disk Usage section, read-only, with configurable warn / critical thresholds. Hidden when no filesystem entities exist.
- Multi-agent support: when more than one agent is configured, each chip shows which agent the drive lives on.
- Stale-data treatments for drives in standby and drives on offline agents.
- Light and dark theme parity, driven by Home Assistant's CSS theme variables.
- Visual config editor with drive and agent filtering.

### Acknowledgements

The original Lovelace card prototype was contributed by [@bangadrum](https://github.com/bangadrum) in PR #4 against `smart-sniffer-app`. This v1 card is a fresh implementation that retains the prototype's core architectural choices (shadow DOM, regex-based entity classification, visual config editor) and rebuilds the visual layer. Thanks for the head start.
