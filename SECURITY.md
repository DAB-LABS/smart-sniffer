# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in SMART Sniffer, please report it responsibly.

**Email:** dbailey@live.com

**Do not** open a public GitHub issue for security vulnerabilities. Please use email so we can assess and address the issue before public disclosure.

## What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

## Scope

SMART Sniffer handles bearer token authentication between the agent and Home Assistant integration. Security-relevant areas include:

- **Token handling** in the Go agent (`agent/main.go`) and HA integration (`coordinator.py`, `config_flow.py`)
- **HTTP server** exposure — the agent listens on a configurable port and serves drive health data
- **Installer scripts** (`install.sh`, `install.ps1`) — these run with elevated privileges and download binaries from GitHub

## Response

We aim to acknowledge reports within 48 hours and provide a fix or mitigation plan within 7 days for confirmed vulnerabilities.
