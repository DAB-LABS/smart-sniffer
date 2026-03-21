# Agent Version Check & HA Repair

When a SMART Sniffer integration update requires features or fixes from a newer agent, users need a clear signal inside Home Assistant telling them to upgrade — not a mysterious failure or silent degradation. This document describes the design for version-aware repair notifications.

## The Problem

The integration and agent are updated independently. A user might update the HA integration via HACS but forget to re-run the installer on their host machines. If the new integration relies on agent-side changes (new API fields, mDNS TXT records, interface filtering), things break silently or with confusing errors.

## How Version Info Flows Today

The agent already has version plumbing in place:

| Channel | Field | Current Value | Read by Integration? |
|---------|-------|---------------|---------------------|
| mDNS TXT record | `version=0.1.0` | Set at build time via `-ldflags` | ❌ Not yet |
| `/api/health` HTTP endpoint | `{"status":"ok"}` | No version field | ❌ N/A |
| `/api/drives` HTTP endpoint | drive list | No version field | ❌ N/A |

The mDNS TXT record has the version, but the integration ignores it. The HTTP API doesn't return version at all, so the integration can't check version during ongoing polling — only at initial discovery.

## Design

### Agent Changes

**`/api/health` returns version** — one small addition to `handleHealth` in `main.go`:

```go
func handleHealth(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "application/json")
    fmt.Fprintf(w, `{"status":"ok","version":"%s"}`, version)
}
```

This is the lightest possible change. The `version` variable is already set at build via `-ldflags`. The `/api/health` endpoint is already called during config flow validation, and adding it to the coordinator poll is trivial (one extra GET per cycle, tiny payload).

### Integration Changes

**1. New constants in `const.py`:**

```python
MIN_AGENT_VERSION = "0.1.0"       # bump when a release requires agent changes
AGENT_RELEASES_URL = "https://github.com/DAB-LABS/smart-sniffer/releases"
```

`MIN_AGENT_VERSION` is the minimum agent version that the current integration release can work with correctly. When we ship an integration change that depends on agent-side work, we bump this constant.

**2. Version comparison helper:**

```python
def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse '0.1.0' → (0, 1, 0) for comparison."""
    return tuple(int(x) for x in v.split("."))

def _agent_is_outdated(agent_version: str, min_version: str) -> bool:
    """Return True if agent_version < min_version."""
    try:
        return _version_tuple(agent_version) < _version_tuple(min_version)
    except (ValueError, AttributeError):
        return False  # don't raise repair on unparseable versions
```

Strict semver libraries are overkill — we control both version strings and they'll always be `major.minor.patch`.

**3. Coordinator checks version on each poll (`coordinator.py`):**

After the existing drive fetch, add a `/api/health` call:

```python
from homeassistant.helpers.issue_registry import (
    async_create_issue,
    async_delete_issue,
    IssueSeverity,
)

# Inside _async_update_data, after the drive fetch succeeds:
async with session.get(
    f"{self._base_url}/api/health",
    headers=self._headers,
    timeout=timeout,
) as resp:
    resp.raise_for_status()
    health = await resp.json()

agent_version = health.get("version", "")
self._check_agent_version(agent_version)
```

The check method:

```python
def _check_agent_version(self, agent_version: str) -> None:
    """Create or clear a repair issue based on agent version."""
    issue_id = f"agent_outdated_{self.host}"

    if not agent_version or _agent_is_outdated(agent_version, MIN_AGENT_VERSION):
        async_create_issue(
            self.hass,
            domain=DOMAIN,
            issue_id=issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="agent_outdated",
            translation_placeholders={
                "hostname": self._hostname or self.host,
                "current_version": agent_version or "unknown",
                "min_version": MIN_AGENT_VERSION,
            },
            learn_more_url=AGENT_RELEASES_URL,
        )
    else:
        async_delete_issue(self.hass, DOMAIN, issue_id)
```

Key details:
- `issue_id` is per-host so each agent gets its own repair card.
- `is_fixable=False` — the user fixes this outside HA (re-running the installer on the host). No point offering a "fix" button.
- `async_delete_issue` is idempotent and silently no-ops if the issue doesn't exist, so calling it every poll when the version is fine costs nothing.
- If the agent is so old that `/api/health` doesn't return a `version` field, `agent_version` will be `""`, which triggers the repair. This covers truly ancient agents.

