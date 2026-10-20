"""Passwords (v0.9): checked when an agent is added, asked again on 401.

- The coordinator turns a refused password (401 or 403 on the drives) into
  ConfigEntryAuthFailed, so Home Assistant asks for it (reauth) instead of
  retrying forever; any other failure stays UpdateFailed.
- The user and discovery steps check the password against /api/drives and
  show invalid_auth; no entry is made that would only fail later.
- The reauth step checks the new password, stores it, and reloads an entry
  whose setup was refused (one that is loaded reloads from its listener).
- The import step (the panel's Add of an agent that wants a password nobody
  gave yet) makes the entry without one, after the user step's other checks.

Real Home Assistant classes; the agents and config entries are stand-ins.
Skipped without Home Assistant.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

import aiohttp  # noqa: E402

from homeassistant import config_entries  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from tests.test_config_flow_under_ha import (  # noqa: E402
    LAN,
    _discovery,
    _entry,
    _flow,
    _run,
)
from tests.test_device_names_under_registry import _make_entry, _new_hass  # noqa: E402

coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")
config_flow = importlib.import_module("custom_components.smart_sniffer.config_flow")

SECRET = ("nas1", ["SER-1"], "letmein")


class _Session:
    def __init__(self, status):
        self.status = status

    def get(self, url, headers=None, timeout=None):
        session = self

        class _Resp:
            status = session.status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                if self.status >= 400:
                    raise aiohttp.ClientResponseError(SimpleNamespace(real_url="http://10.0.0.1/api/drives"), (), status=self.status)

            async def json(self):
                return []

        return _Resp()


@pytest.mark.parametrize(("status", "error"), [(401, ConfigEntryAuthFailed),
                                               (403, ConfigEntryAuthFailed),
                                               (500, UpdateFailed)])
def test_refused_password_asks_for_it(tmp_path, monkeypatch, status, error):
    monkeypatch.setattr(coordinator_mod, "async_get_clientsession", lambda hass: _Session(status))

    async def main():
        hass = await _new_hass(tmp_path)
        try:
            entry = _make_entry("E1", "SMART Sniffer (nas1)",
                                {"host": LAN, "port": 9099, "token": "old", "scan_interval": 60})
            hass.config_entries._entries["E1"] = entry
            config_entries.current_entry.set(entry)
            coordinator = coordinator_mod.SmartSnifferCoordinator(hass, entry)
            with pytest.raises(error) as err:
                await coordinator._async_update_data()
            if error is ConfigEntryAuthFailed:
                assert "refused the password" in str(err.value)
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


def test_user_step_checks_the_password():
    flow = _flow([], "user", {f"{LAN}:9099": SECRET})
    flow.net.agents[f"{LAN}:9099"] = ("nas1", ["SER-1"], "letmein")
    flow._test_connection = _health_open(flow)
    result = _run(flow.async_step_user({"host": LAN, "port": 9099, "token": "wrong",
                                        "scan_interval": 60}))
    assert result["type"] == "form" and result["errors"] == {"base": "invalid_auth"}
    result = _run(flow.async_step_user({"host": LAN, "port": 9099, "token": "letmein",
                                        "scan_interval": 60}))
    assert result["type"] == "create_entry" and result["data"]["token"] == "letmein"


def _health_open(flow):
    """The agent's /api/health needs no password (agent main.go)."""
    async def health(host, port, token):
        flow.net.calls.append(f"{host}:{port}/api/health")
        if f"{host}:{port}" not in flow.net.agents:
            raise aiohttp.ClientError("unreachable")
        return {"status": "ok", "hostname": flow.net.agents[f"{host}:{port}"][0]}
    return health


