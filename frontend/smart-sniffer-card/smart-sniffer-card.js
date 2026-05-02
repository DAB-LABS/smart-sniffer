/**
 * SMART Sniffer Card for Home Assistant
 * Drive health dashboard for the SMART Sniffer integration.
 *
 *   Repo:    https://github.com/DAB-LABS/smart-sniffer
 *   Install: copy this file to /config/www/smart-sniffer-card.js, then add
 *            it as a Lovelace resource (Settings -> Dashboards -> Resources)
 *            with URL /local/smart-sniffer-card.js, Type JavaScript module.
 *   Use:     type: custom:smart-sniffer-card
 *
 * Acknowledgements
 *   Original Lovelace card prototype: @bangadrum (PR #4 on smart-sniffer-app).
 *   The shadow-DOM architecture, regex-based entity classification, and
 *   visual config editor pattern carried over from that prototype. Thank you.
 */

const VERSION = "1.0.16";
const DOMAIN  = "smart_sniffer";

/* ─── Entity-id suffix patterns ─────────────────────────────────────────────
 * Each pattern matches the trailing suffix of an entity_id produced by the
 * SMART Sniffer integration. Order matters only when patterns can overlap;
 * we keep the more specific patterns first.                                 */
const ENTITY_PATTERNS = {
  attention_needed:           /_attention_needed$/,
  attention_reasons:          /_attention_reasons$/,
  health:                     /_health$/,
  standby:                    /_standby$/,
  temperature:                /_temperature$/,
  power_on_hours:             /_power_on_hours$/,
  power_cycle_count:          /_power_cycle_count$/,
  smart_status:               /_smart_status$/,
  reallocated_sector_count:   /_reallocated_sector_count$/,
  current_pending_sectors:    /_current_pending_sector_count$/,
  reallocated_event_count:    /_reallocated_event_count$/,
  spin_retry_count:           /_spin_retry_count$/,
  command_timeout:            /_command_timeout$/,
  reported_uncorrectable:     /_reported_uncorrectable_errors$/,
  // wear_percent: covers both the current key (_wear_leveling_count) and the
  // future-proofing alternative (_percentage_used). Don't simplify this.
  wear_percent:               /_(wear_leveling_count|percentage_used)$/,
  available_spare_threshold:  /_available_spare_threshold$/,
  available_spare:            /_available_spare$/,
  critical_warning:           /_critical_warning$/,
  media_errors:               /_media_errors$/,
};

/* Devices we explicitly skip during drive discovery (model strings). */
const NON_DRIVE_DEVICE_MODELS = new Set([
  "Filesystem Monitor",
  "Agent",
]);

/* Sort priority for chip ordering (lower = appears first). */
const SORT_ORDER = {
  critical:    0,
  watch:       1,
  unsupported: 2,
  stale:       3,
  cached:      4,
  healthy:     5,
};

