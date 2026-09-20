"""Config flow and options flow for SMART Sniffer integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

import aiohttp
import voluptuous as vol

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2025.x compat
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .attention import (
    GAUGE_LABELS,
    PREFILL_ACCEPT,
    PREFILL_DEFAULTS,
    PREFILL_STORED,
    current_readings,
    default_threshold,
    flatten_sections,
    get_thresholds,
    labels_for_drive,
    prefill_thresholds,
    reading_placeholders,
)
from .const import (
    CONF_FORCE_UPDATE,
    CONF_TOKEN,
    DEFAULT_FORCE_UPDATE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_AGENT_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold form layout
# ---------------------------------------------------------------------------
# Ten full-width cards is a wall, so the fields are grouped. Section ids are
# fixed because their names and descriptions come from translations; the field
# keys inside them are the labels themselves, derived at runtime.
SECTION_DAMAGE = "damage"
SECTION_CLEAN = "clean"
SECTION_GAUGES = "gauges"

def _agent_is_outdated(agent_version: str) -> bool:
    """Return True if agent_version < MIN_AGENT_VERSION."""
    try:
        av = tuple(int(x) for x in agent_version.split("."))
        mv = tuple(int(x) for x in MIN_AGENT_VERSION.split("."))
        return av < mv
    except (ValueError, AttributeError):
        return False


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_TOKEN, default=""): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.Coerce(int),
    }
)


class SmartSnifferConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SMART Sniffer."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return SmartSnifferOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user provides agent connection details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            token = user_input.get(CONF_TOKEN, "")

            try:
                await self._test_connection(host, port, token)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                title = f"SMART Sniffer ({host}:{port})"
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def _pick_best_ip(discovery_info: ZeroconfServiceInfo) -> str:
        """Choose the best IP from discovery info.

        Prioritizes real LAN addresses over virtual/tunnel IPs:
          1. IPv4 192.168.x.x, 10.x.x.x  (almost always physical LAN)
          2. IPv4 172.16-31.x.x           (RFC 1918, but often Docker/container bridges)
          3. IPv4 100.64-127.x.x          (CGNAT — Tailscale, WireGuard, etc.)
          4. IPv6                          (deprioritized — unreliable across VLANs)
          5. Anything else

        Falls back to whatever is available.
        """
        # discovery_info may expose ip_address (single) and ip_addresses (list).
        candidates: list[str] = []
        if hasattr(discovery_info, "ip_addresses") and discovery_info.ip_addresses:
            candidates = [str(a) for a in discovery_info.ip_addresses]
        elif discovery_info.ip_address:
            candidates = [str(discovery_info.ip_address)]

        if not candidates:
            return str(discovery_info.ip_address)

        def _score(ip_str: str) -> int:
            """Lower score = more preferred."""
            try:
                addr = ipaddress.ip_address(ip_str)
            except ValueError:
                return 99
            # IPv6 — deprioritize; unreliable across VLANs in home/SMB networks.
            if addr.version == 6:
                return 85
            if not addr.is_private:
                return 90
            # 192.168.x.x and 10.x.x.x — almost always a real LAN interface.
            if ip_str.startswith("192.168.") or ip_str.startswith("10."):
                return 10
            # 172.16-31.x.x — RFC 1918 but frequently Docker/container bridges.
            if ip_str.startswith("172."):
                return 50
            # 100.64-127.x.x — CGNAT range (Tailscale, WireGuard, etc.)
            if ip_str.startswith("100."):
                return 70
            return 80

        candidates.sort(key=_score)
        return candidates[0]

    def _migrate_legacy_unique_ids(self, hostname: str, host: str, port: int) -> None:
        """Migrate existing config entries from IP-based to hostname-based unique IDs.

        Before v0.4.24, unique IDs were "{ip}:{port}". This caused duplicates
        when mDNS reflectors or multi-homed hosts advertised multiple IPs.
        Now we use "smartha-{hostname}" for stable deduplication.

        This scans existing entries and updates any that match by IP or hostname
        so the new discovery is properly deduplicated.
        """
        for entry in self._async_current_entries():
            if entry.domain != DOMAIN:
                continue
            old_uid = entry.unique_id or ""
            # Already migrated.
            if old_uid.startswith("smartha-"):
                continue
            # Match by IP:port (old format) or by hostname in entry title/data.
            entry_host = entry.data.get(CONF_HOST, "")
            entry_port = entry.data.get(CONF_PORT, 0)
            entry_title = entry.title or ""
            if (
                old_uid == f"{host}:{port}"
                or (entry_host == host and entry_port == port)
                or hostname.lower() in entry_title.lower()
            ):
                _LOGGER.info(
                    "Migrating SMART Sniffer unique_id: %s → smartha-%s",
                    old_uid,
                    hostname,
                )
                self.hass.config_entries.async_update_entry(
                    entry,
                    unique_id=f"smartha-{hostname}",
                )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via mDNS/Zeroconf."""
        port = discovery_info.port
        properties = discovery_info.properties
        hostname = properties.get("hostname", "")

        # Agent v0.4.25+ includes an "ip" TXT field with its preferred LAN
        # address. Trust it over our own scoring when present.
        agent_preferred_ip = properties.get("ip", "")
        if agent_preferred_ip:
            host = agent_preferred_ip
        else:
            host = self._pick_best_ip(discovery_info)

        if not hostname:
            hostname = host

        # Migrate any existing IP-based unique IDs to hostname-based.
        self._migrate_legacy_unique_ids(hostname, host, port)

        # Deduplicate — hostname-based ID is stable across interfaces/VLANs.
        await self.async_set_unique_id(f"smartha-{hostname}")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host}  # Update IP if it changed (e.g. DHCP)
        )

        # Stash discovery data for the confirmation step.
        self._discovery_host = host
        self._discovery_port = port
        self._discovery_hostname = hostname
        self._discovery_drives = properties.get("drives", "?")
        self._discovery_auth = properties.get("auth", "0") == "1"

        # Check if agent version from mDNS TXT is outdated.
        agent_version = properties.get("version", "")
        self._agent_outdated = bool(
            agent_version and _agent_is_outdated(agent_version)
        )
        self._agent_version = agent_version

        # Set a nice title for the discovery notification.
        self.context["title_placeholders"] = {
            "hostname": self._discovery_hostname,
        }

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovered agent and optionally collect token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input.get(CONF_TOKEN, "")
            try:
                await self._test_connection(
                    self._discovery_host, self._discovery_port, token
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during zeroconf confirm")
                errors["base"] = "unknown"
            else:
                title = f"SMART Sniffer ({self._discovery_hostname})"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: self._discovery_host,
                        CONF_PORT: self._discovery_port,
                        CONF_TOKEN: token,
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    },
                )

        # Auth enabled — show form with token field.
        # No auth — show confirmation with no input fields (just Submit).
        if self._discovery_auth:
            schema = vol.Schema({vol.Optional(CONF_TOKEN, default=""): str})
        else:
            schema = vol.Schema({})

        # Build description placeholders, including an optional version warning.
        placeholders = {
            "hostname": self._discovery_hostname,
            "host": self._discovery_host,
            "port": str(self._discovery_port),
            "drives": str(self._discovery_drives),
            "agent_version_warning": "",
        }
        if getattr(self, "_agent_outdated", False):
            placeholders["agent_version_warning"] = (
                f"\n\n⚠️ This agent is running **v{self._agent_version}** "
                f"but the integration requires at least **v{MIN_AGENT_VERSION}**. "
                "You can still add it, but please update the agent afterwards."
            )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _test_connection(self, host: str, port: int, token: str) -> None:
        """Test that the agent is reachable and returns a healthy status."""
        session = async_get_clientsession(self.hass)
        url = f"http://{host}:{port}/api/health"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("status") != "ok":
                raise aiohttp.ClientError("Unexpected health response")


class SmartSnifferOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options for an existing SMART Sniffer config entry.

    Two branches from the menu: connection settings (port, token, interval),
    and per-drive alert thresholds (#36).

    Everything here persists into ``entry.data``, never ``entry.options``. This
    flow calls async_create_entry(data={}), so options is permanently empty;
    anything written there would be silently discarded.

    The base class is deprecated upstream. It is deliberately not migrated as
    part of this change: hacs.json declares a floor of 2024.1.0, and
    self.config_entry only became available on plain OptionsFlow in 2024.11, so
    migrating would raise the supported floor by ten months. That is a
    user-facing decision, not a cleanup to fold into a feature.
    """

    # Drive chosen in the picker, carried into the editor step.
    _threshold_drive_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between connection settings and per-drive thresholds."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "thresholds"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the connection form pre-filled with current values."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate connectivity with potentially new settings.
            host = self.config_entry.data[CONF_HOST]
            port = user_input.get(CONF_PORT, self.config_entry.data[CONF_PORT])
            token = user_input.get(CONF_TOKEN, "")

            try:
                session = async_get_clientsession(self.hass)
                url = f"http://{host}:{port}/api/health"
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    resp.raise_for_status()
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in options flow")
                errors["base"] = "unknown"
            else:
                # Merge new options into the config entry data.
                new_data = {**self.config_entry.data, **user_input}
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                # Trigger a coordinator refresh with the new settings.
                return self.async_create_entry(title="", data={})

        # Pre-fill with current values.
        current = self.config_entry.data
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PORT,
                    default=current.get(CONF_PORT, DEFAULT_PORT),
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_TOKEN,
                    default=current.get(CONF_TOKEN, ""),
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_FORCE_UPDATE,
                    default=current.get(CONF_FORCE_UPDATE, DEFAULT_FORCE_UPDATE),
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Per-drive alert thresholds (#36)
    # ------------------------------------------------------------------

    def _coordinator(self) -> Any:
        return self.hass.data[DOMAIN][self.config_entry.entry_id]["coordinator"]

    def _drive_choices(self) -> list[dict[str, str]]:
        """Selectable drives, newest agent data first."""
        coordinator = self._coordinator()
        choices: list[dict[str, str]] = []
        for drive_id, drive_data in (coordinator.data or {}).items():
            if drive_id.startswith("_"):
                continue  # internal keys like _filesystems
            if drive_data.get("readable") is False:
                continue  # the agent could not read it; identity is not trusted
            model = drive_data.get("model") or "Unknown drive"
            serial = drive_data.get("serial") or drive_id
            choices.append({"value": drive_id, "label": f"{model} ({serial})"})
        return choices

    async def async_step_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the drive whose thresholds are being edited."""
        choices = self._drive_choices()

        if not choices:
            # Agent unreachable or nothing readable. An empty picker would look
            # like a bug, so say what happened instead.
            return self.async_abort(reason="no_drives")

        if len(choices) == 1:
            self._threshold_drive_id = choices[0]["value"]
            return await self.async_step_drive_thresholds()

        if user_input is not None:
            self._threshold_drive_id = user_input["drive_id"]
            return await self.async_step_drive_thresholds()

        schema = vol.Schema(
            {
                vol.Required("drive_id"): SelectSelector(
                    SelectSelectorConfig(
                        options=choices,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="thresholds", data_schema=schema)

    def _drive_label(self) -> str:
        drive_id = self._threshold_drive_id or ""
        return next(
            (c["label"] for c in self._drive_choices() if c["value"] == drive_id),
            drive_id,
        )

    async def async_step_drive_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to fill the threshold form.

        All three options land on the same form, prefilled differently. Nothing
        applies on click: the user sees every value and can edit any of them
        before the single commit, which is the Submit button.
        """
        return self.async_show_menu(
            step_id="drive_thresholds",
            menu_options=["edit_thresholds", "accept_current", "reset_defaults"],
            description_placeholders={"drive": self._drive_label()},
        )

    async def async_step_edit_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._threshold_mode = PREFILL_STORED
        return await self.async_step_threshold_form()

    async def async_step_accept_current(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._threshold_mode = PREFILL_ACCEPT
        return await self.async_step_threshold_form()

    async def async_step_reset_defaults(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._threshold_mode = PREFILL_DEFAULTS
        return await self.async_step_threshold_form()

    async def async_step_threshold_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The one threshold form. Reached from all three menu options."""
        drive_id = self._threshold_drive_id or ""
        coordinator = self._coordinator()
        drive_data = (coordinator.data or {}).get(drive_id) or {}

        # Only labels this drive's protocol can report. An NVMe drive has no
        # Spin Retry Count, and offering it would invite a setting that could
        # never do anything.
        labels = labels_for_drive(drive_data)
        readings = current_readings(drive_data)

        if user_input is not None:
            flat = flatten_sections(user_input)
            chosen: dict[str, int] = {}
            for label in labels:
                if label not in flat:
                    continue
                try:
                    chosen[label] = int(flat[label])
                except (TypeError, ValueError):
                    continue  # leave it at the default rather than storing junk

            # Store only genuine overrides. Keeping a value that equals the
            # built-in default would freeze today's default in place, so a
            # future change to it would silently not reach this user.
            chosen = {
                label: value
                for label, value in chosen.items()
                if value != default_threshold(label)
            }

            data = {**self.config_entry.data}
            thresholds = {**(data.get("thresholds") or {})}
            if chosen:
                thresholds[drive_id] = chosen
            else:
                thresholds.pop(drive_id, None)
            data["thresholds"] = thresholds

            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        stored = get_thresholds(self.config_entry, drive_id)
        prefill = prefill_thresholds(
            drive_data, stored, getattr(self, "_threshold_mode", PREFILL_STORED)
        )

        # Three groups, because ten full-width cards is a wall.
        gauges = [lbl for lbl in labels if lbl in GAUGE_LABELS]
        counters = [lbl for lbl in labels if lbl not in GAUGE_LABELS]
        damage = [lbl for lbl in counters if readings.get(lbl, 0) > 0]
        clean = [lbl for lbl in counters if lbl not in damage]

        def _field(label: str) -> Any:
            return NumberSelector(
                NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX)
            )

        def _group(group_labels: list[str]) -> vol.Schema:
            return vol.Schema(
                {
                    vol.Optional(label, default=prefill[label]): _field(label)
                    for label in group_labels
                }
            )

        schema: dict[Any, Any] = {}
        if damage:
            schema[vol.Required(SECTION_DAMAGE)] = section(
                _group(damage), {"collapsed": False}
            )
        if clean:
            # With nothing reporting damage the form would otherwise be three
            # collapsed headers and no visible field, so expand this instead.
            schema[vol.Required(SECTION_CLEAN)] = section(
                _group(clean), {"collapsed": bool(damage)}
            )
        if gauges:
            schema[vol.Required(SECTION_GAUGES)] = section(
                _group(gauges), {"collapsed": True}
            )

        return self.async_show_form(
            step_id="threshold_form",
            data_schema=vol.Schema(schema),
            description_placeholders=reading_placeholders(
                readings, self._drive_label(), len(clean)
            ),
        )
