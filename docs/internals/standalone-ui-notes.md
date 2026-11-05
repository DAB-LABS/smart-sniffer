# Standalone UI notes

Notes from the design call on serving SMART Sniffer to homelabbers without Home Assistant. Lookup and decisions only; no implementation yet.

## 1. Goal

Give homelabbers a way to use SMART Sniffer **without Home Assistant**.

Today the product surface is HA-centric: the Go agent (`smartha-agent`) exposes a REST API, and the Control Center / Lovelace card live inside the Home Assistant integration. Community Docker packaging for a standalone option exists but is **stale** and not something to lean on.

Target users still run the same agent on NAS boxes, Proxmox hosts, etc.; they need a first-class web UI pointed at that API.

## 2. Decision: embed the frontend in the agent

**Ship the UI inside the Go agent binary** with `go:embed`.

| Aspect | Choice |
|--------|--------|
| Deliverable | One binary (`smartha-agent`) |
| Port | One port (default **9099**) |
| Routes | UI at `/`, API at `/api/*` |
| Docker | Optional packaging, not required |

Rationale: the agent already owns an `net/http` `ServeMux` for `/api/*`. Embedding static assets keeps the install story identical (install script → service → open the host:port in a browser). No second container or reverse-proxy sidecar required for the common case.

## 3. Existing HA panel cannot be reused as-is

The Control Center under `custom_components/smart_sniffer/panel/` is the right **look and feel**, not the right **data layer**.

- `panel/cc.js` talks to Home Assistant over the websocket bridge (`panel.js` → `smart_sniffer/panel/*`). It does not call the agent REST API.
- Attention verdicts, card “views,” thresholds, fleet, replicas, and Agentic export are produced or stored by the integration, then pushed to the page.
- A new frontend should **reuse** `cc.css`, Alpine, brand assets, and card chrome, but **poll the agent REST API** (and any new agent-side verdicts endpoint) instead of `BRIDGE.callWS`.

## 4. Agent API (as it stands)

| Fact | Detail |
|------|--------|
| Language / form | Go 1.25, statically linked single binary (`CGO_ENABLED=0` on Linux) |
| Entry | `agent/main.go`; listens on `0.0.0.0:<port>` (default **9099**) |
| Shape | **GET-only** JSON |
| Auth | Optional bearer token (`Authorization: Bearer …`); empty token = open |
| CORS / TLS | **Neither** today |
| Packaging | `install.sh` / `install.ps1` → systemd / launchd / Windows service; no official Dockerfile |

### Endpoints

| Endpoint | Auth | Notes |
|----------|------|--------|
| `GET /api/health` | **Always public** | Uptime, version, hostname, OS, endpoint list, drive/fs/pool counts, `agent_id` |
| `GET /api/drives` | If token set | Drive summaries |
| `GET /api/drives/{id}` | If token set | Full drive: raw smartctl JSON (`smart_data`), `device_statistics`, derived TB written/read, standby / readability flags |
| `GET /api/filesystems` | If token set | Only when `filesystems:` is configured; otherwise **404** |
| `GET /api/pools` | If token set | Only when ZFS pool status is on; otherwise missing / unavailable |

## 5. Predictability scoring today

“Predictability” (Attention Needed: reallocated/pending sectors, uncorrectables, NVMe spare/wear, temperature-adjacent rules, etc.) lives in the **HA Python** module `custom_components/smart_sniffer/attention.py`.

- `panel_view.py` wraps those rules into per-card verdicts and row marks for the Control Center.
- `panel/cc.js` **only displays** what the integration already judged; it does not score drives itself.
- Pool health rules are similarly in `pool_health.py` (also HA-side, no HA imports in the rule modules themselves).

Implication for standalone: the agent API returns raw SMART; without something new, a browser UI cannot match HA’s NO / MAYBE / YES behavior.

## 6. Decision: agent verdicts **and** client-side fallback

Do **both**:

1. **Source of truth:** add `GET /api/verdicts` (or equivalent) on the Go agent, implementing the same scoring rules so HA and standalone agree over time.
2. **Fallback:** client-side JS scoring for **older agents** that do not expose the endpoint. A **404** on `/api/verdicts` triggers local scoring against `/api/drives/{id}` payloads.

This keeps new installs simple (one round-trip to the agent) while older binaries on the LAN still get badges in the embedded UI.

## 7. Machine-readable discovery for AI agents

Serve an **OpenAPI spec** and/or **`llms.txt`** at a **known path** on the agent (exact path TBD; e.g. `/openapi.json` and/or `/llms.txt`).

`/api/health` already lists available endpoints and counts; keep that as the lightweight discovery signal, and point richer clients at the spec / llms document for schemas and usage.

## 8. Usage shapes

| Audience | How they run it |
|----------|-----------------|
| Power users | Official install script → systemd / launchd / Windows service; open `http://host:9099/` |
| Docker users | Want a **one-liner** image/compose (optional; not the primary path) |
| Source builds | Not expected except niches (Nix, TrueNAS-style packaging) |

One embedded binary serves both “I installed the agent” and “I also want the UI” without a second artifact.

## 9. Auth and network exposure

Outside HA’s trusted LAN assumptions:

- **Require a bearer token** whenever the agent is reachable beyond localhost.
- **Do not publish port 9099 to the internet.** Prefer LAN, Tailscale, or VPN.
- If TLS is needed, terminate it on a reverse proxy in front of the agent (agent itself remains plain HTTP for now).
- Remember `/api/health` stays unauthenticated and **leaks** version, hostname, and drive/fs/pool counts — fine for local healthchecks, not for a public edge.

The embedded UI and `/api/*` share one origin on 9099, so browser CORS is not required for same-host use. If the UI ever were split onto another origin, CORS and token handling would need an explicit design (prefer keeping them together via embed).

## 10. Open questions

1. **Verdicts response shape** — exact JSON for `/api/verdicts` (per-drive state/severity/reasons? pool verdicts? filesystem warnings? card-view row marks or state only?). Must stay aligned with `attention.py` / `panel_view.py` over time.
2. **Threshold config outside HA** — today per-drive “accept up to N” overrides live on the HA config entry. Standalone needs a place for the same knobs (agent `config.yaml`? UI-editable file next to the agent? read-only defaults only until HA is present?).

---

*Captured for implementation planning. Related code: `agent/main.go`, `custom_components/smart_sniffer/attention.py`, `panel_view.py`, `panel/cc.js`.*