/* ─── Styles ──────────────────────────────────────────────────────────────── */
const STYLES = `
:host {
  /* Brand. Hardcoded, NOT theme-overridable. */
  --ss-brand-blue:        #41BDF5;
  --ss-brand-blue-tint:   rgba(65, 189, 245, 0.10);
  --ss-brand-blue-ring:   rgba(65, 189, 245, 0.40);

  /* Severity. Defer to HA theme variables when present. */
  --ss-ok:                var(--success-color, #2E7D32);
  --ss-warn:              var(--warning-color, #C77A00);
  --ss-crit:              var(--error-color,   #C5302C);
  --ss-unknown:           #9B9B9B;
  --ss-warn-tint:         rgba(199, 122, 0, 0.10);
  --ss-crit-tint:         rgba(197, 48, 44, 0.10);

  /* Stripe semantics:
     Healthy = clearly visible medium grey at 3px. The drive has data.
     Unidentified = visible but distinctly quieter than healthy. The drive
     is "in scope" but reporting nothing useful. The chip's outer border
     gives each card its shape; the stripes provide the at-a-glance signal
     of whether there's anything to read. */
  --ss-stripe-healthy:      #9E9E9E;
  --ss-stripe-unidentified: #D6D6D6;

  /* Ink scale. Defer to HA. */
  --ss-ink-1:             var(--primary-text-color,   #1A1A1A);
  --ss-ink-2:             var(--primary-text-color,   #2C2C2C);
  --ss-ink-3:             var(--secondary-text-color, #6B6B6B);
  --ss-ink-4:             #9B9B9B;
  --ss-ink-5:             var(--divider-color,        rgba(0,0,0,0.10));

  /* Surfaces. Defer to HA. */
  --ss-bg:                var(--card-background-color,      #FFFFFF);
  --ss-surface-1:         var(--secondary-background-color, #FAFAFA);
  --ss-surface-hover:     rgba(0, 0, 0, 0.03);

  /* Card body and detail panel surfaces, light mode.
     The card body itself takes a faint HA-blue tint so the brand color
     shows on the dashboard, not just on click-to-reveal surfaces. Chip
     and disk-tile backgrounds stay white (var(--ss-bg)) so they pop as
     "cards on a tinted surface". The detail panel takes a stronger tint
     so it reads as an elevated layer above the already-tinted card body. */
  --ss-card-bg:           rgba(65, 189, 245, 0.08);
  --ss-detail-bg:         rgba(65, 189, 245, 0.16);

  /* Type. */
  --ss-font:              var(--ha-font-family, 'Roboto', 'Noto Sans', sans-serif);
  --ss-fs-display:        22px;
  --ss-fs-title:          15px;
  --ss-fs-body:           13px;
  --ss-fs-small:          11px;
  --ss-fs-micro:          10px;
  --ss-fw-regular:        400;
  --ss-fw-medium:         500;
  --ss-fw-semibold:       600;

  /* Spacing & shape. */
  --ss-pad-1: 4px; --ss-pad-2: 8px; --ss-pad-3: 12px;
  --ss-pad-4: 16px; --ss-pad-5: 20px; --ss-pad-6: 24px;
  --ss-radius-card: 12px;
  --ss-radius-chip: 8px;
  --ss-radius-pill: 4px;
  --ss-stripe-width: 3px;
  --ss-shadow-detail: 0 4px 16px rgba(0, 0, 0, 0.08);

  /* Motion. */
  --ss-duration-fast: 120ms;
  --ss-duration-medium: 180ms;
  --ss-easing-standard: cubic-bezier(0.2, 0, 0, 1);

  display: block;
  font-family: var(--ss-font);
}
/* HA dark mode. Activated by adding the .is-ha-dark class to the host
   element, which the card does in set hass() based on hass.themes.darkMode.
   This sidesteps the OS-level prefers-color-scheme media query, which
   previously fired on Mac dark-OS even when HAs actual theme was light. */
:host(.is-ha-dark) {
  --ss-ok:              var(--success-color, #4CAF50);
  --ss-warn:            var(--warning-color, #E0A82E);
  --ss-crit:            var(--error-color,   #E64A45);
  --ss-ink-4:           #6B6F75;
  /* Stripe greys with a subtle HA-blue cast so they harmonize with the
     brand chrome (header magnifier, agent section labels) on dark surfaces.
     Healthy is the light tone (visible against dark surface). Unidentified
     is the dark tone (retreats into dark surface). The blue cast is
     deliberately subtle: the stripes still read as grey, just warm
     toward HA blue rather than cold. */
  --ss-stripe-healthy:      #5A6678;
  --ss-stripe-unidentified: #2D353F;
  /* Dark mode card and detail surfaces. Card body keeps the standard dark
     surface (no blue tint, since blue tints disappear against dark). Detail
     panel matches the unsupported stripe color, so the panel and the
     "no signal" chips share a tone. Coherent dark palette. */
  --ss-card-bg:             var(--ss-bg);
  --ss-detail-bg:           #2D353F;
}
@media (prefers-reduced-motion: reduce) {
  :host { --ss-duration-fast: 0ms; --ss-duration-medium: 0ms; }
  .ss-skel { animation: none !important; }
}

* { box-sizing: border-box; margin: 0; padding: 0; }

.ss-card {
  background: var(--ss-card-bg);
  color: var(--ss-ink-1);
  font-size: var(--ss-fs-body);
  line-height: 1.5;
  border: 1px solid var(--ss-ink-5);
  border-radius: var(--ss-radius-card);
  overflow: hidden;
}

/* Header */
.ss-header {
  display: flex;
  align-items: center;
  gap: var(--ss-pad-3);
  padding: var(--ss-pad-3) var(--ss-pad-4);
  background: var(--ss-surface-1);
  border-bottom: 1px solid var(--ss-ink-5);
}
.ss-header-icon {
  width: 32px; height: 32px;
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.ss-header-icon svg { width: 100%; height: 100%; }
.ss-header-text { min-width: 0; flex: 1; }
.ss-header-title {
  font-size: var(--ss-fs-title);
  font-weight: var(--ss-fw-semibold);
  color: var(--ss-ink-1);
  letter-spacing: -0.01em;
  line-height: 1.2;
}
.ss-header-sub {
  font-size: var(--ss-fs-small);
  color: var(--ss-ink-3);
  margin-top: 2px;
}

/* Stats strip */
.ss-stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  border-bottom: 1px solid var(--ss-ink-5);
}
.ss-stat {
  padding: var(--ss-pad-3) var(--ss-pad-4);
  border-right: 1px solid var(--ss-ink-5);
}
.ss-stat:last-child { border-right: none; }
.ss-stat-val {
  font-size: var(--ss-fs-display);
  font-weight: var(--ss-fw-semibold);
  line-height: 1;
  letter-spacing: -0.02em;
  color: var(--ss-ink-1);
  margin-bottom: 4px;
  font-variant-numeric: tabular-nums;
}
.ss-stat-val.is-ok { color: var(--ss-ok); }
.ss-stat-val.is-warn { color: var(--ss-warn); }
.ss-stat-val.is-crit { color: var(--ss-crit); }
.ss-stat-val.is-unknown { color: var(--ss-unknown); }
.ss-stat-lbl {
  font-size: var(--ss-fs-micro);
  color: var(--ss-ink-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: var(--ss-fw-medium);
}
.ss-stats.is-condensed {
  display: block;
  padding: var(--ss-pad-3) var(--ss-pad-4);
  font-size: var(--ss-fs-body);
  line-height: 1.6;
  color: var(--ss-ink-3);
}
.ss-stats.is-condensed strong { color: var(--ss-ink-1); font-weight: var(--ss-fw-semibold); }
.ss-stats.is-condensed strong.is-ok { color: var(--ss-ok); }
.ss-stats.is-condensed strong.is-warn { color: var(--ss-warn); }
.ss-stats.is-condensed strong.is-crit { color: var(--ss-crit); }
.ss-stats.is-condensed strong.is-unknown { color: var(--ss-unknown); }

/* Alert banner */
.ss-banner {
  padding: var(--ss-pad-3) var(--ss-pad-4);
  border-bottom: 1px solid var(--ss-ink-5);
  border-left: var(--ss-stripe-width) solid;
  font-size: var(--ss-fs-body);
}
.ss-banner.is-crit { border-left-color: var(--ss-crit); background: var(--ss-crit-tint); }
.ss-banner.is-warn { border-left-color: var(--ss-warn); background: var(--ss-warn-tint); }
.ss-banner-headline { color: var(--ss-ink-1); font-weight: var(--ss-fw-semibold); }
.ss-banner-detail { color: var(--ss-ink-3); font-size: var(--ss-fs-small); margin-top: 2px; }

/* Drives section */
.ss-drives-section { padding: var(--ss-pad-4); }
/* Filter hint, only shown when show_ok is false and some drives are hidden. */
.ss-drives-hint {
  font-size: var(--ss-fs-micro);
  color: var(--ss-ink-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: var(--ss-pad-3);
  padding-bottom: var(--ss-pad-2);
  border-bottom: 1px solid var(--ss-ink-5);
}
.ss-drives-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--ss-pad-2); }
.ss-drives-grid.cols-1 { grid-template-columns: 1fr; }
.ss-drives-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
.ss-drives-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }

/* Agent section header inside the drives grid. Spans full grid width. The
   agent name uses brand blue so the header reads unambiguously as a section
   divider (matches the brand discipline: blue is for chrome accents only). */
.ss-agent-header {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ss-pad-2);
  padding: var(--ss-pad-3) 0 var(--ss-pad-1) 0;
  font-size: var(--ss-fs-micro);
  text-transform: uppercase;
  letter-spacing: 0.10em;
  font-weight: var(--ss-fw-semibold);
  color: var(--ss-ink-3);
  border-bottom: 1px solid var(--ss-ink-5);
  margin-bottom: var(--ss-pad-1);
}
.ss-agent-header:first-child { padding-top: 0; }
.ss-agent-header-name {
  color: var(--ss-brand-blue);
  letter-spacing: 0.12em;
  font-size: var(--ss-fs-small);
}
.ss-agent-header-summary {
  font-size: var(--ss-fs-micro);
  text-transform: none;
  letter-spacing: 0;
  color: var(--ss-ink-3);
  font-weight: var(--ss-fw-medium);
}
.ss-agent-header-summary .has-crit { color: var(--ss-crit); font-weight: var(--ss-fw-semibold); }
.ss-agent-header-summary .has-warn { color: var(--ss-warn); font-weight: var(--ss-fw-semibold); }

/* Drive chip */
.ss-chip {
  position: relative;
  background: var(--ss-bg);
  border: 1px solid var(--ss-ink-5);
  border-left: none;  /* the ::before stripe replaces the left border so the
                         stripe color is the only thing the eye sees on the
                         left edge, not stripe-on-top-of-darker-border. */
  border-radius: var(--ss-radius-chip);
  padding: var(--ss-pad-3) var(--ss-pad-3) var(--ss-pad-3) calc(var(--ss-pad-3) + var(--ss-stripe-width) + 4px);
  cursor: pointer;
  user-select: none;
  overflow: hidden;
  transition: background var(--ss-duration-fast) var(--ss-easing-standard),
              border-color var(--ss-duration-fast) var(--ss-easing-standard);
}
.ss-chip::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: var(--ss-stripe-width);
  background: var(--ss-stripe-unidentified);
  border-top-left-radius: var(--ss-radius-chip);
  border-bottom-left-radius: var(--ss-radius-chip);
}
.ss-chip.is-ok::before          { background: var(--ss-stripe-healthy); }
.ss-chip.is-warn::before        { background: var(--ss-warn); }
.ss-chip.is-crit::before        { background: var(--ss-crit); }
.ss-chip.is-unsupported::before { background: var(--ss-stripe-unidentified); }
.ss-chip.is-stale::before       { background: var(--ss-stripe-unidentified); }
.ss-chip:hover { background: var(--ss-surface-hover); border-color: var(--ss-ink-3); }
.ss-chip:focus-visible {
  outline: 2px solid var(--ss-brand-blue-ring);
  outline-offset: 2px;
}
.ss-chip.is-active {
  border-color: var(--ss-brand-blue);
  background: var(--ss-brand-blue-tint);
}
.ss-chip.is-active.is-expanded {
  grid-column: 1 / -1;
}
.ss-chip-row {
  display: flex; align-items: center;
  gap: var(--ss-pad-2);
  min-width: 0;
}
.ss-chip-row + .ss-chip-row,
.ss-chip-row + .ss-chip-context,
.ss-chip-row + .ss-chip-reason,
.ss-chip-context + .ss-chip-reason {
  margin-top: var(--ss-pad-1);
}
.ss-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  display: inline-block;
}
.ss-dot.is-ok { background: var(--ss-ok); }
.ss-dot.is-warn { background: var(--ss-warn); }
.ss-dot.is-crit { background: var(--ss-crit); }
.ss-dot.is-unknown {
  background: transparent;
  border: 1.5px solid var(--ss-unknown);
}
.ss-chip-name {
  font-size: var(--ss-fs-body);
  font-weight: var(--ss-fw-medium);
  color: var(--ss-ink-1);
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ss-chip-metric {
  font-size: var(--ss-fs-small);
  color: var(--ss-ink-3);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0; white-space: nowrap;
}
.ss-chip-metric.is-stale { color: var(--ss-ink-4); font-style: italic; }
.ss-chip-context {
  font-size: var(--ss-fs-small);
  color: var(--ss-ink-4);
  margin-left: calc(8px + var(--ss-pad-2));
}
.ss-chip-reason {
  font-size: var(--ss-fs-small);
  color: var(--ss-ink-2);
  margin-left: calc(8px + var(--ss-pad-2));
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* Detail view */
.ss-detail {
  grid-column: 1 / -1;
  background: var(--ss-detail-bg);
  border: 1px solid var(--ss-ink-5);
  border-radius: var(--ss-radius-chip);
  padding: var(--ss-pad-4);
  box-shadow: var(--ss-shadow-detail);
}
.ss-detail-header {
  display: flex; justify-content: space-between;
  gap: var(--ss-pad-3);
  padding-bottom: var(--ss-pad-3);
  border-bottom: 1px solid var(--ss-ink-5);
}
.ss-detail-title {
  font-size: var(--ss-fs-title);
  font-weight: var(--ss-fw-semibold);
  color: var(--ss-ink-1);
  letter-spacing: -0.01em;
}
.ss-detail-sub {
  font-size: var(--ss-fs-small);
  color: var(--ss-ink-3);
  margin-top: 2px;
}
.ss-detail-close {
  background: none; border: none;
  color: var(--ss-ink-3);
  font-size: 18px; line-height: 1;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
}
.ss-detail-close:hover { color: var(--ss-ink-1); background: var(--ss-surface-hover); }
.ss-detail-close:focus-visible {
  outline: 2px solid var(--ss-brand-blue-ring);
  outline-offset: 2px;
}
.ss-attention {
  margin-top: var(--ss-pad-4);
  padding: var(--ss-pad-3) var(--ss-pad-4);
  border-radius: var(--ss-radius-chip);
  background: var(--ss-surface-hover);
  border-left: var(--ss-stripe-width) solid var(--ss-unknown);
}
.ss-attention.is-ok   { border-left-color: var(--ss-ok); }
.ss-attention.is-warn { border-left-color: var(--ss-warn); background: var(--ss-warn-tint); }
.ss-attention.is-crit { border-left-color: var(--ss-crit); background: var(--ss-crit-tint); }
.ss-attention-label {
  font-size: var(--ss-fs-micro);
  color: var(--ss-ink-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: var(--ss-fw-semibold);
}
.ss-attention-state {
  font-size: var(--ss-fs-title);
  font-weight: var(--ss-fw-semibold);
  margin-top: 2px;
  color: var(--ss-ink-1);
}
.ss-attention-state.is-ok   { color: var(--ss-ok); }
.ss-attention-state.is-warn { color: var(--ss-warn); }
.ss-attention-state.is-crit { color: var(--ss-crit); }
.ss-attention-state.is-unknown { color: var(--ss-ink-3); }
.ss-attention-reasons {
  margin-top: var(--ss-pad-2);
  color: var(--ss-ink-2);
  font-size: var(--ss-fs-body);
  line-height: 1.55;
}
.ss-detail-grid {
  margin-top: var(--ss-pad-4);
  display: grid; gap: var(--ss-pad-4);
  grid-template-columns: 1fr 1fr;
}
@media (max-width: 600px) {
  .ss-detail-grid { grid-template-columns: 1fr; }
}
.ss-detail-section-label {
  font-size: var(--ss-fs-micro);
  text-transform: uppercase;
  letter-spacing: 0.10em;
  color: var(--ss-ink-3);
  font-weight: var(--ss-fw-semibold);
  margin-bottom: var(--ss-pad-2);
}
.ss-metric-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--ss-pad-2);
}
/* Metric tile inside the detail panel. Default chrome is a grey 1px stroke;
   when the metric value triggers a severity, the stroke (not the background)
   adopts the severity color. The label and value text stay neutral so the
   tile reads as "this metric is the reason," not "this metric is celebrating." */
.ss-metric {
  background: var(--ss-bg);
  border: 1px solid var(--ss-ink-5);
  border-radius: var(--ss-radius-chip);
  padding: var(--ss-pad-2) var(--ss-pad-3);
}
.ss-metric.is-warn { border-color: var(--ss-warn); }
.ss-metric.is-crit { border-color: var(--ss-crit); }
.ss-metric-lbl {
  font-size: var(--ss-fs-micro);
  color: var(--ss-ink-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 2px;
}
.ss-metric-val {
  font-size: var(--ss-fs-body);
  font-weight: var(--ss-fw-semibold);
  color: var(--ss-ink-1);
  font-variant-numeric: tabular-nums;
}
.ss-metric-val.is-warn { color: var(--ss-warn); }
.ss-metric-val.is-crit { color: var(--ss-crit); }
/* PASSED status and other healthy-state values stay neutral ink. Color is
   reserved for problems; the absence of color says "no concern here." */

/* Disk usage tile. Always full-width inside the drives grid. Sits at the
   bottom of an agent group, after that agent's drive chips. Visually
   distinct from drive chips: no severity stripe, leading disk glyph,
   lower-key surface, single horizontal row. */
.ss-disk-tile {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: auto 1fr 2fr auto auto;
  align-items: center;
  gap: var(--ss-pad-3);
  background: var(--ss-bg);
  border: 1px solid var(--ss-ink-5);
  border-radius: var(--ss-radius-chip);
  padding: var(--ss-pad-2) var(--ss-pad-3);
  font-size: var(--ss-fs-small);
}
.ss-disk-tile + .ss-disk-tile { margin-top: -2px; }  /* tighten siblings */
.ss-disk-glyph {
  width: 14px; height: 14px;
  flex-shrink: 0;
  color: var(--ss-ink-3);
  display: flex; align-items: center; justify-content: center;
}
.ss-disk-glyph svg { width: 100%; height: 100%; }
.ss-disk-mount {
  color: var(--ss-ink-2);
  font-weight: var(--ss-fw-medium);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  /* Truncate from the LEFT so the path tail (the recognizable part)
     remains visible. The unicode-bidi + direction trick keeps the
     ellipsis at the start without reversing the text. */
  direction: rtl;
  text-align: left;
}
.ss-disk-bar {
  height: 6px;
  background: var(--ss-ink-5);
  border-radius: 3px;
  overflow: hidden;
}
/* Bar fill is greyscale at all usage levels. Severity is conveyed by the
   percentage number's color, not the bar. Keeps disk tiles visually quiet
   and reserves color for the alert moments that truly demand the eye. */
.ss-disk-bar-fill {
  height: 100%;
  background: var(--ss-ink-3);
  border-radius: 3px;
}
/* Stale disk: bar fill uses the unidentified-stripe grey so the bar is
   present but distinctly quieter than a live reading. */
.ss-disk-tile.is-stale .ss-disk-bar-fill { background: var(--ss-stripe-unidentified); }
.ss-disk-pct {
  font-variant-numeric: tabular-nums;
  color: var(--ss-ink-1);
  font-weight: var(--ss-fw-semibold);
  min-width: 40px; text-align: right;
}
.ss-disk-tile.is-warn .ss-disk-pct { color: var(--ss-warn); }
.ss-disk-tile.is-crit .ss-disk-pct { color: var(--ss-crit); }
.ss-disk-tile.is-stale .ss-disk-pct { color: var(--ss-ink-4); font-style: italic; }
.ss-disk-size {
  color: var(--ss-ink-3);
  font-variant-numeric: tabular-nums;
  font-size: var(--ss-fs-micro);
  min-width: 96px; text-align: right;
}

/* Empty state */
.ss-empty {
  padding: 48px var(--ss-pad-4);
  text-align: center;
  color: var(--ss-ink-3);
}
.ss-empty-icon {
  width: 48px; height: 48px;
  margin: 0 auto var(--ss-pad-3);
  opacity: 0.5;
}
.ss-empty-title {
  font-size: var(--ss-fs-title);
  font-weight: var(--ss-fw-semibold);
  color: var(--ss-ink-1);
  margin-bottom: var(--ss-pad-2);
}
.ss-empty-text {
  font-size: var(--ss-fs-body);
  line-height: 1.6;
  max-width: 320px;
  margin: 0 auto;
}

/* Skeletons */
@keyframes ss-skel {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 0.8; }
}
.ss-skel {
  background: var(--ss-ink-5);
  border-radius: 4px;
  animation: ss-skel 1.6s ease-in-out infinite;
}
.ss-chip.is-loading { cursor: default; pointer-events: none; }
.ss-chip.is-loading::before { background: var(--ss-ink-5); }
`;

