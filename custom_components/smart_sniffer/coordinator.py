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

One exception to the first-poll rule (D11, v0.8.0): a reason that only the
drive's Device Statistics could give (it ends "from device statistics") and
that was never announced for that drive raises the notification on the first
poll too, with a line saying so. Announcements are kept in the entry's Store,
so a restart does not repeat them.

ZFS pools (GH #50) follow the same rules through pool_health.py: a pool that
goes from healthy to unhealthy raises a notification, a change in its reasons
updates it, and recovery dismisses it. A registered pool the agent stops
listing (exported, or failed to import) is missing, and raises a notification
under the same id, even on the first poll. IDs are
smart_sniffer_pool_{entry_id}_{pool name}.
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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .attention import (
    SEVERITY_CRITICAL,
    STATE_MAYBE,
    STATE_NO,
    STATE_UNSUPPORTED,
    STATE_YES,
    evaluate_attention,
    get_thresholds,
)
from .const import (
    AGENT_INSTALL_URL,
    AGENT_RELEASES_URL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_AGENT_VERSION,
    POOLS_KEY,
    POOLS_MISSING_KEY,
    STORE_SAVE_DELAY,
)
from .devstat import (
    ANNOUNCEMENT_LINE,
    DEVSTAT_KEY,
    announcement_records,
    baseline_announcement,
    forget_serial,
    merge_devstat,
    normalize_store,
)
from .entity_plan import DriveClass, EntitySpec, forget_device as forget_created
from .pool_health import (
    advertises_pools,
    build_missing_notification as build_pool_missing_notification,
    build_notification as build_pool_notification,
    fetch_pools,
    missing_pools,
    notification_actions as pool_notification_actions,
    notification_id as pool_notification_id,
    registered_pool_names as pool_names_in_registry,
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

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: Store | None = None,
        stored: Any = None,
    ) -> None:
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

        # The same for ZFS pools: pool name -> its problem reasons at the last
        # poll that could read them ([] = healthy). A name absent = not seen.
        self._prev_pool_reasons: dict[str, list[str]] = {}

        # Device Statistics (devstat.py): the held readings per drive and the
        # D11 announcement records, loaded from the entry's Store before the
        # first refresh and written back, delayed, when they change.
        self._store = store
        self._devstat = normalize_store(stored)

        # Entity plan (entity_plan.py): what each platform has created, by
        # unique id, and each drive's class from its first plan.
        self.created: dict[str, dict[str, EntitySpec]] = {}
        self.drive_classes: dict[str, DriveClass] = {}

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
                    "install_url": AGENT_INSTALL_URL,
                },
                learn_more_url=AGENT_RELEASES_URL,
            )
        else:
            async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch drive list, then full details per drive.

        Returns a dict keyed by drive ID, plus a ``_filesystems`` key
        containing a list of filesystem info dicts (empty list when the
        agent is older or has no filesystems configured), a ``_pools`` key
        with the agent's ZFS pools (empty list when the agent does not
        advertise them, None when it does and the fetch failed), and a
        ``_pools_missing`` key naming the registered pools that list left out
        (None when it could not be read).

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
            health: dict[str, Any] | None = None
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
                health = None

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

            # ZFS pool status, when the agent advertises it (GH #50). Agents
            # without it, or with it off, do not list /api/pools in health
            # and are never asked. A failed fetch, or a failed health check,
            # stores None: pools unknown, not pools gone.
            async def _get_json(path: str) -> Any:
                try:
                    async with session.get(
                        f"{self._base_url}{path}",
                        headers=self._headers,
                        timeout=timeout,
                    ) as resp:
                        resp.raise_for_status()
                        return await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    _LOGGER.debug("SMART Sniffer: %s fetch failed, skipping", path)
                    return None

            pools = await fetch_pools(health, _get_json) if health is not None else None
            result[POOLS_KEY] = pools
            result[POOLS_MISSING_KEY] = missing_pools(
                advertises_pools(health), pools, self.registered_pool_names()
            )

        except aiohttp.ClientError as err:
            raise UpdateFailed(
                f"Error communicating with SMART Sniffer agent: {err}"
            ) from err

        self._merge_devstat(result)
        await self._handle_attention_notifications(result)
        self._handle_pool_notifications(
            result.get(POOLS_KEY), result.get(POOLS_MISSING_KEY)
        )
        return result

    def _merge_devstat(self, result: dict[str, Any]) -> None:
        """Merge each drive's Device Statistics with the held copy (6.3).

        Stores the effective readings on the drive as ``_devstat``, which
        attention, the threshold form and the Data Written / Read sensors all
        read, and schedules a delayed save when a held value changed.
        """
        held = self._devstat["held"]
        changed = False
        for drive_id, drive_data in result.items():
            if drive_id.startswith("_") or not isinstance(drive_data, dict):
                continue
            effective, new_held = merge_devstat(drive_data, held.get(drive_id))
            drive_data[DEVSTAT_KEY] = effective
            if new_held and new_held != held.get(drive_id):
                held[drive_id] = new_held
                changed = True
        if changed:
            self._save_devstat()

    def _save_devstat(self, delay: float = STORE_SAVE_DELAY) -> None:
        if self._store is not None:
            self._store.async_delay_save(lambda: self._devstat, delay)

    def _record_announced(self, records: list[str]) -> None:
        announced = self._devstat["announced"]
        new = [r for r in records if r not in announced]
        if new:
            announced.extend(new)
            # An announcement is rare and must survive an unclean stop, or the
            # drive is announced again on the next start. Held readings can
            # wait for the long delay; this record is written straight away.
            self._save_devstat(delay=1)

    def forget_device(self, identifier: str, serial: str | None = None) -> None:
        """A device the user removed: forget what was created for it, its
        class, and, for a drive, its held readings and announcement records,
        so it starts afresh if it comes back."""
        forget_created(self.created, identifier)
        self.drive_classes.pop(identifier, None)
        changed = self._devstat["held"].pop(identifier, None) is not None
        announced = forget_serial(self._devstat["announced"], serial)
        if announced != self._devstat["announced"]:
            self._devstat["announced"] = announced
            changed = True
        if changed:
            self._save_devstat()

    def registered_pool_names(self) -> list[str]:
        """Pools with a device in the registry for this config entry.

        This is what "seen before" means for a pool: its device was created
        when its entities were. Removing the device is how a user tells the
        integration a pool is gone on purpose, so it is read fresh each poll.
        """
        entry_id = self.config_entry.entry_id
        devices = dr.async_entries_for_config_entry(dr.async_get(self.hass), entry_id)
        return pool_names_in_registry((device.identifiers for device in devices), entry_id)

    def forget_pool(self, name: str) -> None:
        """Drop a pool whose device the user removed: dismiss its notification
        and forget its state, so it is never reported missing again."""
        pn_dismiss(self.hass, pool_notification_id(self.config_entry.entry_id, name))
        self._prev_pool_reasons.pop(name, None)

    def _handle_pool_notifications(
        self,
        pools: list[dict[str, Any]] | None,
        missing: list[str] | None,
    ) -> None:
        """Raise, update or dismiss ZFS pool notifications after a poll."""
        actions, self._prev_pool_reasons = pool_notification_actions(
            self._prev_pool_reasons, pools, missing
        )
        entry_id = self.config_entry.entry_id
        for action in actions:
            notif_id = pool_notification_id(entry_id, action.name)
            if action.kind == "dismiss":
                _LOGGER.info("SMART Sniffer: ZFS pool %s attention cleared", action.name)
                pn_dismiss(self.hass, notif_id)
                continue
            if action.kind == "missing":
                _LOGGER.warning(
                    "SMART Sniffer: ZFS pool %s is no longer reported by the agent",
                    action.name,
                )
                title, message = build_pool_missing_notification(action.name, self._hostname)
            else:
                _LOGGER.warning(
                    "SMART Sniffer: ZFS pool %s needs attention: %s",
                    action.name, "; ".join(action.reasons),
                )
                title, message = build_pool_notification(
                    action.name, self._hostname, action.pool_lines, action.device_lines
                )
            pn_create(self.hass, message=message, title=title, notification_id=notif_id)

    async def _handle_attention_notifications(
        self, new_data: dict[str, Any]
    ) -> None:
        """Compare attention state to previous, fire/dismiss notifications."""
        current_drive_ids = {k for k in new_data if not k.startswith("_")}

        for drive_id, drive_data in new_data.items():
            if drive_id.startswith("_"):
                continue  # skip internal keys like _filesystems
            # The fourth value is deliberately ignored here. Accepted entries
            # must not reach the reasons comparison below, or an accepted value
            # drifting under its threshold would read as a change on every poll.
            state, severity, reasons, _ = evaluate_attention(
                drive_data, get_thresholds(self.config_entry, drive_id)
            )
            prev = self._prev_state.get(drive_id)

            if prev is None:
                # First observation — record baseline, no notification.
                _LOGGER.debug(
                    "SMART Sniffer: first observation of %s, state=%s",
                    drive_id, state,
                )
                self._prev_state[drive_id] = state
                self._prev_reasons[drive_id] = reasons
                # Except (D11): a reason only Device Statistics could give,
                # never announced for this drive, is news even on the first
                # poll; otherwise a combined agent and integration upgrade
                # would turn a drive YES with nobody told. Announced once per
                # drive and reading, across restarts.
                announce, records = baseline_announcement(
                    reasons, drive_data.get("serial"), self._devstat["announced"]
                )
                if announce:
                    _LOGGER.warning(
                        "SMART Sniffer: %s now requires attention: %s",
                        drive_id, "; ".join(reasons),
                    )
                    title, message = _build_notification(
                        drive_data, state, severity, reasons,
                    )
                    pn_create(self.hass, message=f"{message}\n\n{ANNOUNCEMENT_LINE}",
                              title=title, notification_id=_notif_id(drive_id))
                    self._record_announced(records)
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
                # A gap-filled reason told this way counts as announced.
                self._record_announced(
                    announcement_records(reasons, drive_data.get("serial"))
                )

            self._prev_state[drive_id] = state
            self._prev_reasons[drive_id] = reasons

        # Clean up state for drives that disappeared (e.g., USB unplugged).
        removed = set(self._prev_state.keys()) - current_drive_ids
        for drive_id in removed:
            _LOGGER.debug("SMART Sniffer: %s no longer present, cleaning up", drive_id)
            del self._prev_state[drive_id]
            self._prev_reasons.pop(drive_id, None)
            pn_dismiss(self.hass, _notif_id(drive_id))


class AgentHealthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Lightweight coordinator that pings /api/health.

    Never raises UpdateFailed -- always returns a result dict with
    connected=True/False so the binary sensor stays available even
    when the agent is offline.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data[CONF_PORT]
        self.token: str = entry.data.get(CONF_TOKEN, "")
        self.entry = entry
        interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_health",
            update_interval=timedelta(seconds=interval),
        )

        # Track last known values so we can serve them when the agent is down.
        self._last_version: str = ""
        self._last_seen: str | None = None
        self._last_os: str = ""
        self._last_uptime: int | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Ping the agent health endpoint. Never raises UpdateFailed."""
        from homeassistant.util import dt as dt_util

        session = async_get_clientsession(self.hass)
        url = f"http://{self.host}:{self.port}/api/health"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}

        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                now = dt_util.utcnow()
                self._last_version = data.get("version", "")
                self._last_seen = now.isoformat()
                self._last_os = data.get("os", "")
                self._last_uptime = data.get("uptime_seconds")
                return {
                    "connected": True,
                    "version": self._last_version,
                    "os": self._last_os,
                    "uptime_seconds": self._last_uptime,
                    "last_seen": self._last_seen,
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
            return {
                "connected": False,
                "version": self._last_version,
                "os": self._last_os,
                "uptime_seconds": None,
                "last_seen": self._last_seen,
            }
