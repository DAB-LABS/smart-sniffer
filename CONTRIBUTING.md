# Contributing to SMART Sniffer

Thanks for your interest in contributing! SMART Sniffer is in beta and we welcome bug reports, drive compatibility data, and code contributions.

## Reporting Bugs

[Open an issue](https://github.com/DAB-LABS/smart-sniffer/issues) with:

- What you expected to happen vs. what actually happened
- Your setup: OS, HA version, agent version, how the agent was installed
- Relevant logs from the agent (`journalctl -u smartha-agent -f` on Linux) or HA (Settings > System > Logs)

## Submitting Drive Data

The most valuable contribution you can make is a `smartctl` dump from a drive we haven't tested against. This helps us catch manufacturer-specific attribute name variants that would otherwise be invisible.

```bash
sudo smartctl -a --json /dev/sdX > my-drive-dump.json
```

Attach the JSON file to an issue or PR. Feel free to redact the serial number if you prefer. The fields we care about most are the attribute names and the JSON structure, not identifying information.

See [docs/smart-attribute-name-variants.md](docs/smart-attribute-name-variants.md) for the current mapping and known gaps.

## Development Setup

### Agent (Go)

```bash
cd agent
go build -o smartha-agent .
sudo ./smartha-agent --port 9099
```

Requires Go 1.22+ and `smartmontools` installed.

### Integration (Python / Home Assistant)

Copy `custom_components/smart_sniffer/` into your HA development instance's `custom_components/` directory. Restart HA to pick up changes.

For testing without real drives, use the [Mock Agent](docs/mock-agent.md):

```bash
python3 tools/mock-agent.py --port 9100 --preload sata_hdd,nvme,usb_blocked
```

### Key Files

| File | What it does |
|------|-------------|
| `agent/main.go` | HTTP server, smartctl execution, caching, mDNS |
| `agent/config.go` | Config loading (YAML + CLI flags) |
| `custom_components/smart_sniffer/attention.py` | Attention state classification logic |
| `custom_components/smart_sniffer/sensor.py` | All sensor entities + extraction |
| `custom_components/smart_sniffer/coordinator.py` | Data polling + notification lifecycle |
| `custom_components/smart_sniffer/config_flow.py` | HA config flow + Zeroconf discovery |

## Pull Requests

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test against the mock agent if touching the integration
4. Test with `go build` if touching the agent
5. Open a PR with a clear description of what and why

Keep PRs focused — one feature or fix per PR. If you're planning something large, open an issue first to discuss the approach.

## Code Style

- **Go agent:** Standard `gofmt` formatting
- **Python integration:** Follow existing patterns in the codebase. Type hints are used throughout. No external dependencies (stdlib + HA core only).

## What's Needed

Check the [roadmap](README.md#roadmap) and [known issues](docs/build-journal.md#known-issues--tech-debt) for ideas. Some areas where help is especially welcome:

- **SAS/SCSI drive support** — we need `smartctl -a --json` dumps from SAS drives
- **Drive-specific `smartctl` dumps** — any manufacturer or model we haven't seen

## Updating llms.txt

`llms.txt` in the repo root is a machine-readable project summary used by LLMs and
AI-powered search to accurately describe and recommend this project. Keep it current.

**Update `llms.txt` when your PR:**
- Modifies `README.md` in a way that affects features, capabilities, supported
  platforms, configuration options, or documentation structure
- Adds a new feature or ships a roadmap item
- Changes platform support status (e.g., a platform moves from untested to tested)
- Adds a new doc file to `docs/` that belongs in the Documentation section

**Format rules:**
- Follow the llmstxt.org spec: H1, blockquote summary, body, H2 link sections
- Keep the file under ~200 lines
- No em-dashes -- use double-hyphens (--), parentheses, or separate sentences
- Every claim must be verifiable against README or CHANGELOG