/* The brand magnifier mark used in the header. Geometric placeholder; a
   Spy v Spy variant is on the v1.1 roadmap. */
const BRAND_MAGNIFIER_SVG = `
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <circle cx="13" cy="13" r="9" fill="none" stroke="var(--ss-brand-blue)" stroke-width="2.5"/>
  <path d="M13 8 L18 13 L17 13 L17 18 L9 18 L9 13 L8 13 Z" fill="var(--ss-brand-blue)" opacity="0.85"/>
  <line x1="20" y1="20" x2="27" y2="27" stroke="var(--ss-brand-blue)" stroke-width="3" stroke-linecap="round"/>
</svg>`;

const EMPTY_STATE_SVG = `
<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <circle cx="20" cy="20" r="14" fill="none" stroke="currentColor" stroke-width="2.5"/>
  <line x1="30" y1="30" x2="42" y2="42" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
</svg>`;

/* Small disk glyph used as the leading mark on disk usage tiles. Stylized
   stacked-platter shape that reads as "disk" without competing with the
   chip's status dot in the agent group above. Inherits currentColor. */
const DISK_GLYPH_SVG = `
<svg viewBox="0 0 14 14" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <ellipse cx="7" cy="3" rx="5" ry="1.6" fill="none" stroke="currentColor" stroke-width="1.4"/>
  <path d="M2 3 V11 A5 1.6 0 0 0 12 11 V3" fill="none" stroke="currentColor" stroke-width="1.4"/>
  <path d="M2 7 A5 1.6 0 0 0 12 7" fill="none" stroke="currentColor" stroke-width="1.2" opacity="0.55"/>
</svg>`;

/* ═══════════════════════════════════════════════════════════════════════════
   Main card element
   ═══════════════════════════════════════════════════════════════════════════ */
class SmartSnifferCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config   = null;
    this._hass     = null;
    this._drives   = [];
    this._filesystems = [];
    this._agentCount  = 0;
    this._selected = null;
  }

  /* ── HA card protocol ───────────────────────────────────────────────── */

  static getConfigElement() {
    return document.createElement("smart-sniffer-card-editor");
  }

  static getStubConfig() {
    return { title: "Drive Health", columns: 2, show_ok: true };
  }

  setConfig(config) {
    if (!config) throw new Error("smart-sniffer-card: config is required");
    const merged = {
      title: "Drive Health",
      columns: 2,
      show_ok: true,
      drives: [],
      agents: [],
      usage_warn: 90,
      usage_crit: 95,
      show_storage: true,
      ...config,
    };
    merged.columns    = Math.min(4, Math.max(1, parseInt(merged.columns) || 2));
    merged.usage_warn = Math.min(100, Math.max(0, parseInt(merged.usage_warn) || 90));
    merged.usage_crit = Math.min(100, Math.max(0, parseInt(merged.usage_crit) || 95));
    if (merged.usage_crit < merged.usage_warn) merged.usage_crit = merged.usage_warn;
    if (!Array.isArray(merged.drives)) merged.drives = [];
    if (!Array.isArray(merged.agents)) merged.agents = [];
    this._config = merged;
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    if (!this._config) return;

    // Toggle the dark-mode class based on HA's own theme decision (which
    // itself respects the user's "Auto" setting). This is the single source
    // of truth and replaces the unreliable prefers-color-scheme media query.
    this._applyDarkModeClass(hass);

    if (prev && !this._shouldUpdate(prev, hass)) return;

    const { drives, filesystems, agentCount } = this._collect(hass);
    this._drives = drives;
    this._filesystems = filesystems;
    this._agentCount  = agentCount;
    this._render();
  }

  _applyDarkModeClass(hass) {
    const isDark = !!(hass && hass.themes && hass.themes.darkMode);
    if (isDark) this.classList.add("is-ha-dark");
    else this.classList.remove("is-ha-dark");
  }

  getCardSize() {
    const cols = this._config?.columns || 2;
    const rows = Math.ceil((this._drives?.length || 4) / cols);
    return rows + 2;
  }

  /* ── Change detection ───────────────────────────────────────────────── */

  _shouldUpdate(prev, curr) {
    if (prev.entities !== curr.entities) return true;
    if (prev.devices  !== curr.devices)  return true;
    if (prev.config_entries !== curr.config_entries) return true;
    if (this._drives.length === 0 && this._filesystems.length === 0) return true;

    // Tracked entity states across all drives + filesystem entities + agent statuses.
    for (const drive of this._drives) {
      for (const eid of Object.values(drive.entities)) {
        if (!eid) continue;
        if (prev.states[eid]?.state !== curr.states[eid]?.state) return true;
      }
      if (drive.agent_status_entity) {
        const e = drive.agent_status_entity;
        if (prev.states[e]?.state !== curr.states[e]?.state) return true;
      }
    }
    for (const fs of this._filesystems) {
      if (prev.states[fs.entity_id]?.state !== curr.states[fs.entity_id]?.state) return true;
    }
    return false;
  }

  /* ── Collection: drives, filesystems, agent count ───────────────────── */

  _collect(hass) {
    const drives = [];
    const filesystems = [];

    if (!hass?.entities || !hass?.devices) {
      return { drives, filesystems, agentCount: 0 };
    }

    // Group entity IDs by device_id (only entries from our domain).
    const entitiesByDevice = {};
    for (const [entityId, entry] of Object.entries(hass.entities)) {
      if ((entry.platform || "").toLowerCase() !== DOMAIN) continue;
      if (!hass.states[entityId]) continue;
      const devId = entry.device_id;
      if (!devId) continue;
      if (!entitiesByDevice[devId]) entitiesByDevice[devId] = [];
      entitiesByDevice[devId].push(entityId);
    }

    // Index agent devices by entry ID, AND extract the friendly agent name
    // from the agent device's name (since hass.config_entries is not reliably
    // exposed to custom cards via the browser-side hass object). The agent
    // device's name on this integration is "SMART Sniffer (hostname)" which
    // _friendlyAgentName strips down to just "hostname".
    const agentDevicesByEntry = {};
    const agentNameByEntry = {};
    for (const [devId, dev] of Object.entries(hass.devices)) {
      if (!dev) continue;
      if (dev.model !== "Agent") continue;
      const entryId = (dev.config_entries || [])[0];
      if (!entryId) continue;
      agentDevicesByEntry[entryId] = devId;
      // Prefer name_by_user (user-set friendly name) over name (default).
      const rawName = dev.name_by_user || dev.name || "";
      agentNameByEntry[entryId] = this._friendlyAgentName(rawName);
    }

    const agentCount = Object.keys(agentNameByEntry).length;

    // Walk devices, classify into drives vs filesystem vs agent.
    for (const [devId, entityIds] of Object.entries(entitiesByDevice)) {
      const dev = hass.devices[devId] || {};

      // Skip non-drive devices (filesystem, agent).
      if (NON_DRIVE_DEVICE_MODELS.has(dev.model)) {
        if (dev.model === "Filesystem Monitor") {
          this._collectFilesystems(hass, dev, entityIds, filesystems, agentNameByEntry);
        }
        continue;
      }
      // Belt-and-suspenders: identifier-based skip.
      if (this._deviceHasIdentifierSuffix(dev, "_filesystems")) continue;
      if (this._deviceHasIdentifierSuffix(dev, "_agent")) continue;

      // This device is a drive.
      const drive = this._buildDrive(hass, dev, devId, entityIds, agentDevicesByEntry, agentNameByEntry);
      if (drive) drives.push(drive);
    }

    drives.sort((a, b) => {
      const ao = SORT_ORDER[a.state] ?? 99;
      const bo = SORT_ORDER[b.state] ?? 99;
      if (ao !== bo) return ao - bo;
      return (a.name || "").localeCompare(b.name || "");
    });

    return { drives, filesystems, agentCount };
  }

  _deviceHasIdentifierSuffix(dev, suffix) {
    const ids = dev.identifiers || [];
    for (const tup of ids) {
      if (Array.isArray(tup) && tup.length >= 2) {
        const second = String(tup[1] || "");
        if (second.endsWith(suffix)) return true;
      }
    }
    return false;
  }

  _friendlyAgentName(title) {
    if (!title) return "agent";
    // Strip the "SMART Sniffer (host)" wrapper if present.
    const m = String(title).match(/^SMART Sniffer \((.+)\)$/);
    return m ? m[1] : title;
  }

  _collectFilesystems(hass, dev, entityIds, out, agentNameByEntry) {
    const entryId = (dev.config_entries || [])[0];
    let agentName = agentNameByEntry[entryId];
    if (!agentName && entryId) agentName = `entry ${entryId.slice(-6)}`;
    if (!agentName) agentName = "unknown agent";
    for (const eid of entityIds) {
      const st = hass.states[eid];
      if (!st) continue;
      const attrs = st.attributes || {};
      // Only true disk-usage sensors carry a mountpoint attribute. Skip anything else.
      if (!attrs.mountpoint) continue;
      out.push({
        entity_id: eid,
        agent_name: agentName,
        mountpoint: attrs.mountpoint,
        friendly_mount: this._friendlyMount(attrs.mountpoint),
        device: attrs.device,
        fstype: attrs.fstype,
        total_gb: attrs.total_gb,
        used_gb:  attrs.used_gb,
        available_gb: attrs.available_gb,
        percent: parseFloat(st.state),
        available: st.state !== "unavailable" && st.state !== "unknown",
      });
    }
  }

  _friendlyMount(mp) {
    if (mp === "/") return "Root (/)";
    return mp;
  }

  _buildDrive(hass, dev, devId, entityIds, agentDevicesByEntry, agentNameByEntry) {
    const entities = {};
    for (const eid of entityIds) {
      for (const [key, pat] of Object.entries(ENTITY_PATTERNS)) {
        if (entities[key]) continue;
        if (pat.test(eid)) { entities[key] = eid; break; }
      }
    }

    // Must have at minimum the attention sensor for this to be a drive.
    if (!entities.attention_needed) return null;

    const entryId = (dev.config_entries || [])[0];
    // Resolve agent name with a fallback chain so the user always sees
    // something useful: agent device's friendly name, the drive's manufacturer-
    // less device name, or a short slice of the entry ID as a last resort.
    let agentName = agentNameByEntry[entryId];
    if (!agentName && entryId) {
      // Attempt direct lookup of the agent device for this entry.
      const agentDevId = agentDevicesByEntry[entryId];
      const agentDev = agentDevId ? hass.devices[agentDevId] : null;
      if (agentDev) {
        agentName = this._friendlyAgentName(agentDev.name_by_user || agentDev.name || "");
      }
    }
    if (!agentName && entryId) {
      // Use a short, recognizable slice of the entry ID. Better than literal "agent".
      agentName = `entry ${entryId.slice(-6)}`;
    }
    if (!agentName) agentName = "unknown agent";

    // Look up the agent-status entity for this drive's agent.
    let agentStatusEntity = null;
    const agentDevId = agentDevicesByEntry[entryId];
    if (agentDevId && hass.entities) {
      for (const [eid, entry] of Object.entries(hass.entities)) {
        if (entry.device_id !== agentDevId) continue;
        if (eid.endsWith("_agent_status")) { agentStatusEntity = eid; break; }
      }
    }
    const agentOnline = agentStatusEntity
      ? hass.states[agentStatusEntity]?.state === "on"
      : true; // Optimistic if we can't find the entity.

    // Look up the agent's last-seen entity for stale durations.
    let agentLastSeenEntity = null;
    if (agentDevId && hass.entities) {
      for (const [eid, entry] of Object.entries(hass.entities)) {
        if (entry.device_id !== agentDevId) continue;
        if (eid.endsWith("_agent_last_seen")) { agentLastSeenEntity = eid; break; }
      }
    }

    const attnRaw = (this._state(hass, entities.attention_needed) || "").toLowerCase();
    const reasonsRaw = this._state(hass, entities.attention_reasons) || "";
    const standbyRaw = this._state(hass, entities.standby);
    const standby = standbyRaw === "on";
    const dataAsOf = standby
      ? hass.states[entities.standby]?.attributes?.data_as_of
      : null;

    // Decide visual state. See spec §5 decision tree.
    let state;
    if (!agentOnline)               state = "stale";
    else if (attnRaw === "unsupported") state = "unsupported";
    else if (attnRaw === "yes")     state = "critical";
    else if (attnRaw === "maybe")   state = "watch";
    else if (standby)               state = "cached";
    else if (attnRaw === "no")      state = "healthy";
    else                            state = "unsupported";

    // Build the temperature display string.
    const tempVal = this._state(hass, entities.temperature);
    const tempUnit = this._attr(hass, entities.temperature, "unit_of_measurement") || "°C";
    const tempStr = tempVal != null ? `${tempVal} ${tempUnit}` : null;

    // Drive name fallback.
    const rawName = dev.name || dev.name_by_user;
    const name = (rawName && String(rawName).trim()) || "Unidentified drive";

    // Build a sublabel for the detail header.
    const protocol = this._detectProtocol(entities);
    const subParts = [
      dev.manufacturer,
      dev.serial_number || dev.serial,
      protocol,
      `on ${agentName}`,
    ].filter(Boolean);

    return {
      device_id: devId,
      agent_entry_id: entryId,
      agent_name: agentName,
      agent_status_entity: agentStatusEntity,
      agent_last_seen_entity: agentLastSeenEntity,
      agent_online: agentOnline,
      name,
      manufacturer: dev.manufacturer || "",
      model: dev.model || "",
      serial: dev.serial_number || dev.serial || "",
      protocol,
      detail_sub: subParts.join(" · "),
      entities,
      attention: attnRaw,
      attention_reasons: reasonsRaw,
      health_raw: this._state(hass, entities.health),
      standby,
      data_as_of: dataAsOf,
      temperature: tempStr,
      state,
    };
  }

  _detectProtocol(entities) {
    if (entities.critical_warning || entities.available_spare) return "NVMe";
    if (entities.reallocated_sector_count || entities.spin_retry_count) return "ATA";
    return "";
  }

  _state(hass, eid) {
    if (!eid) return null;
    const s = hass.states[eid]?.state;
    if (s === "unavailable" || s === "unknown") return null;
    return s ?? null;
  }

  _attr(hass, eid, name) {
    if (!eid) return null;
    return hass.states[eid]?.attributes?.[name] ?? null;
  }

  /* ── Render ─────────────────────────────────────────────────────────── */

  _render() {
    const root = this.shadowRoot;
    const cfg  = this._config;

    // First render: install styles.
    if (!root.querySelector("style")) {
      const styleEl = document.createElement("style");
      styleEl.textContent = STYLES;
      root.appendChild(styleEl);
    }

    // Clear existing card body.
    root.querySelectorAll(".ss-card").forEach(el => el.remove());

    const card = document.createElement("div");
    card.className = "ss-card";
    root.appendChild(card);

    // Apply config filters to drives.
    let drives = this._drives;
    if (cfg.agents.length > 0) {
      drives = drives.filter(d => cfg.agents.includes(d.agent_entry_id));
    }
    if (cfg.drives.length > 0) {
      drives = drives.filter(d => cfg.drives.includes(d.device_id));
    }

    // Loading detection.
    const isLoading = this._isLoading(drives);

    // Header.
    card.appendChild(this._renderHeader(drives, this._agentCount, isLoading));

    // Empty state.
    if (drives.length === 0 && this._filesystems.length === 0 && !isLoading) {
      card.appendChild(this._renderEmpty());
      return;
    }

    // Loading state.
    if (isLoading) {
      card.appendChild(this._renderLoading());
      return;
    }

    // Stats strip.
    if (drives.length > 0) {
      card.appendChild(this._renderStats(drives));
    }

    // Alert banner.
    const banner = this._renderBanner(drives);
    if (banner) card.appendChild(banner);

    // Compute agent order once (severity-first, alphabetical tiebreaker)
    // so both the drives and storage sections render in the same hierarchy.
    const driveAgentOrder = this._computeAgentOrder(drives);

    // Drives section. As of v1.0.6, this also renders disk usage tiles
    // inline within each agent's group, so there is no separate storage
    // section anymore. The section renders if EITHER drives or filesystems
    // exist (filesystem-only agents are valid).
    const haveStorage = cfg.show_storage && this._filesystems.length > 0;
    if (drives.length > 0 || haveStorage) {
      card.appendChild(this._renderDrivesSection(drives, driveAgentOrder));
    }
  }

  /* Compute the order in which agents should appear: agents with the worst
     drives first, then alphabetical (case-insensitive) tiebreaker. */
  _computeAgentOrder(drives) {
    const byAgent = {};
    for (const d of drives) {
      const k = d.agent_name || "agent";
      if (!byAgent[k]) byAgent[k] = [];
      byAgent[k].push(d);
    }
    const rank = (driveList) => {
      let best = 99;
      for (const d of driveList) {
        const r = SORT_ORDER[d.state] ?? 99;
        if (r < best) best = r;
      }
      return best;
    };
    return Object.keys(byAgent).sort((a, b) => {
      const ra = rank(byAgent[a]);
      const rb = rank(byAgent[b]);
      if (ra !== rb) return ra - rb;
      return a.localeCompare(b, undefined, { sensitivity: "base" });
    });
  }

  _isLoading(drives) {
    if (drives.length > 0) return false;
    if (!this._hass) return true;
    if (!this._hass.config_entries) return false;
    const haveEntries = Object.values(this._hass.config_entries)
      .some(e => (e.domain || "").toLowerCase() === DOMAIN);
    return haveEntries;
  }

  /* Header */
  _renderHeader(drives, agentCount, isLoading) {
    const el = document.createElement("div");
    el.className = "ss-header";

    const icon = document.createElement("div");
    icon.className = "ss-header-icon";
    icon.innerHTML = BRAND_MAGNIFIER_SVG;

    const text = document.createElement("div");
    text.className = "ss-header-text";

    const title = document.createElement("div");
    title.className = "ss-header-title";
    title.textContent = this._config.title || "Drive Health";

    const sub = document.createElement("div");
    sub.className = "ss-header-sub";
    sub.textContent = this._headerSubline(drives.length, agentCount, isLoading);

    text.appendChild(title);
    text.appendChild(sub);
    el.appendChild(icon);
    el.appendChild(text);
    return el;
  }

  _headerSubline(driveCount, agentCount, isLoading) {
    if (isLoading) return "SMART Sniffer · finding drives…";
    if (driveCount === 0 && this._filesystems.length === 0) return "SMART Sniffer";

    const parts = ["SMART Sniffer"];
    if (driveCount > 0) {
      parts.push(`${driveCount} drive${driveCount === 1 ? "" : "s"}`);
    }
    if (agentCount > 1) {
      parts.push(`${agentCount} agents`);
    }
    return parts.join(" · ");
  }

  /* Stats strip */
  _renderStats(drives) {
    const counts = { healthy: 0, watch: 0, critical: 0, unsupported: 0, stale: 0, cached: 0 };
    for (const d of drives) counts[d.state] = (counts[d.state] || 0) + 1;

    const total = drives.length;
    const healthy = counts.healthy + counts.cached; // cached drives count as their underlying state's color, but for the stat strip cached is healthy-ish
    const watch = counts.watch;
    const critical = counts.critical;
    const noData = counts.unsupported + counts.stale;

    // Use compact (single-line) layout when host width is narrow.
    const condensed = this._isNarrowViewport();

    const el = document.createElement("div");
    el.className = "ss-stats" + (condensed ? " is-condensed" : "");

    if (condensed) {
      el.innerHTML = `
        <div class="ss-stat-line">
          <strong>${total}</strong> drives ·
          <strong${healthy > 0 ? ' class="is-ok"' : ''}>${healthy}</strong> healthy ·
          <strong${watch    > 0 ? ' class="is-warn"' : ''}>${watch}</strong> watch ·
          <strong${critical > 0 ? ' class="is-crit"' : ''}>${critical}</strong> critical ·
          <strong${noData   > 0 ? ' class="is-unknown"' : ''}>${noData}</strong> no data
        </div>`;
      return el;
    }

    const cells = [
      { val: total,    lbl: "Drives",   cls: "" },
      { val: healthy,  lbl: "Healthy",  cls: healthy > 0 ? "is-ok" : "" },
      { val: watch,    lbl: "Watch",    cls: watch > 0 ? "is-warn" : "" },
      { val: critical, lbl: "Critical", cls: critical > 0 ? "is-crit" : "" },
      { val: noData,   lbl: "No data",  cls: noData > 0 ? "is-unknown" : "" },
    ];
    for (const c of cells) {
      const cell = document.createElement("div");
      cell.className = "ss-stat";
      cell.innerHTML = `
        <div class="ss-stat-val ${c.cls}">${c.val}</div>
        <div class="ss-stat-lbl">${this._esc(c.lbl)}</div>`;
      el.appendChild(cell);
    }
    return el;
  }

  _isNarrowViewport() {
    // Use the host's offsetWidth. Falls back to window width if unrendered.
    const w = this.offsetWidth || window.innerWidth || 9999;
    return w < 380;
  }

  /* Banner */
  _renderBanner(drives) {
    const crits = drives.filter(d => d.state === "critical");
    const watches = drives.filter(d => d.state === "watch");
    if (crits.length === 0 && watches.length === 0) return null;

    const el = document.createElement("div");
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");

    if (crits.length > 0) {
      el.className = "ss-banner is-crit";
      const headline = `${crits.length} drive${crits.length === 1 ? " needs" : "s need"} attention`;
      const detail = crits.map(d => d.name).join(" · ");
      el.innerHTML = `
        <div class="ss-banner-headline">${this._esc(headline)}</div>
        <div class="ss-banner-detail">${this._esc(detail)}</div>`;
    } else {
      el.className = "ss-banner is-warn";
      const headline = `${watches.length} drive${watches.length === 1 ? "" : "s"} showing early warning signs`;
      const detail = watches.map(d => d.name).join(" · ");
      el.innerHTML = `
        <div class="ss-banner-headline">${this._esc(headline)}</div>
        <div class="ss-banner-detail">${this._esc(detail)}</div>`;
    }
    return el;
  }

  /* Drives section. Drives are grouped by agent, with agents sorted by their
     worst-drive severity (an agent with a critical drive floats above an
     agent whose drives are all healthy, even if it would alphabetize later).
     Within each agent, drives sort worst-first, then disk tiles render below.
     The agentOrder argument comes from _computeAgentOrder so the same
     hierarchy is honored even for agents that have no drives (filesystem-
     only agents). */
  _renderDrivesSection(drives, agentOrder) {
    const cfg = this._config;
    const visible = (cfg.show_ok === false)
      ? drives.filter(d => d.state !== "healthy" && d.state !== "cached")
      : drives;

    const section = document.createElement("div");
    section.className = "ss-drives-section";

    // When show_ok is false and the visible count differs from total, show a
    // small hint chip so the user knows drives are being filtered. Otherwise
    // suppress the redundant "Drives / X shown" header. The card title above
    // and the stats strip already say "X drives".
    if (visible.length !== drives.length) {
      const hint = document.createElement("div");
      hint.className = "ss-drives-hint";
      hint.textContent = `${visible.length} of ${drives.length} shown · healthy hidden`;
      section.appendChild(hint);
    }

    const grid = document.createElement("div");
    let cols = cfg.columns;
    if (this._isNarrowViewport() || this.offsetWidth < 600) cols = 1;
    grid.className = `ss-drives-grid cols-${cols}`;
    section.appendChild(grid);

    // Group visible drives by agent name.
    const drivesByAgent = {};
    for (const d of visible) {
      const key = d.agent_name || "agent";
      if (!drivesByAgent[key]) drivesByAgent[key] = [];
      drivesByAgent[key].push(d);
    }

    // Group filesystems by agent name (when storage display is enabled).
    const filesystemsByAgent = {};
    if (cfg.show_storage) {
      for (const fs of this._filesystems) {
        const key = fs.agent_name || "agent";
        if (!filesystemsByAgent[key]) filesystemsByAgent[key] = [];
        filesystemsByAgent[key].push(fs);
      }
    }

    // Build the union of agents with either drives or disks. Order them per
    // _computeAgentOrder, then append any disk-only agents alphabetically.
    const seen = new Set();
    const agents = [];
    for (const a of (agentOrder || Object.keys(drivesByAgent))) {
      if (drivesByAgent[a] || filesystemsByAgent[a]) {
        if (!seen.has(a)) { agents.push(a); seen.add(a); }
      }
    }
    const remaining = Object.keys(filesystemsByAgent)
      .filter(a => !seen.has(a))
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    agents.push(...remaining);

    // Render each agent's section: header, drive chips (if any), disk tiles (if any).
    for (const agent of agents) {
      const agentDrives = drivesByAgent[agent] || [];
      const agentDisks  = filesystemsByAgent[agent] || [];
      if (agentDrives.length === 0 && agentDisks.length === 0) continue;

      // Per-agent header summary. Drives only; disks live below as their own
      // tiles with their own coloring. When the agent has no drives, the
      // summary reads "disks only" exactly as the user asked for.
      const counts = { critical: 0, watch: 0, unsupported: 0, stale: 0, cached: 0, healthy: 0 };
      for (const d of agentDrives) counts[d.state] = (counts[d.state] || 0) + 1;

      let summaryHtml;
      if (agentDrives.length === 0) {
        summaryHtml = "disks only";
      } else {
        const parts = [];
        if (counts.critical > 0) parts.push(`<span class="has-crit">${counts.critical} critical</span>`);
        if (counts.watch    > 0) parts.push(`<span class="has-warn">${counts.watch} watch</span>`);
        const noData = (counts.unsupported || 0) + (counts.stale || 0);
        if (noData > 0) parts.push(`${noData} no data`);
        const calmCount = (counts.healthy || 0) + (counts.cached || 0);
        if (calmCount > 0 && parts.length === 0) parts.push(`${calmCount} healthy`);
        summaryHtml = parts.join(" · ");
      }

      const headerRow = document.createElement("div");
      headerRow.className = "ss-agent-header";
      headerRow.innerHTML = `
        <span class="ss-agent-header-name">${this._esc(agent)}</span>
        <span class="ss-agent-header-summary">${summaryHtml}</span>`;
      grid.appendChild(headerRow);

      // Drive chips, with detail panel inline if active.
      for (const d of agentDrives) {
        const chip = this._buildChip(d);
        grid.appendChild(chip);
        if (this._selected === d.device_id) {
          chip.classList.add("is-active", "is-expanded");
          const detail = this._buildDetail(d);
          grid.appendChild(detail);
        }
      }

      // Disk tiles, full-width, after the agent's drive chips.
      for (const fs of agentDisks) {
        grid.appendChild(this._buildDiskTile(fs));
      }
    }

    return section;
  }

  /* Build a single disk usage tile. Full-width, leading disk glyph,
     bar + percentage + size in a single horizontal row. Stale-aware
     when its agent is offline. */
  _buildDiskTile(fs) {
    const cfg = this._config;
    const tile = document.createElement("div");
    tile.className = "ss-disk-tile";
    if (!fs.available) tile.classList.add("is-stale");
    else if (fs.percent >= cfg.usage_crit) tile.classList.add("is-crit");
    else if (fs.percent >= cfg.usage_warn) tile.classList.add("is-warn");

    const pct = (!fs.available || isNaN(fs.percent))
      ? "?"
      : `${Math.round(fs.percent)}%`;
    const fillWidth = (!fs.available || isNaN(fs.percent))
      ? 0
      : Math.max(0, Math.min(100, fs.percent));

    tile.innerHTML = `
      <span class="ss-disk-glyph">${DISK_GLYPH_SVG}</span>
      <div class="ss-disk-mount" title="${this._esc(fs.mountpoint)}">${this._esc(fs.friendly_mount)}</div>
      <div class="ss-disk-bar"><div class="ss-disk-bar-fill" style="width:${fillWidth}%"></div></div>
      <div class="ss-disk-pct">${this._esc(pct)}</div>
      <div class="ss-disk-size">${this._esc(this._fmtSize(fs))}</div>`;
    return tile;
  }

  /* Drive chip */
  _buildChip(drive) {
    const chip = document.createElement("div");
    chip.className = `ss-chip is-${this._stateClass(drive.state)}`;
    chip.setAttribute("role", "button");
    chip.setAttribute("tabindex", "0");
    chip.setAttribute("aria-label", `${drive.name}, ${this._stateAriaLabel(drive.state)}`);
    chip.dataset.deviceId = drive.device_id;

    const dotCls = this._dotClass(drive.state);
    const dotLabel = this._stateAriaLabel(drive.state);

    const metric = this._chipMetric(drive);
    const context = this._chipContext(drive);
    const reason = this._chipReason(drive);

    chip.innerHTML = `
      <div class="ss-chip-row">
        <span class="ss-dot ${dotCls}" role="img" aria-label="${this._esc(dotLabel)}"></span>
        <span class="ss-chip-name">${this._esc(drive.name)}</span>
        ${metric.html}
      </div>
      ${context ? `<div class="ss-chip-context">${this._esc(context)}</div>` : ""}
      ${reason  ? `<div class="ss-chip-reason">${this._esc(reason)}</div>` : ""}
    `;

    const onActivate = () => this._toggleSelected(drive.device_id);
    chip.addEventListener("click", onActivate);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate();
      }
    });
    return chip;
  }

  _chipMetric(drive) {
    if (drive.state === "stale") {
      return { html: `<span class="ss-chip-metric is-stale">agent offline</span>` };
    }
    if (drive.state === "cached") {
      const dur = this._humanDuration(drive.data_as_of);
      return { html: `<span class="ss-chip-metric is-stale">cached ${this._esc(dur)}</span>` };
    }
    if (drive.state === "unsupported") {
      // "Unsupported" matches the integration's STATE_UNSUPPORTED naming and
      // tells the user the drive doesn't expose readable SMART, rather than
      // implying transient data absence.
      return { html: `<span class="ss-chip-metric is-stale">Unsupported</span>` };
    }
    if (drive.temperature) {
      return { html: `<span class="ss-chip-metric">${this._esc(drive.temperature)}</span>` };
    }
    return { html: `<span class="ss-chip-metric is-stale">no temp</span>` };
  }

  _chipContext(drive) {
    // Agent label always renders, regardless of how many agents are configured.
    // Stale and cached states append their own descriptors.
    const parts = [`on ${drive.agent_name}`];
    if (drive.state === "stale") {
      const dur = this._humanDuration(this._lastSeenForDrive(drive));
      parts.push(`last seen ${dur} ago`);
    } else if (drive.state === "cached") {
      parts.push("in standby");
    }
    return parts.join(" · ");
  }

  _chipReason(drive) {
    if (drive.state === "watch" || drive.state === "critical" || drive.state === "unsupported") {
      const r = drive.attention_reasons;
      if (r && r !== "No issues detected") return r;
    }
    return null;
  }

  _lastSeenForDrive(drive) {
    if (!drive.agent_last_seen_entity || !this._hass) return null;
    return this._hass.states[drive.agent_last_seen_entity]?.state || null;
  }

  _humanDuration(isoOrNull) {
    if (!isoOrNull) return "?";
    const then = Date.parse(isoOrNull);
    if (isNaN(then)) return "?";
    const ms = Date.now() - then;
    if (ms < 0) return "now";
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m`;
    const hr = Math.floor(min / 60);
    const minRem = min % 60;
    if (hr < 24) return minRem ? `${hr}h ${String(minRem).padStart(2, "0")}m` : `${hr}h`;
    const day = Math.floor(hr / 24);
    const hrRem = hr % 24;
    return hrRem ? `${day}d ${hrRem}h` : `${day}d`;
  }

  _stateClass(state) {
    return ({
      healthy: "ok",
      watch: "warn",
      critical: "crit",
      unsupported: "unsupported",
      stale: "stale",
      cached: "ok",
    })[state] || "unsupported";
  }

  _dotClass(state) {
    return ({
      healthy: "is-ok",
      watch: "is-warn",
      critical: "is-crit",
      unsupported: "is-unknown",
      stale: "is-unknown",
      cached: "is-ok",
    })[state] || "is-unknown";
  }

  _stateAriaLabel(state) {
    return ({
      healthy: "Healthy",
      watch: "Watch: needs monitoring",
      critical: "Critical: needs immediate attention",
      unsupported: "No data",
      stale: "Stale: agent offline",
      cached: "Healthy: data cached, drive in standby",
    })[state] || "Unknown";
  }

  _toggleSelected(deviceId) {
    if (this._selected === deviceId) this._selected = null;
    else this._selected = deviceId;
    this._render();
  }

  /* Detail view */
  _buildDetail(drive) {
    const wrap = document.createElement("div");
    wrap.className = "ss-detail";
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", `${drive.name} details`);

    const headerSub = drive.detail_sub;

    wrap.innerHTML = `
      <div class="ss-detail-header">
        <div>
          <div class="ss-detail-title">${this._esc(drive.name)}</div>
          ${headerSub ? `<div class="ss-detail-sub">${this._esc(headerSub)}</div>` : ""}
        </div>
        <button class="ss-detail-close" type="button" aria-label="Close detail view">×</button>
      </div>
      ${this._renderAttentionBlock(drive)}
      ${this._renderDiagnosticGrid(drive)}
    `;

    wrap.querySelector(".ss-detail-close").addEventListener("click", (e) => {
      e.stopPropagation();
      this._selected = null;
      this._render();
    });
    wrap.querySelector(".ss-detail-close").addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        this._selected = null;
        this._render();
      }
    });
    return wrap;
  }

  _renderAttentionBlock(drive) {
    const cls = this._stateClass(drive.state);
    const stateWord = this._attentionStateWord(drive);
    const reasons = drive.attention_reasons && drive.attention_reasons !== "No issues detected"
      ? drive.attention_reasons
      : (drive.state === "healthy" || drive.state === "cached") ? "No issues detected." : "";

    return `
      <div class="ss-attention is-${cls}">
        <div class="ss-attention-label">Attention</div>
        <div class="ss-attention-state is-${cls}">${this._esc(stateWord)}</div>
        ${reasons ? `<div class="ss-attention-reasons">${this._esc(reasons)}</div>` : ""}
      </div>`;
  }

  _attentionStateWord(drive) {
    switch (drive.state) {
      case "healthy":     return "NO: all clear";
      case "watch":       return "MAYBE: plan replacement";
      case "critical":    return "YES: back up immediately";
      case "unsupported": return "NO DATA";
      case "stale":       return "AGENT OFFLINE";
      case "cached": {
        const under = drive.attention === "yes" ? "YES: back up immediately"
                    : drive.attention === "maybe" ? "MAYBE: plan replacement"
                    : drive.attention === "unsupported" ? "NO DATA"
                    : "NO: all clear";
        const dur = this._humanDuration(drive.data_as_of);
        return `${under} · cached ${dur}`;
      }
      default: return "UNKNOWN";
    }
  }

  _renderDiagnosticGrid(drive) {
    const protocol = drive.protocol || "ATA";
    const common = this._collectCommonMetrics(drive);
    const proto  = protocol === "NVMe" ? this._collectNvmeMetrics(drive) : this._collectAtaMetrics(drive);

    if (common.length === 0 && proto.length === 0) return "";

    const renderSection = (label, metrics) => {
      if (metrics.length === 0) return "";
      // Severity class moves from the value text to the tile border. The
      // value text stays neutral ink; the border color carries the signal.
      const rows = metrics.map(m => `
        <div class="ss-metric ${m.cls || ""}">
          <div class="ss-metric-lbl">${this._esc(m.label)}</div>
          <div class="ss-metric-val ${m.cls || ""}">${this._esc(m.value)}</div>
        </div>`).join("");
      return `
        <div>
          <div class="ss-detail-section-label">${this._esc(label)}</div>
          <div class="ss-metric-grid">${rows}</div>
        </div>`;
    };

    return `
      <div class="ss-detail-grid">
        ${renderSection("Common", common)}
        ${renderSection(protocol, proto)}
      </div>`;
  }

  _collectCommonMetrics(drive) {
    const out = [];
    const hass = this._hass;
    const e = drive.entities;

    const temp = this._state(hass, e.temperature);
    if (temp != null) {
      const unit = this._attr(hass, e.temperature, "unit_of_measurement") || "°C";
      out.push({ label: "Temperature", value: `${temp} ${unit}` });
    }
    const poh = this._state(hass, e.power_on_hours);
    if (poh != null) out.push({ label: "Power-On Hours", value: `${this._fmtNumber(poh)} h` });

    const pcc = this._state(hass, e.power_cycle_count);
    if (pcc != null) out.push({ label: "Power Cycles", value: this._fmtNumber(pcc) });

    const smart = this._state(hass, e.smart_status);
    if (smart != null) {
      // PASSED stays neutral; color is reserved for problems. Only FAILED
      // gets a critical stroke.
      const cls = smart === "FAILED" ? "is-crit" : "";
      out.push({ label: "S.M.A.R.T. Status", value: smart, cls });
    }
    const wear = this._state(hass, e.wear_percent);
    if (wear != null) {
      const n = parseFloat(wear);
      let cls = "";
      if (!isNaN(n)) {
        if (n >= 90) cls = "is-crit";
        else if (n >= 70) cls = "is-warn";
      }
      out.push({ label: "Wear (%)", value: `${wear} %`, cls });
    }
    return out;
  }

  _collectAtaMetrics(drive) {
    const out = [];
    const hass = this._hass;
    const e = drive.entities;

    const counterMetric = (label, eid, threshold) => {
      const v = this._state(hass, eid);
      if (v == null) return;
      const n = parseFloat(v);
      let cls = "";
      if (!isNaN(n)) {
        if (threshold.crit != null && n > threshold.crit) cls = "is-crit";
        else if (threshold.warn != null && n > threshold.warn) cls = "is-warn";
      }
      out.push({ label, value: this._fmtNumber(v), cls });
    };

    counterMetric("Realloc. Sectors",     e.reallocated_sector_count,   { crit: 0 });
    counterMetric("Pending Sectors",      e.current_pending_sectors,    { crit: 0 });
    counterMetric("Realloc. Events",      e.reallocated_event_count,    { warn: 0 });
    counterMetric("Spin Retries",         e.spin_retry_count,           { warn: 0 });
    counterMetric("Cmd Timeouts",         e.command_timeout,            { warn: 100 });
    counterMetric("Uncorrect. Errors",    e.reported_uncorrectable,     { crit: 0 });
    return out;
  }

  _collectNvmeMetrics(drive) {
    const out = [];
    const hass = this._hass;
    const e = drive.entities;

    const spareV = this._state(hass, e.available_spare);
    const threshV = this._state(hass, e.available_spare_threshold);
    if (spareV != null) {
      const n = parseFloat(spareV);
      const t = parseFloat(threshV);
      let cls = "";
      if (!isNaN(n)) {
        if (!isNaN(t) && n <= t) cls = "is-crit";
        else if (n < 20) cls = "is-warn";
      }
      out.push({ label: "Available Spare", value: `${spareV} %`, cls });
    }
    if (threshV != null) {
      out.push({ label: "Spare Threshold", value: `${threshV} %` });
    }
    const cw = this._state(hass, e.critical_warning);
    if (cw != null) {
      const n = parseInt(cw);
      const hex = isNaN(n) ? cw : `0x${n.toString(16).padStart(2, "0")}`;
      out.push({ label: "Critical Warning", value: hex, cls: (!isNaN(n) && n > 0) ? "is-crit" : "" });
    }
    const me = this._state(hass, e.media_errors);
    if (me != null) {
      const n = parseFloat(me);
      out.push({ label: "Media Errors", value: this._fmtNumber(me), cls: (!isNaN(n) && n > 0) ? "is-crit" : "" });
    }
    return out;
  }

  _fmtNumber(v) {
    const n = parseFloat(v);
    if (isNaN(n)) return String(v);
    if (Number.isInteger(n)) return n.toLocaleString("en-US");
    return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  _fmtSize(fs) {
    const used = parseFloat(fs.used_gb);
    const total = parseFloat(fs.total_gb);
    if (isNaN(used) || isNaN(total)) return "";
    if (total >= 1024) {
      return `${(used / 1024).toFixed(2)} / ${(total / 1024).toFixed(2)} TB`;
    }
    return `${used.toFixed(1)} / ${total.toFixed(1)} GB`;
  }

  /* Empty state */
  _renderEmpty() {
    const el = document.createElement("div");
    el.className = "ss-empty";
    el.innerHTML = `
      <div class="ss-empty-icon">${EMPTY_STATE_SVG}</div>
      <div class="ss-empty-title">No drives yet</div>
      <div class="ss-empty-text">
        Install the SMART Sniffer integration and start at least one agent.
        Drives appear here as they're discovered.
      </div>`;
    return el;
  }

  /* Loading state */
  _renderLoading() {
    const section = document.createElement("div");
    section.className = "ss-drives-section";
    section.setAttribute("aria-busy", "true");

    const grid = document.createElement("div");
    grid.className = "ss-drives-grid";
    section.appendChild(grid);

    for (let i = 0; i < 4; i++) {
      const chip = document.createElement("div");
      chip.className = "ss-chip is-loading";
      chip.setAttribute("aria-hidden", "true");
      chip.innerHTML = `
        <div class="ss-chip-row">
          <span class="ss-skel" style="width:8px;height:8px;border-radius:50%"></span>
          <span class="ss-skel" style="width:60%;height:12px"></span>
          <span class="ss-skel" style="width:40px;height:11px"></span>
        </div>`;
      grid.appendChild(chip);
    }
    return section;
  }

  /* Utility */
  _esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Visual config editor
   ═══════════════════════════════════════════════════════════════════════════ */
class SmartSnifferCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
  }

  set hass(hass) { this._hass = hass; if (this._config) this._render(); }

  setConfig(config) {
    this._config = {
      title: "Drive Health",
      columns: 2,
      show_ok: true,
      drives: [],
      agents: [],
      usage_warn: 90,
      usage_crit: 95,
      show_storage: true,
      ...config,
    };
    if (this._hass) this._render();
  }

  _render() {
    const cfg = this._config;
    const hass = this._hass;

    // Discover drive-device options for the filter checklist.
    const driveOptions = [];
    const agentOptions = [];
    if (hass?.entities && hass?.devices) {
      const seenDrives = new Set();
      for (const [, entry] of Object.entries(hass.entities)) {
        if ((entry.platform || "").toLowerCase() !== DOMAIN) continue;
        const devId = entry.device_id;
        if (!devId || seenDrives.has(devId)) continue;
        const dev = hass.devices[devId] || {};
        if (NON_DRIVE_DEVICE_MODELS.has(dev.model)) continue;
        seenDrives.add(devId);
        driveOptions.push({ id: devId, name: dev.name || devId });
      }
      driveOptions.sort((a, b) => a.name.localeCompare(b.name));
    }
    if (hass?.config_entries) {
      for (const entry of Object.values(hass.config_entries)) {
        if ((entry.domain || "").toLowerCase() !== DOMAIN) continue;
        agentOptions.push({ id: entry.entry_id, name: entry.title || entry.entry_id });
      }
      agentOptions.sort((a, b) => a.name.localeCompare(b.name));
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; font-family: var(--ha-font-family, Roboto, sans-serif); padding: 0; }
        .field { margin-bottom: 16px; }
        label { display: block; font-size: 12px; color: var(--secondary-text-color); margin-bottom: 5px; font-weight: 500; }
        input[type="text"], input[type="number"], select {
          width: 100%; padding: 8px 10px;
          border: 1px solid var(--divider-color, #ddd);
          border-radius: 6px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 14px;
          box-sizing: border-box;
        }
        .row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
        .row label { margin: 0; font-size: 14px; color: var(--primary-text-color); font-weight: normal; }
        .check-list {
          border: 1px solid var(--divider-color, #ddd);
          border-radius: 6px; max-height: 170px; overflow-y: auto;
        }
        .check-list label {
          display: flex; align-items: center; gap: 8px;
          padding: 7px 10px; font-size: 13px;
          color: var(--primary-text-color);
          font-weight: normal;
          margin: 0;
          border-bottom: 1px solid var(--divider-color, #ddd);
          cursor: pointer;
        }
        .check-list label:last-child { border-bottom: none; }
        .check-list label:hover { background: rgba(0,0,0,0.04); }
        .hint { font-size: 11px; color: var(--secondary-text-color); margin-top: 5px; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
      </style>

      <div class="field">
        <label>Card Title</label>
        <input type="text" data-key="title" value="${this._esc(cfg.title || "Drive Health")}">
      </div>

      <div class="grid-2">
        <div class="field">
          <label>Columns (drives per row)</label>
          <select data-key="columns">
            ${[1,2,3,4].map(n => `<option value="${n}" ${parseInt(cfg.columns) === n ? "selected" : ""}>${n}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label>&nbsp;</label>
          <div class="row">
            <input type="checkbox" id="ed-show-ok" data-key="show_ok" ${cfg.show_ok !== false ? "checked" : ""}>
            <label for="ed-show-ok">Show healthy drives</label>
          </div>
        </div>
      </div>

      <div class="grid-2">
        <div class="field">
          <label>Storage warn threshold (%)</label>
          <input type="number" min="0" max="100" data-key="usage_warn" value="${cfg.usage_warn ?? 90}">
        </div>
        <div class="field">
          <label>Storage critical threshold (%)</label>
          <input type="number" min="0" max="100" data-key="usage_crit" value="${cfg.usage_crit ?? 95}">
        </div>
      </div>

      <div class="row">
        <input type="checkbox" id="ed-show-storage" data-key="show_storage" ${cfg.show_storage !== false ? "checked" : ""}>
        <label for="ed-show-storage">Show storage usage section</label>
      </div>

      ${agentOptions.length > 1 ? `
      <div class="field">
        <label>Filter Agents</label>
        <div class="check-list">
          ${agentOptions.map(a => `
            <label>
              <input type="checkbox" data-agent-id="${this._esc(a.id)}" ${(cfg.agents || []).includes(a.id) ? "checked" : ""}>
              <span>${this._esc(a.name)}</span>
            </label>`).join("")}
        </div>
        <div class="hint">Leave all unchecked to show every agent.</div>
      </div>` : ""}

      <div class="field">
        <label>Filter Drives</label>
        ${driveOptions.length === 0
          ? `<div class="hint">No drives discovered yet. Install the SMART Sniffer integration and start an agent first.</div>`
          : `<div class="check-list">
              ${driveOptions.map(d => `
                <label>
                  <input type="checkbox" data-drive-id="${this._esc(d.id)}" ${(cfg.drives || []).includes(d.id) ? "checked" : ""}>
                  <span>${this._esc(d.name)}</span>
                </label>`).join("")}
            </div>
            <div class="hint">Leave all unchecked to show every drive.</div>`
        }
      </div>`;

    this.shadowRoot.querySelectorAll("[data-key]").forEach(el => {
      el.addEventListener("change", () => {
        const key = el.dataset.key;
        const value = el.type === "checkbox" ? el.checked
                    : el.type === "number"   ? Number(el.value)
                    : el.value;
        this._config = { ...this._config, [key]: value };
        this._fire();
      });
    });
    this.shadowRoot.querySelectorAll("[data-drive-id]").forEach(el => {
      el.addEventListener("change", () => {
        const selected = [];
        this.shadowRoot.querySelectorAll("[data-drive-id]:checked")
          .forEach(cb => selected.push(cb.dataset.driveId));
        this._config = { ...this._config, drives: selected };
        this._fire();
      });
    });
    this.shadowRoot.querySelectorAll("[data-agent-id]").forEach(el => {
      el.addEventListener("change", () => {
        const selected = [];
        this.shadowRoot.querySelectorAll("[data-agent-id]:checked")
          .forEach(cb => selected.push(cb.dataset.agentId));
        this._config = { ...this._config, agents: selected };
        this._fire();
      });
    });
  }

  _fire() {
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: this._config },
      bubbles: true, composed: true,
    }));
  }

  _esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
}

/* ─── Register ────────────────────────────────────────────────────────────
 * NOTE: this card registers itself under TWO custom-element names so it can
 * coexist on the same HA instance with the original bangadrum prototype:
 *
 *   smart-sniffer-card           (canonical name; will be the only name once
 *                                 the prototype is decommissioned)
 *   dablabs-smart-sniffer-card   (uniquely-namespaced alias for side-by-side
 *                                 dashboards during the transition)
 *
 * customElements.define() throws if the name is already taken, so each
 * registration is wrapped to no-op on conflict. Whichever script loads first
 * "wins" the canonical name; the other can still be used via the alias. */

function _safeDefine(name, ctor) {
  try {
    if (!customElements.get(name)) customElements.define(name, ctor);
  } catch (e) {
    console.warn(`smart-sniffer-card: skipping define("${name}"):`, e?.message || e);
  }
}

_safeDefine("smart-sniffer-card",         SmartSnifferCard);
_safeDefine("smart-sniffer-card-editor",  SmartSnifferCardEditor);
_safeDefine("dablabs-smart-sniffer-card", SmartSnifferCard);
_safeDefine("dablabs-smart-sniffer-card-editor", SmartSnifferCardEditor);

window.customCards = window.customCards || [];

// Only push catalog entries for tags WE successfully registered (not ones
// that were already taken by another script). Otherwise users see duplicate
// entries that point to the wrong card.
function _pushCardEntry(type, namePrefix) {
  if (!customElements.get(type)) return;
  // Avoid duplicate catalog entries when this script is loaded twice.
  if (window.customCards.some(c => c.type === type)) return;
  window.customCards.push({
    type,
    name: `${namePrefix}`,
    description: `Drive health dashboard for the SMART Sniffer integration (v${VERSION})`,
    preview: false,
  });
}

_pushCardEntry("smart-sniffer-card",         "SMART Sniffer Card");
_pushCardEntry("dablabs-smart-sniffer-card", "SMART Sniffer Card (DAB-LABS v1.0)");

console.info(
  `%c SMART-SNIFFER-CARD %c v${VERSION} (DAB-LABS) `,
  "background:#41BDF5;color:#fff;font-weight:700;padding:2px 4px;border-radius:3px 0 0 3px",
  "background:#1A1A1A;color:#41BDF5;font-weight:500;padding:2px 4px;border-radius:0 3px 3px 0"
);
