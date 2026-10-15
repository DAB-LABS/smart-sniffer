"""Config flow and options flow for SMART Sniffer integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2025.x compat
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .agent_identity import UNIQUE_ID_PREFIX, confirms_same_agent, ip_score
from .attention import (
    current_readings,
    form_order,
    get_thresholds,
    labels_for_drive,
    overrides_from_form,
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
                health = await self._test_connection(host, port, token)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                # The same agent may already be configured under another
                # address, by discovery or by hand (#64, app#12). Only when
                # the agents agree on host name and drives (agent_identity).
                if await self._configured_elsewhere(
                    health.get("hostname"), host, port, token
                ):
                    return self.async_abort(reason="already_configured")

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

        Prioritizes real LAN addresses over virtual/tunnel IPs, then IPv6,
        public and link-local addresses (agent_identity.ip_score has the
        order). Used only when the agent's TXT record has no "ip".

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

        candidates.sort(key=ip_score)
        return candidates[0]

    async def _get_json(self, host: str, port: int, token: str, path: str) -> Any:
        """One GET to an agent, 5 s at most; None on any failure."""
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with session.get(
                f"http://{host}:{port}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError):
            return None

    async def _configured_elsewhere(
        self,
        hostname: Any,
        new_host: str,
        port: int,
        token: str,
        advertised_drive_count: Any = None,
        exclude_unique_id: str = "",
    ) -> ConfigEntry | None:
        """The existing entry that is shown to be this agent, or None.

        For each other entry on the same port: its host's /api/health must
        give the same host name, and the drives at both addresses must match
        (agent_identity.confirms_same_agent). Host names alone collide (two
        routers both called "OpenWrt"), so a name match is never enough. An
        entry whose host cannot be read is not a match. The new address is
        read with ``token``, or with the existing entry's token when there is
        none (a discovery: the same agent takes the same token); when it
        cannot be read, the advertised drive count stands in.
        """
        if not isinstance(hostname, str) or not hostname:
            return None
        candidates = []
        for entry in self._async_current_entries(include_ignore=False):
            if exclude_unique_id and entry.unique_id == exclude_unique_id:
                continue
            try:
                entry_port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
            except (TypeError, ValueError):
                continue
            entry_host = str(entry.data.get(CONF_HOST, "") or "")
            if entry_port != port or not entry_host or entry_host == new_host:
                continue
            candidates.append(entry)

        async def check(entry: ConfigEntry) -> bool:
            entry_host = str(entry.data[CONF_HOST])
            entry_token = str(entry.data.get(CONF_TOKEN, "") or "")
            health, existing, new = await asyncio.gather(
                self._get_json(entry_host, port, entry_token, "/api/health"),
                self._get_json(entry_host, port, entry_token, "/api/drives"),
                self._get_json(new_host, port, token or entry_token, "/api/drives"),
            )
            return confirms_same_agent(
                hostname, health, existing, new, advertised_drive_count
            )

        results = await asyncio.gather(*(check(entry) for entry in candidates))
        return next((e for e, same in zip(candidates, results, strict=True) if same), None)

    async def _reachable(self, host: str, port: int, token: str) -> bool:
        try:
            await self._test_connection(host, port, token)
        except Exception:  # noqa: BLE001 - any failure means "not usable"
            return False
        return True

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
            # The title test is the title discovery gave the entry, exactly.
            # A hand-added entry is titled "SMART Sniffer (host:port)" and a
            # substring test matched one entered by the agent's host name,
            # then rewrote its host to the advertised address (#64).
            if (
                old_uid == f"{host}:{port}"
                or (entry_host == host and entry_port == port)
                or entry_title.lower() == f"smart sniffer ({hostname})".lower()
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

        own_id = f"{UNIQUE_ID_PREFIX}{hostname}"

        # Deduplicate — hostname-based ID is stable across interfaces/VLANs.
        # First, as it is cheap: a flow already running for this agent, or an
        # entry discovery made, ends here.
        entry = await self.async_set_unique_id(own_id)
        if entry is not None:
            await self._abort_for_known_entry(entry, host, port)

        # An entry added by hand or by the Control Center has a "host:port"
        # unique_id, so the unique_id check cannot see it when the
        # agent advertises another address (app#12: its WAN address; the
        # App: its container network). Abort only when the agents agree on
        # host name and drives, and leave that entry's host alone: the
        # advertised address may be the wrong one.
        if hostname != host:
            existing = await self._configured_elsewhere(
                hostname,
                host,
                port,
                "",
                advertised_drive_count=properties.get("drives"),
                exclude_unique_id=own_id,
            )
            if existing is not None:
                _LOGGER.debug(
                    "Agent %s (%s:%s) is already configured at %s",
                    hostname, host, port, existing.data.get(CONF_HOST),
                )
                return self.async_abort(reason="already_configured")

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

    async def _abort_for_known_entry(
        self, entry: ConfigEntry, host: str, port: int
    ) -> None:
        """Abort a rediscovery of a configured agent, moving the entry to the
        advertised address only when that is the one that works.

        The entry's host is changed only if it fails a connection test and
        the advertised address passes one. An entry that works is never
        repointed, and a wrong advertised address (app#12: a WAN address)
        cannot break one that does.
        """
        entry_host = str(entry.data.get(CONF_HOST, "") or "")
        entry_port = entry.data.get(CONF_PORT, port)
        token = str(entry.data.get(CONF_TOKEN, "") or "")
        if (
            entry.source != SOURCE_IGNORE
            and entry_host
            and entry_host != host
            and not await self._reachable(entry_host, entry_port, token)
            and await self._reachable(host, port, token)
        ):
            _LOGGER.info(
                "SMART Sniffer agent %s: %s:%s does not answer, moving to the "
                "advertised %s:%s",
                entry.unique_id, entry_host, entry_port, host, port,
            )
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )
        self._abort_if_unique_id_configured()

    async def _test_connection(self, host: str, port: int, token: str) -> dict[str, Any]:
        """Test that the agent is reachable and returns a healthy status.

        Returns the /api/health body.
        """
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
            if not isinstance(data, dict) or data.get("status") != "ok":
                raise aiohttp.ClientError("Unexpected health response")
            return data


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
            return await self.async_step_threshold_form()

        if user_input is not None:
            self._threshold_drive_id = user_input["drive_id"]
            return await self.async_step_threshold_form()

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

    async def async_step_threshold_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The threshold form for one drive.

        Every field is prefilled with the value in force, and shows the drive's
        current reading and the built-in default under it. Typing the default
        clears an override, because only values that differ from it are stored.
        """
        drive_id = self._threshold_drive_id or ""
        coordinator = self._coordinator()
        drive_data = (coordinator.data or {}).get(drive_id) or {}

        # Only labels this drive's protocol can report. An NVMe drive has no
        # Spin Retry Count, and offering it would invite a setting that could
        # never do anything.
        labels = labels_for_drive(drive_data)
        readings = current_readings(drive_data)

        if user_input is not None:
            chosen = overrides_from_form(labels, user_input)

            data = {**self.config_entry.data}
            thresholds = {**(data.get("thresholds") or {})}
            if chosen:
                thresholds[drive_id] = chosen
            else:
                thresholds.pop(drive_id, None)
            data["thresholds"] = thresholds

            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        prefill = prefill_thresholds(
            drive_data, get_thresholds(self.config_entry, drive_id)
        )
        schema = vol.Schema(
            {
                vol.Optional(label, default=prefill[label]): NumberSelector(
                    NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX)
                )
                for label in form_order(labels, readings)
            }
        )

        return self.async_show_form(
            step_id="threshold_form",
            data_schema=schema,
            description_placeholders=reading_placeholders(
                readings, self._drive_label()
            ),
        )