def test_discovery_confirm_checks_the_password_and_asks_for_it():
    flow = _flow([], "zeroconf", {f"{LAN}:9099": SECRET})
    flow._test_connection = _health_open(flow)
    result = _run(flow.async_step_zeroconf(_discovery("nas1", LAN, preferred=LAN)))
    assert result["step_id"] == "zeroconf_confirm"
    # The agent says no auth in its TXT record, but refuses: the form now asks.
    result = _run(flow.async_step_zeroconf_confirm({}))
    assert result["errors"] == {"base": "invalid_auth"}
    assert "token" in [str(k) for k in result["data_schema"].schema]
    result = _run(flow.async_step_zeroconf_confirm({"token": "letmein"}))
    assert result["type"] == "create_entry"
    # The panel's Network list reads what the discovery found.
    facts = flow.context["smart_sniffer_agent"]
    assert facts["host"] == LAN and facts["hostname"] == "nas1" and facts["auth"] is False


class _Reloads:
    def __init__(self, flow, entry):
        self.reloads = []
        entries = flow.hass.config_entries
        entries.async_get_entry = lambda entry_id: entry if entry_id == entry.entry_id else None
        entries.async_schedule_reload = self.reloads.append


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_ERROR, ConfigEntryState.LOADED])
def test_reauth_stores_the_new_password(state):
    entry = _entry("E1", f"{LAN}:9099", LAN, token="old")
    entry.state = state
    flow = _flow([entry], "reauth", {f"{LAN}:9099": SECRET})
    flow.context["entry_id"] = "E1"
    reloads = _Reloads(flow, entry)
    result = _run(flow.async_step_reauth(dict(entry.data)))
    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"
    result = _run(flow.async_step_reauth_confirm({"token": "nope"}))
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["token"] == "old"
    result = _run(flow.async_step_reauth_confirm({"token": "letmein"}))
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["token"] == "letmein"
    # A loaded entry reloads from its own update listener (a token change).
    assert reloads.reloads == ([] if state is ConfigEntryState.LOADED else ["E1"])


def test_reauth_of_a_deleted_entry_ends():
    flow = _flow([], "reauth", {})
    flow.context["entry_id"] = "gone"
    flow.hass.config_entries.async_get_entry = lambda entry_id: None
    result = _run(flow.async_step_reauth({}))
    assert result["type"] == "abort" and result["reason"] == "reauth_entry_gone"


def test_import_adds_an_agent_that_wants_a_password_without_one():
    flow = _flow([], "import", {f"{LAN}:9099": SECRET})
    flow._test_connection = _health_open(flow)
    result = _run(flow.async_step_import({"host": LAN, "port": 9099, "token": "",
                                          "hostname": "nas1"}))
    assert result["type"] == "create_entry"
    assert result["data"]["token"] == "" and result["title"] == "SMART Sniffer (nas1)"
    assert flow.context["unique_id"] == "smartha-nas1"


def test_import_keeps_the_user_steps_duplicate_checks():
    existing = _entry("E1", f"{LAN}:9099", LAN)
    flow = _flow([existing], "import", {f"{LAN}:9099": SECRET})
    flow._test_connection = _health_open(flow)
    result = _run(flow.async_step_import({"host": LAN, "port": 9099}))
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    flow = _flow([], "import", {})
    result = _run(flow.async_step_import({"host": "10.9.9.9", "port": 9099}))
    assert result["reason"] == "cannot_connect"


