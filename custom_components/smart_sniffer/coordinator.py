"""DataUpdateCoordinator for SMART Sniffer.

Polls the smartha-agent REST API at the configured interval and caches the
result so that all entities read from a single, consistent snapshot.

Persistent notifications
------------------------
After each successful poll the coordinator evaluates attention state for
every drive (using the shared attention.py module) and compares it to the
previous state. When a drive's state changes it fires or dismisses a HA
persistent_notification automatically — no user automation required.

Transition rules:
  first poll         Record baseline, no notification.
  NO  → MAYBE       Fire ⚠️ WARNING notification.
  NO  → YES         Fire 🔴 CRITICAL notification.
  MAYBE → YES       Update notification to escalate.
  YES → MAYBE       Update notification to de-escalate.
  YES/MAYBE → NO    Dismiss notification (resolved).
  * → UNSUPPORTED   Fire ℹ️ informational notification (once).
  UNSUPPORTED → *   Dismiss informational notification.

Notification IDs are stable: smart_sniffer_attention_{drive_id}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.persistent_notification import (
    async_create as pn_create,
    async_dismiss as pn_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .attention import (
    SEVERITY_CRITICAL,
    STATE_MAYBE,
    STATE_NO,
    STATE_UNSUPPORTED,
    STATE_YES,
    evaluate_attention,
)
from .const import (
    AGENT_RELEASES_URL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_AGENT_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_NOTIF_PREFIX = "smart_sniffer_attention_"


def _notif_id(drive_id: str) -> str:
    return f"{_NOTIF_PREFIX}{drive_id}"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse '0.4.28' → (0, 4, 28) for comparison."""
    return tuple(int(x) for x in v.split("."))


def _agent_is_outdated(agent_version: str, min_version: str) -> bool:
    """Return True if agent_version < min_version."""
    try:
        return _version_tuple(agent_version) < _version_tuple(min_version)
    except (ValueError, AttributeError):
        return False  # don't raise repair on unparseable versions (e.g. "dev")


def _build_notification(
    drive_data: dict[str, Any],
    state: str,
    severity: str,
    reasons: list[str],
) -> tuple[str, str]:
    """Return (title, message) for a persistent notification."""
    model  = drive_data.get("model", "Unknown Drive")
    serial = drive_data.get("serial", "")
    label  = f"{model} ({serial})" if serial else model

    if state == STATE_UNSUPPORTED:
        title   = f"ℹ️ SMART Monitoring Unavailable — {label}"
        message = (
            "SMART Sniffer cannot read health data from this drive. "
            "This commonly happens with USB enclosures that block SMART "
            "passthrough. Health monitoring is not available for this drive."
        )
        return title, message

    if severity == SEVERITY_CRITICAL:
        icon    = "🔴"
        urgency = "**CRITICAL — Back up your data immediately.**"
    else:
        icon    = "⚠️"
        urgency = "**WARNING — Monitor closely and plan for replacement.**"

    bullet_list = "\n".join(f"• {r}" for r in reasons)
    title   = f"{icon} Drive Attention Required — {label}"
    message = f"{urgency}\n\n{bullet_list}"
    return title, message


class SmartSnifferCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch SMART drive data from the agent and make it available to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.host:  str = entry.data[CONF_HOST]
        self.port:  int = entry.data[CONF_PORT]
        self.token: str = entry.data.get(CONF_TOKEN, "")
        interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        # Hostname for display in repair notifications — use the title if
        # it looks like "SMART Sniffer (hostname)", otherwise fall back to IP.
        title = entry.title or ""
        if title.startswith("SMART Sniffer (") and title.endswith(")"):
            self._hostname: str = title[len("SMART Sniffer ("):-1]
        else:
            self._hostname = self.host

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

        # Track the last known attention state and reasons per drive for
        # transition detection. None = drive not yet seen (first poll baseline).
        self._prev_state: dict[str, str | None] = {}
        self._prev_reasons: dict[str, list[str]] = {}

    @property
    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def _headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _check_agent_version(self, agent_version: str) -> None:
        """Create or clear a HA repair issue based on agent version."""
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
                    "hostname": self._hostname,
                    "current_version": agent_version or "unknown",
                    "min_version": MIN_AGENT_VERSION,
                },
                learn_more_url=AGENT_RELEASES_URL,
            )
        else:
            async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch drive list, then full details per drive.

        Returns a dict keyed by drive ID, plus a ``_filesystems`` key
        containing a list of filesystem info dicts (empty list when the
        agent is older or has no filesystems configured).

        After fetching, evaluates attention states and fires/dismisses
        notifications as needed.
        """
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=30)

        try:
            async with session.get(
                f"{self._base_url}/api/drives",
                headers=self._headers,
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                drives_list: list[dict[str, Any]] = await resp.json()

            result: dict[str, Any] = {}
            for drive_summary in drives_list:
                drive_id = drive_summary["id"]
                async with session.get(
                    f"{self._base_url}/api/drives/{drive_id}",
                    headers=self._headers,
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    result[drive_id] = await resp.json()

            # Check agent version via /api/health (tiny payload, negligible overhead).
            fs_count = 0
            try:
                async with session.get(
                    f"{self._base_url}/api/health",
                    headers=self._headers,
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    health = await resp.json()
                agent_version = health.get("version", "")
                fs_count = health.get("filesystems", 0)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Health check failed — don't block the poll, but treat as
                # unknown version so the repair fires.
                agent_version = ""

            self._check_agent_version(agent_version)

            # Fetch filesystem usage data when the agent supports it.
            # Older agents (pre-0.5.0) won't advertise filesystems in
            # /api/health and won't expose the endpoint — skip gracefully.
            filesystems: list[dict[str, Any]] = []
            if fs_count > 0:
                try:
                    async with session.get(
                        f"{self._base_url}/api/filesystems",
                        headers=self._headers,
                        timeout=timeout,
                    ) as resp:
                        resp.raise_for_status()
                        filesystems = await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    _LOGGER.debug(
                        "SMART Sniffer: /api/filesystems fetch failed, skipping"
                    )

            result["_filesystems"] = filesystems

        except aiohttp.ClientError as err:
            raise UpdateFailed(
                f"Error communicating with SMART Sniffer agent: {err}"
            ) from err

        await self._handle_attention_notifications(result)
        return result

    async def _handle_attention_notifications(
        self, new_data: dict[str, Any]
    ) -> None:
        """Compare attention state to previous, fire/dismiss notifications."""
        current_drive_ids = {k for k in new_data if not k.startswith("_")}

        for drive_id, drive_data in new_data.items():
            if drive_id.startswith("_"):
                continue  # skip internal keys like _filesystems
            state, severity, reasons = evaluate_attention(drive_data)
            prev = self._prev_state.get(drive_id)

            if prev is None:
                # First observation — record baseline, no notification.
                _LOGGER.debug(
                    "SMART Sniffer: first observation of %s, state=%s",
                    drive_id, state,
                )
                self._prev_state[drive_id] = state
                self._prev_reasons[drive_id] = reasons
                continue

            prev_reasons = self._prev_reasons.get(drive_id, [])
            reasons_changed = sorted(reasons) != sorted(prev_reasons)

            if state == prev and not reasons_changed:
                continue  # No change in state or reasons.

            notif_id = _notif_id(drive_id)

            if state == STATE_NO:
                # Resolved — dismiss.
                _LOGGER.info(
                    "SMART Sniffer: %s attention cleared (was %s)", drive_id, prev,
                )
                pn_dismiss(self.hass, notif_id)

            elif state == STATE_UNSUPPORTED:
                # Unsupported — informational notification (once).
                _LOGGER.info(
                    "SMART Sniffer: %s has no usable SMART data", drive_id,
                )
                title, message = _build_notification(
                    drive_data, state, severity, reasons,
                )
                pn_create(self.hass, message=message, title=title,
                          notification_id=notif_id)

            elif state in (STATE_MAYBE, STATE_YES):
                # Attention needed — fire, escalate/de-escalate, or refresh reasons.
                if state == prev and reasons_changed:
                    action = "reasons updated"
                elif prev == STATE_NO:
                    action = "now requires attention"
                elif prev == STATE_MAYBE and state == STATE_YES:
                    action = "ESCALATED to critical"
                elif prev == STATE_YES and state == STATE_MAYBE:
                    action = "de-escalated to warning"
                else:
                    action = f"changed from {prev} to {state}"

                _LOGGER.warning(
                    "SMART Sniffer: %s %s: %s",
                    drive_id, action, "; ".join(reasons),
                )
                title, message = _build_notification(
                    drive_data, state, severity, reasons,
                )
                pn_create(self.hass, message=message, title=title,
                          notification_id=notif_id)

            self._prev_state[drive_id] = state
            self._prev_reasons[drive_id] = reasons

        # Clean up state for drives that disappeared (e.g., USB unplugged).
        removed = set(self._prev_state.keys()) - current_drive_ids
        for drive_id in removed:
            _LOGGER.debug("SMART Sniffer: %s no longer present, cleaning up", drive_id)
            del self._prev_state[drive_id]
            self._prev_reasons.pop(drive_id, None)
            pn_dismiss(self.hass, _notif_id(drive_id))
