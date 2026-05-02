# Contributing to the SMART Sniffer Card

This is the developer-side guide for anyone who wants to modify, extend, or fix bugs in the card. If you're a user looking to install or configure the card, see the [README](README.md).

## How the card is structured

`smart-sniffer-card.js` is a single self-contained file. There is no build step. Users register it as a Lovelace resource and Home Assistant loads it directly.

The file is organized top-to-bottom in render order:

1. **Constants** (`VERSION`, `DOMAIN`, `ENTITY_PATTERNS`, `NON_DRIVE_DEVICE_MODELS`, `SORT_ORDER`).
2. **Styles** (`STYLES` template literal). All CSS lives here. Tokens are defined first, components later.
3. **Inline SVGs** (`BRAND_MAGNIFIER_SVG`, `EMPTY_STATE_SVG`).
4. **`SmartSnifferCard` class** (the main element). HA card protocol, change detection, collection, render.
5. **`SmartSnifferCardEditor` class** (the visual editor). Independent custom element.
6. **Registration** at the bottom (`customElements.define(...)`, `window.customCards.push(...)`).

Within the main class, methods are grouped by phase:

- HA protocol methods (`setConfig`, `set hass`, `getCardSize`, `getConfigElement`, `getStubConfig`).
- Change detection (`_shouldUpdate`).
- Collection (`_collect`, `_buildDrive`, `_collectFilesystems`, plus their helpers). This is the data layer: it walks `hass.entities` / `hass.devices` / `hass.config_entries` and produces normalized `drive` and `filesystem` objects.
- Render (`_render` and the `_render*` helpers). This is the view layer: it consumes the normalized objects and produces DOM.

Keep the data layer and view layer separate. The render methods should never reach back into `hass` directly; they should consume what `_collect` produced.

## How to make common changes

### Adding a new metric to the detail view

Decide whether the metric is Common, ATA-only, or NVMe-only. Then:

1. Add a regex to `ENTITY_PATTERNS` that matches the metric's entity_id suffix. Example: `my_new_metric: /_my_new_metric$/`.

2. In `_collectCommonMetrics`, `_collectAtaMetrics`, or `_collectNvmeMetrics`, add a line that reads the entity and pushes to the `out` array. Use the existing pattern:

```js
const v = this._state(hass, e.my_new_metric);
if (v != null) out.push({ label: "My New Metric", value: `${v} unit`, cls: "is-warn" });
```

3. The `cls` field can be `"is-ok"`, `"is-warn"`, `"is-crit"`, or `""`. **Don't invent your own thresholds.** If the integration treats the metric as severity-relevant, the integration's `Attention Reasons` will already say so. The detail-view coloring is display reinforcement, not source of truth.

That's it. The metric will appear automatically in the protocol section you added it to, will render with the chosen severity color, and will be skipped on drives where the entity doesn't exist.

### Adding a new visual state

A new visual state means a new entry in the chip-state decision tree. Common candidates: "drive in self-test," "drive being securely erased," etc.

