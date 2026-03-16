"""Config flow and options flow for SMART Sniffer integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

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
            except aiohttp.ClientError:
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

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via mDNS/Zeroconf."""
        host = str(discovery_info.ip_address)
        port = discovery_info.port
        properties = discovery_info.properties

        # Deduplicate — don't prompt for agents already configured.
        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()

        # Stash discovery data for the confirmation step.
        self._discovery_host = host
        self._discovery_port = port
        self._discovery_hostname = properties.get("hostname", host)
        self._discovery_drives = properties.get("drives", "?")
        self._discovery_auth = properties.get("auth", "0") == "1"

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
            except aiohttp.ClientError:
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

        # Build schema — only show token field if agent has auth enabled.
        if self._discovery_auth:
            schema = vol.Schema({vol.Optional(CONF_TOKEN, default=""): str})
        else:
            schema = vol.Schema({})

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "hostname": self._discovery_hostname,
                "host": self._discovery_host,
                "port": str(self._discovery_port),
                "drives": str(self._discovery_drives),
            },
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

    Allows changing the bearer token, polling interval, and port without
    having to delete and re-add the integration.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form pre-filled with current values."""
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
            except aiohttp.ClientError:
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
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
