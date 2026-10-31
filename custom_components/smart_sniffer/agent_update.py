"""Agent update notices (v1.0, #68 phase 1): what to tell a user whose agent
is behind the newest release, and how that machine is updated.

Home Assistant never installs an agent. The notice gives the install
command for the machine's OS, as the agent reports it in /api/health: the
curl one-liner on Linux and macOS, the irm one-liner on Windows. An agent in
a container (the "container" health field, v1.0 agents) or on an OS the
installers do not cover is pointed to the docs instead.

No Home Assistant imports, so the tests load this module directly.
"""

from __future__ import annotations

from typing import Any

from .agent_version import agent_is_outdated, display_version
from .const import (
    AGENT_DOCKER_DOCS_URL,
    AGENT_INSTALL_DOCS_URL,
    AGENT_INSTALL_PS1_URL,
    AGENT_INSTALL_URL,
    LATEST_AGENT_NOTES,
    LATEST_AGENT_VERSION,
)

SUMMARY_LIMIT = 255  # what Home Assistant keeps of an update entity's summary

CURL_COMMAND = f"curl -sSL {AGENT_INSTALL_URL} | sudo bash"
IRM_COMMAND = f"irm {AGENT_INSTALL_PS1_URL} | iex"

UNIX = ("linux", "darwin")


def agent_behind(installed: Any, latest: str = LATEST_AGENT_VERSION) -> bool:
    """Whether an agent reporting ``installed`` is older than ``latest``.
    False when either cannot be read (a "dev" build, no answer yet)."""
    return bool(display_version(installed)) and agent_is_outdated(installed, latest)


def install_hint(os_name: Any, container: Any = False) -> dict[str, str | None]:
    """How the machine is updated: {"kind", "command", "docs"}.

    kind is "unix" (curl), "windows" (irm), "docker" or "unknown"; command
    is None for the last two, which carry a docs link only.
    """
    os_key = str(os_name or "").strip().lower()
    if container is True:
        return {"kind": "docker", "command": None, "docs": AGENT_DOCKER_DOCS_URL}
    if os_key in UNIX:
        return {"kind": "unix", "command": CURL_COMMAND, "docs": AGENT_INSTALL_DOCS_URL}
    if os_key == "windows":
        return {"kind": "windows", "command": IRM_COMMAND, "docs": AGENT_INSTALL_DOCS_URL}
    return {"kind": "unknown", "command": None, "docs": AGENT_INSTALL_DOCS_URL}


def release_summary(os_name: Any, container: Any = False,
                    notes: str = LATEST_AGENT_NOTES) -> str:
    """The update entity's summary: what is new, then how to update this
    machine. Never longer than Home Assistant keeps."""
    hint = install_hint(os_name, container)
    if hint["kind"] == "unix":
        how = f"To update, run on this machine: {hint['command']}"
    elif hint["kind"] == "windows":
        how = f"To update, run in PowerShell as Administrator: {hint['command']}"
    elif hint["kind"] == "docker":
        how = f"This agent runs in a container: rebuild it from the new release. {hint['docs']}"
    else:
        how = f"See the install guide: {hint['docs']}"
    text = f"{notes} {how}".strip()
    if len(text) > SUMMARY_LIMIT:
        # The command matters more than the notes: shorten the notes.
        room = SUMMARY_LIMIT - len(how) - 4
        text = f"{notes[:max(room, 0)].rstrip()}... {how}" if room > 0 else how[:SUMMARY_LIMIT]
    return text