1. Add a new key to `SORT_ORDER` deciding where the state slots in the worst-first sort.
2. In `_buildDrive`, add a branch to the state-decision logic. Where in the tree it sits matters: states earlier in the tree override later ones (e.g., "stale" overrides every attention state because we don't trust the data).
3. In `_stateClass` and `_dotClass`, decide what visual stripe and dot the new state gets. Reuse existing tokens (`--ss-warn`, `--ss-crit`, etc.) unless the brand actually needs a new one (it usually doesn't).
4. In `_stateAriaLabel`, write the screen-reader label for the new state.
5. If the new state needs special copy in the chip's metric or context line, extend `_chipMetric` and `_chipContext`.
6. If the new state needs special copy in the detail view's Attention block, extend `_attentionStateWord`.
7. Add the state to the README's "Severity logic" section.

If the new state needs a NEW token (e.g., a purple stripe), add it in the `:host` token block AND in the dark-mode `@media` block, with values that satisfy WCAG 2.1 AA contrast against both light and dark surfaces.

### Changing severity thresholds

Don't. The card should never have its own thresholds. Severity is decided by `custom_components/smart_sniffer/attention.py` in the integration. If you want to change what counts as critical or watch, change the integration. The card will follow because it reads the integration's `Attention Needed` enum.

The single exception is the Disk Usage section's `usage_warn` / `usage_crit`, which ARE card-side because filesystem usage is not part of the `Attention Needed` aggregate. Those have explicit config knobs so users can override them per-card.

### Changing the brand magnifier icon

The current icon is a geometric placeholder defined as `BRAND_MAGNIFIER_SVG`. It uses `var(--ss-brand-blue)` for the lens stroke and house silhouette so the brand color is consistent with the rest of the card.

The v1.1 plan is to replace it with a "Spy v Spy" variant: same lens and handle, but with a tiny spy-head silhouette inside the lens. The design prompt is in `DABLABS-public-relations/image-prompts.md`. Whoever does the swap should keep the SVG inline (don't add a binary asset to the card) and keep `currentColor` semantics on the handle so the icon adapts to surrounding text color in the empty-state context.

### Adding a config option

1. Add the option to `setConfig`'s default merge.
2. Validate / normalize it in the same place.
3. Use it where it should take effect (probably in `_render`).
4. Add it to the visual editor in `SmartSnifferCardEditor._render` with a matching `data-key` attribute. The editor's generic `[data-key]` change handler will pick it up automatically.
5. Add it to the README's "Full config schema" table.

## Things to NOT do

- **Don't add a build step.** No webpack, no rollup, no TypeScript, no LitElement. The single-file no-build-step distribution is a feature, not a debt.
- **Don't add `localStorage` or other browser storage.** HA users' state lives in HA, not in browser-local-anything.
- **Don't reach into HA's internal CSS classes.** The card lives in shadow DOM specifically so it doesn't depend on HA's class names. Use HA's documented CSS variables only.
- **Don't reinvent severity.** Read `Attention Needed` and `Attention Reasons`. Always.
- **Don't ship hardcoded colors.** The brand blue is the only exception. Everything else uses tokens that defer to HA theme variables.
- **Don't use em-dashes in user-facing strings.** Project-wide rule from `brand-voice.md`. The card has zero em-dashes today; keep it that way.
- **Don't add looping animations.** A single subtle pulse on the loading skeleton is the entire motion budget.
- **Don't add emoji to the card source.** Every emoji that appeared in the bangadrum prototype was removed deliberately.

## Testing

There is no automated test suite for the card today. Manual testing on a real Home Assistant instance is the floor.

### Minimum manual test matrix

1. **Fresh install:** new HA instance, install the integration, install the card, verify default render is the empty state followed by the loading state followed by drives appearing.
2. **Healthy drive:** at least one drive in `Attention NO` state. Verify green dot, lighter-grey stripe, no banner.
3. **Watch drive:** at least one drive in `Attention MAYBE` state. Verify amber dot, amber stripe, watch banner with the drive name.
4. **Critical drive:** at least one drive in `Attention YES` state. Verify red dot, red stripe, critical banner with the drive name.
5. **Mixed:** at least one critical and one watch on the same card. Critical banner should win.
6. **Unsupported:** at least one drive in `Attention UNSUPPORTED` state (USB enclosures with passthrough disabled work for this). Verify hollow grey dot, "no data" metric, "No usable S.M.A.R.T. data" reason line.
7. **Standby:** at least one drive in standby (test by stopping a spinning drive's polling and waiting). Verify "cached Xh Ym" affordance and "in standby" subline.
8. **Agent offline:** stop the agent process. Verify all drives on that agent flip to grey-stripe / hollow-dot / "agent offline" within one HA poll cycle. Critical drives on the offline agent should still show as stale, not critical.
9. **Multi-agent:** at least two configured agents. Verify "on AGENT" labels appear on every chip.
10. **Detail view:** click a chip. Verify it expands to full grid width, the detail panel sits directly below it, no other chip sits beside it. Verify the Attention block leads with the state word and reasons text. Verify the diagnostic grid splits Common / ATA or Common / NVMe correctly.
11. **Disk Usage section:** at least one agent with filesystem monitoring enabled. Verify the section appears with one row per mountpoint, bar fill percentage matches the integration's reported value, and crossing the 90 / 95 thresholds turns the bar amber / red.
12. **Light theme:** switch HA to a light theme, hard-refresh, verify all chrome reads correctly with no dark-on-dark or hardcoded-dark issues.
13. **Mobile width:** resize browser below 380px wide, verify single-column grid and condensed single-line stats strip.
14. **Filter to one agent:** in the visual editor, tick one agent in "Filter Agents." Verify only that agent's drives appear AND the stats strip math sums correctly to just those drives.
15. **Hide healthy:** tick `show_ok: false`. Verify healthy drives disappear but stats strip total still shows the real count.

If you find a regression, fix it AND add a regression test scenario above before merging.

### Browser console

The card logs `SMART-SNIFFER-CARD v1.0.0` (with the version current at load) to the console on initialization. If you don't see that line, your resource registration is broken or your browser cache is stale. Hard-refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`).

The card does not currently emit any telemetry, error reporting, or analytics. If you want to add development-time assertions (e.g., the stats-strip math check), gate them behind a `if (window.__SS_DEV)` flag so production users don't see noisy console output.

## Code style

Match the existing file. Highlights:

- Two-space indentation.
- Semicolons.
- `const` by default, `let` where reassignment is needed, no `var`.
- Trailing commas in multi-line literals.
- Method names start with `_` for internal, no underscore for HA protocol methods.
- HTML in template literals uses inline `${}` expressions for values, never string concatenation. Always wrap user-derived values in `this._esc()`.
- CSS uses tokens (`var(--ss-foo)`) not raw color values. The brand blue is the only exception, and even it has a token (`--ss-brand-blue`).

## How to update the version

1. Bump the `VERSION` constant at the top of `smart-sniffer-card.js`.
2. Update `CHANGELOG.md` with the new entry.
3. Tag the release in git: `git tag card-v1.0.1` (note: the card's tag is namespaced separate from the integration's tag).

The `customCards.push()` description string at the bottom uses the `VERSION` constant, so it's automatically in sync.

## Where to ask questions

- Bug reports and feature requests: open an issue in the main `smart-sniffer` repo with the `card` label.
- Architecture questions / proposing larger changes: open a Discussion thread first.
- Quick clarifications: same place. We're a small project; over-formality slows things down.
