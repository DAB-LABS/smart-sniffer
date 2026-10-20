"""Config flow and options flow for SMART Sniffer integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

import aiohttp
import voluptuous as vol

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2025.x compat
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import (
    SOURCE_ZEROCONF,
    ConfigEntryState,
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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .agent_identity import (
    ENTRY_AGENT_DRIVES,
    ENTRY_AGENT_HOSTNAME,
    ENTRY_AGENT_ID,
    ENTRY_AGENT_MOCK,
    UNIQUE_ID_PREFIX,
    confirms_same_agent,
    display_hostname,
    drive_digest,
    identity_changes,
    ip_score,
    norm_agent_id,
    norm_host,
)
from .agent_version import agent_is_outdated, display_version
from .host_name import (
    APP_AGENT_LABEL,
    APP_HOST,
    ENTRY_APP_SLUG,
    address_label,
    app_kind_change,
    app_names,
    clean_host,
    is_app_entry,
)
from .source import (
    REPLICA_TITLE,
    REPLICA_UNIQUE_ID,
    is_replica_data,
    replica_entry_data,
)
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
    DISCOVERY_CONTEXT,
    CONF_FORCE_UPDATE,
    CONF_TOKEN,
    DEFAULT_FORCE_UPDATE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    IMPORT_FROM_APP,
    MIN_AGENT_VERSION,
)

_LOGGER = logging.getLogger(__name__)


# Every password field: shown as dots, as Home Assistant's own flows do.
TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_TOKEN, default=""): TOKEN_SELECTOR,
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
                # The agent answers; does it take this password? (v0.9: a
                # wrong or missing password no longer makes an entry that
                # only fails later.)
                if await self._drives_status(host, port, token) == "invalid_auth":
                    errors["base"] = "invalid_auth"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=self.add_suggested_values_to_schema(
                            STEP_USER_DATA_SCHEMA, user_input
                        ),
                        errors=errors,
                    )
                # The SMART Sniffer App's replica agent (App 0.3.x): replicas
                # are made in the integration now (v0.9).
                if _app_replica(host, health):
                    return self.async_abort(reason="replicas_moved")
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                # An entry already polls this address (a discovered entry
                # added again by its address from the Control Center).
                if await self._entry_at(host, port) is not None:
                    return self.async_abort(reason="already_configured")

                # The same agent may already be configured under another
                # address, by discovery or by hand (#64, app#12). Only when
                # the agents agree on host name and drives (agent_identity).
                if await self._configured_elsewhere(
                    health.get("hostname"), host, port, token
                ):
                    return self.async_abort(reason="already_configured")

                title = f"SMART Sniffer ({host}:{port})"
                identity = await self._identity_at_creation(
                    host, port, token, health, user_input, title
                )
                return self.async_create_entry(
                    title=title, data={**user_input, **identity}
                )

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

    async def _identity_at_creation(
        self,
        host: str,
        port: int,
        token: str,
        health: Any,
        data: dict[str, Any],
        title: str,
    ) -> dict[str, Any]:
        """What a new entry stores about its agent from the start: the host
        name, drive digest and agent_id the coordinator would store at its
        first poll (agent_identity.identity_changes), and for the HA App
        whether it is the replica. The device names use the host name from
        the first entity on. Nothing from an agent before v0.8.3; no digest
        when /api/drives cannot be read."""
        drives = await self._get_json(host, port, token, "/api/drives")
        identity, _ = identity_changes({}, health, drives)
        return {**identity, **app_kind_change({**data, **identity}, title, health)}

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
        read with ``token`` only, the one the user typed (blank: no token).
        An existing entry's token never goes to the new address: it may be
        any device on the LAN advertising the service. When the new address
        cannot be read, the advertised drive count stands in.
        """
        if not isinstance(hostname, str) or not hostname:
            return None
        candidates = []
        for entry in self._async_current_entries(include_ignore=False):
            if exclude_unique_id and entry.unique_id == exclude_unique_id:
                continue
            if is_replica_data(entry.data):
                continue  # the Replica has no address
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
                self._get_json(new_host, port, token, "/api/drives"),
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

    async def _resolve(self, host: str) -> frozenset[str]:
        """The addresses a host name resolves to, 3 s at most; an address
        stands for itself; empty when it cannot be resolved."""
        try:
            return frozenset({str(ipaddress.ip_address(host))})
        except ValueError:
            pass
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM),
                timeout=3,
            )
        except (OSError, UnicodeError, asyncio.TimeoutError, TimeoutError):
            return frozenset()
        return frozenset(str(info[4][0]) for info in infos)

    async def _entry_at(
        self, host: str, port: int, exclude_entry_id: str | None = None
    ) -> ConfigEntry | None:
        """An entry (not ignored) that already polls host:port, by the same
        name or by a name that resolves to the same address; else None.

        One address and port is one agent, so no identity check is needed.
        A name that does not resolve matches only itself.
        """
        same_port = []
        for entry in self._async_current_entries(include_ignore=False):
            if exclude_entry_id is not None and entry.entry_id == exclude_entry_id:
                continue
            if is_replica_data(entry.data):
                continue  # the Replica has no address
            try:
                entry_port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
            except (TypeError, ValueError):
                continue
            entry_host = str(entry.data.get(CONF_HOST, "") or "")
            if entry_port != port or not entry_host:
                continue
            if norm_host(entry_host) == norm_host(host):
                return entry
            same_port.append((entry, entry_host))
        if not same_port:
            return None
        wanted = await self._resolve(host)
        if not wanted:
            return None
        found = await asyncio.gather(*(self._resolve(h) for _, h in same_port))
        return next((e for (e, _), addrs in zip(same_port, found, strict=True)
                     if addrs & wanted), None)

    def _migrate_legacy_unique_ids(self, hostname: str, host: str, port: int) -> None:
        """Migrate existing config entries from IP-based to hostname-based unique IDs.

        Before v0.4.24, unique IDs were "{ip}:{port}". This caused duplicates
        when mDNS reflectors or multi-homed hosts advertised multiple IPs.
        Now we use "smartha-{hostname}" for stable deduplication.

        This scans existing entries and updates any that match by IP or hostname
        so the new discovery is properly deduplicated.
        """
        entries = self._async_current_entries()
        for entry in entries:
            if entry.domain != DOMAIN or is_replica_data(entry.data):
                continue
            old_uid = entry.unique_id or ""
            # Already migrated.
            if old_uid.startswith("smartha-"):
                continue
            # Only entries discovery made. A hand-added (or Control Center)
            # entry keeps its "host:port" unique_id; an entry at the
            # advertised address is caught by _entry_at instead.
            if entry.source != SOURCE_ZEROCONF:
                continue
            new_uid = f"smartha-{hostname}"
            if any(other.unique_id == new_uid for other in entries):
                continue  # taken: two entries must not share a unique_id
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
                    unique_id=new_uid,
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

        # An entry under another unique_id already polls this address.
        if await self._entry_at(host, port) is not None:
            return self.async_abort(reason="already_configured")

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
            agent_version and agent_is_outdated(agent_version, MIN_AGENT_VERSION)
        )
        self._agent_version = agent_version

        # Set a nice title for the discovery notification.
        self.context["title_placeholders"] = {
            "hostname": self._discovery_hostname,
        }
        # What the SMART Sniffer panel's Network list shows of this agent
        # while the discovery waits (panel_net.discovered_rows).
        self.context[DISCOVERY_CONTEXT] = {
            "host": host,
            "port": port,
            "hostname": hostname,
            "drives": properties.get("drives"),
            "auth": self._discovery_auth,
            "version": agent_version,
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
                health = await self._test_connection(
                    self._discovery_host, self._discovery_port, token
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during zeroconf confirm")
                errors["base"] = "unknown"
            else:
                # The card may have waited while the same agent was added
                # another way (the Control Center): the duplicate checks
                # again, as they stand now.
                password = await self._drives_status(
                    self._discovery_host, self._discovery_port, token
                )
                if password == "invalid_auth":
                    errors["base"] = "invalid_auth"
                elif await self._added_since_discovery(token):
                    return self.async_abort(reason="already_configured")
                else:
                    return await self._create_discovered(token, health)

        # Auth enabled — show form with token field.
        # No auth — show confirmation with no input fields (just Submit).
        if self._discovery_auth or errors.get("base") == "invalid_auth":
            schema = vol.Schema({vol.Optional(CONF_TOKEN, default=""): TOKEN_SELECTOR})
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
                f"\n\n⚠️ This agent is running **v{display_version(self._agent_version)}** "
                f"but the integration requires at least **v{MIN_AGENT_VERSION}**. "
                "You can still add it, but please update the agent afterwards."
            )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _added_since_discovery(self, token: str) -> bool:
        """At confirm: is this agent now configured, by its unique_id, by an
        entry at its address, or by an entry the agent itself confirms
        (host name and drives, _configured_elsewhere)? The discovered
        address is read with the token typed here only: a blank field sends
        none, never a configured entry's token."""
        host, port = self._discovery_host, self._discovery_port
        hostname = self._discovery_hostname
        if self.unique_id and any(
            entry.unique_id == self.unique_id for entry in self._async_current_entries()
        ):
            return True
        if await self._entry_at(host, port) is not None:
            return True
        if hostname == host:
            return False
        return (
            await self._configured_elsewhere(
                hostname,
                host,
                port,
                token,
                advertised_drive_count=self._discovery_drives,
                exclude_unique_id=self.unique_id or "",
            )
            is not None
        )

    async def _abort_for_known_entry(
        self, entry: ConfigEntry, host: str, port: int
    ) -> None:
        """Abort a rediscovery of a configured agent, moving the entry to the
        advertised address only when the agent is shown to have moved there
        (_may_move)."""
        if await self._may_move(entry, host, port):
            _LOGGER.info(
                "SMART Sniffer agent %s: %s:%s does not answer, moving to the "
                "advertised %s:%s",
                entry.unique_id, entry.data.get(CONF_HOST), entry.data.get(CONF_PORT),
                host, port,
            )
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )
        self._abort_if_unique_id_configured()

    async def _may_move(self, entry: ConfigEntry, host: str, port: int) -> bool:
        """Whether a rediscovered entry's host may move to host:port.

        Only an entry discovery made (unique_id "smartha-{hostname}", titled
        with the host name): a hand-added entry's unique_id and title carry
        its address, and would no longer match it. And only when all hold:
          a. the entry's own address fails a connection test;
          b. the advertised address passes one;
          c. the agent there reports the host name and the drive serials the
             entry last knew (stored when the entry was created and by the
             coordinator from the entry's own polls; nothing stored, no
             move);
          d. no other entry already polls that address.
        The advertised address is probed without a token, so an entry's
        token never goes to an address that came from discovery. An entry
        with a token is therefore never moved here: it stays where it is.
        """
        if entry.source != SOURCE_ZEROCONF or not (entry.unique_id or "").startswith(
            UNIQUE_ID_PREFIX
        ) or is_replica_data(entry.data):
            return False
        entry_host = str(entry.data.get(CONF_HOST, "") or "")
        try:
            entry_port = int(entry.data.get(CONF_PORT, port))
        except (TypeError, ValueError):
            return False
        if not entry_host or (entry_host == host and entry_port == port):
            return False
        known_name = norm_host(entry.data.get(ENTRY_AGENT_HOSTNAME))
        known_drives = entry.data.get(ENTRY_AGENT_DRIVES)
        if not known_name or not known_drives:
            return False
        if entry.data.get(CONF_TOKEN):
            return False  # its token is not sent to an advertised address
        if await self._reachable(entry_host, entry_port, ""):  # a
            return False
        try:
            health = await self._test_connection(host, port, "")  # b
        except Exception:  # noqa: BLE001 - any failure means "not usable"
            return False
        if norm_host(health.get("hostname")) != known_name:  # c
            _LOGGER.debug("Not moving %s to %s:%s: another host name",
                          entry.unique_id, host, port)
            return False
        drives = await self._get_json(host, port, "", "/api/drives")
        if drive_digest(drives) != known_drives:
            _LOGGER.debug("Not moving %s to %s:%s: other drives",
                          entry.unique_id, host, port)
            return False
        if await self._entry_at(host, port, exclude_entry_id=entry.entry_id):  # d
            _LOGGER.debug("Not moving %s to %s:%s: another entry polls it",
                          entry.unique_id, host, port)
            return False
        return True

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

    async def _drives_status(self, host: str, port: int, token: str) -> str:
        """Whether the agent gives its drives with this password: "ok",
        "invalid_auth" (401 or 403), or "cannot_connect" (anything else). The
        health check needs no password, so this is the password check."""
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with session.get(
                f"http://{host}:{port}/api/drives",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (401, 403):
                    return "invalid_auth"
                resp.raise_for_status()
                return "ok"
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
            return "cannot_connect"

    async def _create_discovered(self, token: str, health: dict[str, Any]) -> ConfigFlowResult:
        """The entry a confirmed discovery makes."""
        title = f"SMART Sniffer ({self._discovery_hostname})"
        data = {
            CONF_HOST: self._discovery_host,
            CONF_PORT: self._discovery_port,
            CONF_TOKEN: token,
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        }
        identity = await self._identity_at_creation(
            self._discovery_host, self._discovery_port, token, health, data, title
        )
        return self.async_create_entry(title=title, data={**data, **identity})

    # ------------------------------------------------------------------
    # The SMART Sniffer App's agent, from the Supervisor's discovery (v0.9)
    # ------------------------------------------------------------------

    async def async_step_hassio(self, discovery_info: Any) -> ConfigFlowResult:
        """The SMART Sniffer App (0.4 and later) tells Home Assistant where
        this box's agent is: service smart_sniffer, config {host, port} and
        optionally token and agent_id. Offered as HAOS on HA's Discovered
        card unless that agent is already an entry.

        ``discovery_info`` is a HassioServiceInfo; only its ``config``,
        ``name`` and ``slug`` are read, so nothing from the hassio component
        is imported here. The unique id is "host:port", never the discovery
        uuid, so removing the discovery never removes the entry.
        """
        config = getattr(discovery_info, "config", None)
        config = config if isinstance(config, dict) else {}
        host = str(config.get(CONF_HOST) or "").strip()
        try:
            port = int(config.get(CONF_PORT) or DEFAULT_PORT)
        except (TypeError, ValueError):
            return self.async_abort(reason="cannot_connect")
        token = str(config.get(CONF_TOKEN) or "")
        if not host:
            return self.async_abort(reason="cannot_connect")
        try:
            health = await self._test_connection(host, port, token)
        except Exception:  # noqa: BLE001 - not answering, or not an agent
            return self.async_abort(reason="cannot_connect")
        if _app_replica(host, health):
            return self.async_abort(reason="replicas_moved")
        agent_id = norm_agent_id(config.get("agent_id")) or norm_agent_id(health.get("agent_id"))
        hostname = display_hostname(health.get("hostname")) or ""
        _record_app_facts(self.hass, discovery_info, host, port, agent_id)

        # Already an entry? agent_id first, then the App's own entry, then
        # the address (plan v5 3.5). A token the App sends is kept current.
        existing = self._hassio_known_entry(agent_id, hostname, host)
        if existing is not None:
            self._update_token(existing, token)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured(updates={CONF_TOKEN: token} if token else None)
        existing = await self._entry_at(host, port)
        if existing is not None:
            self._update_token(existing, token)
            return self.async_abort(reason="already_configured")

        label = _hassio_label(hostname, host)
        self._hassio = {
            "host": host, "port": port, "token": token, "label": label,
            "app": str(getattr(discovery_info, "name", "") or "SMART Sniffer App"),
            "agent_id": agent_id, "hostname": hostname,
            "slug": str(getattr(discovery_info, "slug", "") or ""),
            "auth": await self._drives_status(host, port, token) == "invalid_auth",
        }
        self.context["title_placeholders"] = {"hostname": label}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """HA's standard discovery confirm: "Create sensors and alerts for
        HAOS?", with a password field only when the agent wants one."""
        info = self._hassio
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or info["token"])
            try:
                health = await self._test_connection(info["host"], info["port"], token)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Supervisor discovery confirm")
                errors["base"] = "unknown"
            else:
                status = await self._drives_status(info["host"], info["port"], token)
                if status == "invalid_auth":
                    errors["base"] = "invalid_auth"
                    info["auth"] = True
                else:
                    # The card may have waited while the agent was added
                    # another way: the duplicate checks again, as they stand.
                    agent_id = norm_agent_id(health.get("agent_id")) or info["agent_id"]
                    hostname = display_hostname(health.get("hostname")) or info["hostname"]
                    if (self._hassio_known_entry(agent_id, hostname, info["host"]) is not None
                            or await self._entry_at(info["host"], info["port"]) is not None):
                        return self.async_abort(reason="already_configured")
                    self._abort_if_unique_id_configured()
                    title = f"SMART Sniffer ({info['label']})"
                    data = {
                        CONF_HOST: info["host"],
                        CONF_PORT: info["port"],
                        CONF_TOKEN: token,
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        # The App's agent, whatever its container is named.
                        ENTRY_APP_SLUG: info.get("slug") or "app",
                    }
                    identity = await self._identity_at_creation(
                        info["host"], info["port"], token, health, data, title)
                    return self.async_create_entry(title=title, data={**data, **identity})

        if info["auth"]:
            schema = vol.Schema({vol.Optional(CONF_TOKEN, default=""): TOKEN_SELECTOR})
        else:
            schema = vol.Schema({})
            self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "label": info["label"], "app": info["app"],
                "host": info["host"], "port": str(info["port"]),
            },
        )

    def _hassio_known_entry(
        self, agent_id: str, hostname: str, host: str
    ) -> ConfigEntry | None:
        """The entry that already reads the discovered agent, by agent_id,
        else by being the App's agent entry; None otherwise (the address is
        checked by the caller).

        1. An entry whose stored agent_id is the discovered one.
        2. The App's agent entry (host_name.is_app_entry, not its replica),
           only when agent_id cannot tell (either side has none) and the
           entry names the same App container as the discovered agent. So a
           second App on this box (a local dev build: another container name,
           agent_id and port) is still offered, while a HAOS entry added at a
           LAN address or another port before agent_ids were stored is not
           offered twice.
        """
        current = [entry for entry in self._async_current_entries(include_ignore=False)
                   if not is_replica_data(entry.data)]  # the Replica has no agent
        if agent_id:
            for entry in current:
                if norm_agent_id(entry.data.get(ENTRY_AGENT_ID)) == agent_id:
                    return entry
        found = {n for n in (norm_host(hostname), norm_host(host)) if n}
        for entry in current:
            if entry.data.get(ENTRY_AGENT_MOCK) is True:
                continue
            if not is_app_entry(entry.data, entry.title):
                continue
            stored = norm_agent_id(entry.data.get(ENTRY_AGENT_ID))
            if stored and agent_id:
                continue  # agent_id decided above: another agent
            if app_names(entry.data, entry.title) & found:
                return entry
        return None

    def _update_token(self, entry: ConfigEntry, token: str) -> None:
        """Keep an entry's password as the App sends it, when it sends one."""
        if token and entry.data.get(CONF_TOKEN) != token:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_TOKEN: token})

    # ------------------------------------------------------------------
    # The SMART Sniffer panel's Add (v0.9)
    # ------------------------------------------------------------------

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """An agent added from the SMART Sniffer panel that wants a password
        the user has not given yet (the lock in its Network row).

        The user step's checks, except the password one: the agent must
        answer its health, and must not be configured already (by its
        address, unique id or as the same agent elsewhere). The entry is
        made without a password; its first poll is refused, Home Assistant
        asks for the password (reauth), and the panel shows the machine's
        password field, as the Control Center always did. A discovered
        agent keeps its discovery unique id and title ("hostname" given).
        "drives", the drive count the panel's scan or discovery read, stands
        in for the drives the new address will not show without a password
        when the same agent may be configured elsewhere.
        """
        if is_replica_data(import_data):
            return await self._create_replica()
        host = str(import_data[CONF_HOST])
        port = int(import_data.get(CONF_PORT, DEFAULT_PORT))
        token = str(import_data.get(CONF_TOKEN) or "")
        hostname = import_data.get("hostname")
        # A field machine handed over by the SMART Sniffer App (panel_import):
        # made even when it does not answer now; it then shows offline with
        # its address until it does, as the App showed it.
        from_app = import_data.get(IMPORT_FROM_APP) is True
        try:
            health = await self._test_connection(host, port, token)
        except Exception:  # noqa: BLE001 - not answering, or not an agent
            if not from_app:
                return self.async_abort(reason="cannot_connect")
            health = None
        if health is not None and _app_replica(host, health):
            return self.async_abort(reason="replicas_moved")
        unique_id = f"{UNIQUE_ID_PREFIX}{hostname}" if hostname else f"{host}:{port}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        if await self._entry_at(host, port) is not None:
            return self.async_abort(reason="already_configured")
        if health is not None and await self._configured_elsewhere(
                health.get("hostname"), host, port, token,
                advertised_drive_count=import_data.get("drives")):
            return self.async_abort(reason="already_configured")
        title = f"SMART Sniffer ({hostname})" if hostname else f"SMART Sniffer ({host}:{port})"
        data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_TOKEN: token,
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        }
        identity = {} if health is None else await self._identity_at_creation(
            host, port, token, health, data, title)
        return self.async_create_entry(title=title, data={**data, **identity})

    async def _create_replica(self) -> ConfigFlowResult:
        """The Replica machine (v0.9), made by the panel's first + > Replica
        add: one per Home Assistant, no address (source.py)."""
        await self.async_set_unique_id(REPLICA_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        if any(is_replica_data(e.data) for e in self._async_current_entries()):
            return self.async_abort(reason="replica_exists")
        from .replica import new_agent_id  # noqa: PLC0415

        return self.async_create_entry(
            title=REPLICA_TITLE, data=replica_entry_data(new_agent_id(), DEFAULT_SCAN_INTERVAL))

    # ------------------------------------------------------------------
    # Password refused or missing (v0.9)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """The agent refused the entry's password, or wants one. The entry
        is found by the flow's context (Home Assistant 2024.4 has no
        _get_reauth_entry)."""
        return await self.async_step_reauth_confirm()

    def _reauth_entry(self) -> ConfigEntry | None:
        return self.hass.config_entries.async_get_entry(self.context.get("entry_id", ""))

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._reauth_entry()
        if entry is None or is_replica_data(entry.data):
            return self.async_abort(reason="reauth_entry_gone")
        host = str(entry.data.get(CONF_HOST, ""))
        port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "")
            result = await self._drives_status(host, port, token)
            if result == "ok":
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_TOKEN: token}
                )
                # A loaded entry reloads from its update listener (the token
                # is a setting); one whose setup was refused is reloaded here.
                if entry.state is not ConfigEntryState.LOADED:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            errors["base"] = result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_TOKEN, default=""): TOKEN_SELECTOR}),
            errors=errors,
            description_placeholders={"host": f"{host}:{port}", "name": entry.title},
        )


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

        if is_replica_data(self.config_entry.data):
            return await self._replica_settings(user_input)

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
                ): TOKEN_SELECTOR,
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

    async def _replica_settings(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """The Replica's settings: its poll interval and force update only
        (it has no address or password)."""
        if user_input is not None:
            new_data = {**self.config_entry.data,
                        **{k: v for k, v in user_input.items()
                           if k in (CONF_SCAN_INTERVAL, CONF_FORCE_UPDATE)}}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            return self.async_create_entry(title="", data={})
        current = self.config_entry.data
        schema = vol.Schema(
            {
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
        return self.async_show_form(step_id="settings", data_schema=schema, errors={})

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


HASSIO_FACTS = f"{DOMAIN}_app_facts"


def _record_app_facts(
    hass: Any, discovery_info: Any, host: str, port: int, agent_id: str
) -> None:
    """What the Supervisor's discovery said about an App's agent, for this
    run only, by App slug (plan v5 3.5 step 1). No token is kept here."""
    try:
        facts = hass.data.setdefault(HASSIO_FACTS, {})
        slug = str(getattr(discovery_info, "slug", "") or "")
        facts[slug] = {"host": host, "port": port, "agent_id": agent_id,
                       "name": str(getattr(discovery_info, "name", "") or "")}
    except Exception:  # noqa: BLE001 - facts are a convenience only
        _LOGGER.debug("SMART Sniffer: could not record the App's discovery facts")


def _hassio_label(hostname: str, host: str) -> str:
    """The name a discovered App agent is offered and created under: "HAOS"
    for the SMART Sniffer App's container, else the agent's host name (a
    local dev build of the App has another container name)."""
    for name in (hostname, host):
        if name and APP_HOST.match(name.lower()):
            return APP_AGENT_LABEL
    return clean_host(hostname) or address_label(host) or host


def _app_replica(host: str, health: Any) -> bool:
    """Whether an agent is the SMART Sniffer App's replica agent (App
    0.3.x): the App's container name, and "mock": true in its health."""
    if not isinstance(health, dict) or health.get("mock") is not True:
        return False
    return is_app_entry({CONF_HOST: host}, None, health.get("hostname"))