**4. Config flow warns at discovery (`config_flow.py`):**

In `async_step_zeroconf`, read the version from mDNS TXT (it's already broadcast, just not consumed):

```python
agent_version = properties.get("version", "")
if agent_version and _agent_is_outdated(agent_version, MIN_AGENT_VERSION):
    self._agent_outdated_warning = True
    self._agent_version = agent_version
```

Then in the `zeroconf_confirm` step description, conditionally show a warning. This doesn't block setup — the user can still add the device — but they see a heads-up before they even finish the flow.

**5. Translation strings (`strings.json`):**

Add a top-level `"issues"` section (HA's standard location for repair strings):

```json
{
  "config": { ... },
  "options": { ... },
  "issues": {
    "agent_outdated": {
      "title": "SMART Sniffer agent on {hostname} needs an update",
      "description": "The agent is running version **{current_version}**, but this integration requires at least **{min_version}**.\n\nTo update, SSH into the host and run:\n```\ncurl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash\n```\nThe installer will detect the existing installation and upgrade in place. This repair will clear automatically once the agent reports a compatible version."
    }
  }
}
```

The same structure goes into `translations/en.json`.

## Auto-Resolution

This is the best part — no user action needed inside HA. Once the user upgrades the agent on their host machine:

1. Next poll cycle, coordinator calls `/api/health`.
2. New version meets `MIN_AGENT_VERSION`.
3. `async_delete_issue()` fires, repair card vanishes.

No restarts, no reconfiguration, no "mark as resolved" button. It just goes away.

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Agent so old it has no `version` in `/api/health` | `health.get("version", "")` returns `""` → treated as outdated → repair raised |
| Agent has `version` in mDNS but not HTTP (pre-this-change) | mDNS warning shows at discovery; coordinator can't check ongoing → repair raised (empty version) |
| Multiple agents, one outdated | Each gets its own repair card (`issue_id` includes host) |
| Agent temporarily unreachable | Coordinator raises `UpdateFailed` (existing behavior), no version check runs, no spurious repair |
| Unparseable version string (e.g., `"dev"`) | `_agent_is_outdated` returns `False` → no repair (benefit of the doubt for dev builds) |
| Agent newer than integration expects | Totally fine — we only check `<`, not exact match. Forward compatible. |

## Severity Levels (Future)

Starting with a single `WARNING` severity is enough. If we later need to distinguish "outdated but functional" from "so old it's broken," we can add a second constant:

```python
MIN_AGENT_VERSION = "0.2.0"          # soft minimum — warning
MIN_AGENT_VERSION_HARD = "0.1.0"     # hard minimum — error, blocks setup
```

The hard minimum would use `IssueSeverity.ERROR` and could additionally prevent the config flow from completing for truly incompatible agents. Not needed today — leaving this as a note for future-us.

## Files to Change

| File | Change |
|------|--------|
| `agent/main.go` | Add `"version"` field to `handleHealth` JSON response |
| `custom_components/smart_sniffer/const.py` | Add `MIN_AGENT_VERSION`, `AGENT_RELEASES_URL` |
| `custom_components/smart_sniffer/coordinator.py` | Import issue_registry, add `/api/health` call + `_check_agent_version()` |
| `custom_components/smart_sniffer/config_flow.py` | Read `version` from mDNS TXT, warn if outdated in zeroconf_confirm |
| `custom_components/smart_sniffer/strings.json` | Add `issues.agent_outdated` title + description |
| `custom_components/smart_sniffer/translations/en.json` | Same |
| `CHANGELOG.md` | Document the feature |

## Implementation Notes

- The `/api/health` call adds negligible overhead — it's a ~50 byte JSON response with no disk I/O.
- The version check runs every poll cycle, but `async_create_issue` and `async_delete_issue` are both idempotent and cheap — they're in-memory registry operations.
- The `learn_more_url` on the repair card links directly to GitHub Releases so the user can see what changed.
- The curl one-liner installer is idempotent — it detects existing installs and upgrades in place, preserving config.yaml.