def test_strings_have_the_new_steps():
    import json
    from pathlib import Path

    root = Path(config_flow.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        config = json.loads((root / name).read_text())["config"]
        assert "reauth_confirm" in config["step"]
        assert "invalid_auth" in config["error"]
        assert {"reauth_successful", "reauth_entry_gone", "cannot_connect"} <= set(config["abort"])


def _token_field(schema):
    """The validator of the token field in a form's schema."""
    return next(v for k, v in schema.schema.items() if str(k) == "token")


def _is_password(validator):
    from homeassistant.helpers.selector import TextSelector

    return isinstance(validator, TextSelector) and validator.config.get("type") == "password"


def test_every_password_field_shows_dots(tmp_path, monkeypatch):
    if hasattr(config_entries, "report_usage"):  # a log line in an install, as in thresholds
        monkeypatch.setattr(config_entries, "report_usage", lambda *a, **kw: None)
    assert _is_password(_token_field(config_flow.STEP_USER_DATA_SCHEMA))

    flow = _flow([], "zeroconf", {f"{LAN}:9099": SECRET})
    flow._test_connection = _health_open(flow)
    _run(flow.async_step_zeroconf(_discovery("nas1", LAN, preferred=LAN)))
    result = _run(flow.async_step_zeroconf_confirm({}))
    assert _is_password(_token_field(result["data_schema"]))

    entry = _entry("E1", f"{LAN}:9099", LAN, token="old")
    flow = _flow([entry], "reauth", {f"{LAN}:9099": SECRET})
    flow.context["entry_id"] = "E1"
    _Reloads(flow, entry)
    result = _run(flow.async_step_reauth(dict(entry.data)))
    assert _is_password(_token_field(result["data_schema"]))

    flow = _flow([], "hassio", {})
    flow._hassio = {"host": LAN, "port": 9099, "auth": True, "token": "",
                    "label": "HAOS", "app": "SMART Sniffer App"}
    result = _run(flow.async_step_hassio_confirm())
    assert _is_password(_token_field(result["data_schema"]))

    async def settings_form():
        hass = await _new_hass(tmp_path)
        try:
            entry = _make_entry("E2", "SMART Sniffer (nas1)",
                                {"host": LAN, "port": 9099, "token": "stored", "scan_interval": 60})
            hass.config_entries._entries["E2"] = entry
            options = config_flow.SmartSnifferOptionsFlow(entry)
            options.hass, options.handler, options.flow_id = hass, "E2", "options"
            return await options.async_step_settings()
        finally:
            await hass.async_stop(force=True)
    result = asyncio.run(settings_form())
    assert _is_password(_token_field(result["data_schema"]))


WAN = "203.0.113.7"


def test_import_uses_the_rows_drive_count_for_an_agent_added_elsewhere():
    """A password-protected agent shows no drives without its password: the
    count the panel's row read says it is the entry at LAN."""
    agent = ("nas1", ["SER-1", "SER-2"], "letmein")
    agents = {f"{LAN}:9099": agent, f"{WAN}:9099": agent}
    existing = _entry("E1", f"{LAN}:9099", LAN, token="letmein")
    flow = _flow([existing], "import", agents)
    flow._test_connection = _health_open(flow)
    result = _run(flow.async_step_import({"host": WAN, "port": 9099, "drives": 2}))
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert flow.net.tokens_to(WAN) == {""}  # the entry's password stays home
    # Without a count (or another one) it is offered, as before.
    for data in ({}, {"drives": 3}):
        flow = _flow([existing], "import", agents)
        flow._test_connection = _health_open(flow)
        result = _run(flow.async_step_import({"host": WAN, "port": 9099, **data}))
        assert result["type"] == "create_entry"


def test_panel_add_gives_the_import_step_the_rows_drive_count():
    panel_net = importlib.import_module("custom_components.smart_sniffer.panel_net")
    inits = []

    class _Flows:
        async def async_init(self, domain, context=None, data=None):
            inits.append((context["source"], data))
            if context["source"] == "user":
                return {"type": "form", "flow_id": "user1"}
            return {"type": "abort", "reason": "already_configured"}

        async def async_configure(self, flow_id, user_input):
            return {"type": "form", "flow_id": flow_id, "errors": {"base": "invalid_auth"}}

        def async_abort(self, flow_id):
            return None

    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
        flow=_Flows(), async_entries=lambda *a, **kw: []))
    out = asyncio.run(panel_net._add_one(hass, {"address": WAN, "port": 9099, "drives": 2}))
    assert out["result"] == "already_added"
    assert inits[-1] == ("import", {"host": WAN, "port": 9099, "token": "", "drives": 2})
    # A row without a count sends none.
    asyncio.run(panel_net._add_one(hass, {"address": WAN, "port": 9099, "drives": None}))
    assert inits[-1] == ("import", {"host": WAN, "port": 9099, "token": ""})
