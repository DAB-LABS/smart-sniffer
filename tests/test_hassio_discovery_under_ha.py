"""The SMART Sniffer App's agent from the Supervisor's discovery (v0.9).

App 0.4 posts service smart_sniffer with {host, port} (later also token and
agent_id). The integration reads the agent's /api/health and offers it on
HA's Discovered card as HAOS, unless it is already an entry: by agent_id
first, then as the App's own entry, then by address (plan v5 3.5). A local
dev build of the App on the same box is a second agent (another container
name, agent_id and port) and is offered under its own name.

Driven with the stand-ins of test_config_flow_under_ha.py; addresses and ids
are invented. Skipped without Home Assistant.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_HASSIO  # noqa: E402

try:  # noqa: E402
    from homeassistant.helpers.service_info.hassio import HassioServiceInfo
except ImportError:  # HA < 2025.x
    from homeassistant.components.hassio.discovery import HassioServiceInfo

from tests.test_config_flow_under_ha import _entry, _flow, _run  # noqa: E402

APP = "0449a086-smart-sniffer-agent"
DEV = "local-smart-sniffer-agent-dev"
APP_ID = "a" * 32
DEV_ID = "d" * 32
LAN = "192.168.40.20"
UUID = "f" * 32

APP_AGENT = (APP, ["SER-H1", "SER-H2"], "", {"agent_id": APP_ID})
DEV_AGENT = (DEV, ["SER-H1", "SER-H2"], "", {"agent_id": DEV_ID})


def _info(host=APP, port=9099, name="SMART Sniffer App", slug="0449a086_smart_sniffer_agent",
          **more):
    return HassioServiceInfo(config={"host": host, "port": port, **more}, name=name,
                             slug=slug, uuid=UUID)


def _discover(entries, agents, info=None):
    flow = _flow(entries, SOURCE_HASSIO, agents)
    net = flow.net

    async def health(host, port, token):
        # An agent's /api/health needs no password; only its drives do.
        agent = net.agents.get(f"{host}:{port}")
        return await net.test_connection(host, port, agent[2] if agent else token)

    flow._test_connection = health
    return flow, _run(flow.async_step_hassio(info or _info()))


def _app_entry(host=APP, port=9099, unique_id=None, **data):
    return _entry("e-app", unique_id or f"{host}:{port}", host, port, **data)


# --- offered --------------------------------------------------------------------


def test_the_apps_agent_is_offered_as_haos_and_created_with_host_port():
    flow, result = _discover([], {f"{APP}:9099": APP_AGENT})
    assert result["type"] == "form" and result["step_id"] == "hassio_confirm"
    assert flow.context["title_placeholders"] == {"hostname": "HAOS"}
    assert result["description_placeholders"]["label"] == "HAOS"
    assert result["data_schema"].schema == {}  # no password asked
    created = _run(flow.async_step_hassio_confirm({}))
    assert created["type"] == "create_entry"
    assert created["title"] == "SMART Sniffer (HAOS)"
    assert created["data"]["host"] == APP and created["data"]["port"] == 9099
    assert created["data"]["agent_id"] == APP_ID
    assert created["data"]["agent_mock"] is False
    # Never the discovery uuid: HA removes entries whose unique id is the
    # uuid when the App deletes its message (hassio/discovery.py).
    assert flow.unique_id == f"{APP}:9099" and flow.unique_id != UUID


def test_the_apps_facts_are_recorded_for_this_run_without_a_token():
    flow, _ = _discover([], {f"{APP}:9099": (*APP_AGENT[:2], "pw", APP_AGENT[3])},
                        _info(token="pw"))
    facts = flow.hass.data["smart_sniffer_app_facts"]["0449a086_smart_sniffer_agent"]
    assert facts == {"host": APP, "port": 9099, "agent_id": APP_ID, "name": "SMART Sniffer App"}


def test_an_agent_that_wants_a_password_asks_for_it():
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "secret", {"agent_id": APP_ID})}
    flow, result = _discover([], agents)
    assert result["type"] == "form" and "token" in {str(k) for k in result["data_schema"].schema}
    wrong = _run(flow.async_step_hassio_confirm({"token": "nope"}))
    assert wrong["type"] == "form" and wrong["errors"] == {"base": "invalid_auth"}
    created = _run(flow.async_step_hassio_confirm({"token": "secret"}))
    assert created["type"] == "create_entry" and created["data"]["token"] == "secret"


def test_a_token_from_the_app_is_used_without_asking():
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "secret", {"agent_id": APP_ID})}
    flow, result = _discover([], agents, _info(token="secret"))
    assert result["data_schema"].schema == {}
    created = _run(flow.async_step_hassio_confirm({}))
    assert created["data"]["token"] == "secret"


# --- already an entry -------------------------------------------------------------


def test_same_agent_id_at_a_lan_address_aborts():
    entry = _app_entry(LAN, 9099, agent_id=APP_ID, agent_hostname=APP)
    flow, result = _discover([entry], {f"{APP}:9099": APP_AGENT})
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    assert flow.hass.config_entries.updates == []


def test_same_agent_id_takes_the_apps_new_token():
    entry = _app_entry(LAN, 9099, agent_id=APP_ID, token="old")
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "new", {"agent_id": APP_ID})}
    flow, result = _discover([entry], agents, _info(token="new"))
    assert result["reason"] == "already_configured"
    assert flow.hass.config_entries.updates == [("e-app", {"data": {**entry.data, "token": "new"}})]
    assert entry.data["token"] == "new"


def test_the_apps_entry_without_an_agent_id_at_a_remapped_port_aborts():
    """Added by hand at the box's LAN address and another port before
    agent_ids were stored: the App's container name says it is this App."""
    entry = _app_entry(LAN, 19099, agent_hostname=APP, agent_mock=False)
    _, result = _discover([entry], {f"{APP}:9099": APP_AGENT})
    assert result["reason"] == "already_configured"


def test_an_agent_without_an_agent_id_matches_the_apps_entry_by_name():
    entry = _app_entry(LAN, 19099, agent_hostname=APP, agent_id=APP_ID)
    _, result = _discover([entry], {f"{APP}:9099": (APP, ["SER-H1"], "")})
    assert result["reason"] == "already_configured"


def test_the_apps_old_replica_entry_is_not_the_apps_agent():
    replica = _app_entry(APP, 9100, agent_hostname=APP, agent_mock=True)
    _, result = _discover([replica], {f"{APP}:9099": APP_AGENT})
    assert result["type"] == "form"


def test_the_replica_machine_is_never_a_match():
    replica = _entry("e-rep", "replica", "replica.invalid", kind="replica", agent_id=APP_ID)
    _, result = _discover([replica], {f"{APP}:9099": APP_AGENT})
    assert result["type"] == "form"


def test_same_address_aborts_and_takes_the_token():
    entry = _app_entry(APP, 9099, token="old")
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "new")}
    flow, result = _discover([entry], agents, _info(token="new"))
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "new"


def test_an_entry_polling_the_address_under_another_unique_id_aborts():
    entry = _entry("e1", "smartha-somebox", APP, 9099, agent_hostname="somebox")
    _, result = _discover([entry], {f"{APP}:9099": (APP, ["SER-H1"], "")})
    assert result["reason"] == "already_configured"


def test_added_while_the_card_waited_aborts():
    flow, result = _discover([], {f"{APP}:9099": APP_AGENT})
    assert result["type"] == "form"
    flow.hass.config_entries.entries.append(_app_entry(LAN, 9099, agent_id=APP_ID))
    assert _run(flow.async_step_hassio_confirm({}))["reason"] == "already_configured"


# --- a second App on the same box (a local dev build) ------------------------------


def test_a_dev_build_of_the_app_is_offered_beside_the_real_apps_entry():
    """VM999: the real App 0.3.1's agent is an entry; the dev build posts the
    discovery for its own agent, another agent_id on another port."""
    real = _app_entry(APP, 9099, agent_hostname=APP, agent_id=APP_ID, agent_mock=False)
    info = _info(DEV, 9199, name="SMART Sniffer App (dev)", slug="local_smart_sniffer_agent_dev")
    flow, result = _discover([real], {f"{APP}:9099": APP_AGENT, f"{DEV}:9199": DEV_AGENT}, info)
    assert result["type"] == "form" and result["step_id"] == "hassio_confirm"
    # It is this box's App agent too: offered and named HAOS (owner, round 4).
    assert flow.context["title_placeholders"] == {"hostname": "HAOS"}
    created = _run(flow.async_step_hassio_confirm({}))
    assert created["title"] == "SMART Sniffer (HAOS)"
    assert created["data"]["app_slug"] == "local_smart_sniffer_agent_dev"
    assert flow.unique_id == f"{DEV}:9199"


def test_a_dev_build_without_an_agent_id_is_not_taken_for_the_real_app():
    real = _app_entry(APP, 9099, agent_hostname=APP, agent_mock=False)
    info = _info(DEV, 9199, slug="local_smart_sniffer_agent_dev")
    _, result = _discover([real], {f"{DEV}:9199": (DEV, ["SER-H1"], "")}, info)
    assert result["type"] == "form"


def test_a_dev_builds_entry_aborts_its_own_rediscovery():
    dev = _entry("e-dev", f"{DEV}:9199", DEV, 9199, agent_hostname=DEV, agent_id=DEV_ID)
    info = _info(DEV, 9199, slug="local_smart_sniffer_agent_dev")
    _, result = _discover([dev], {f"{DEV}:9199": DEV_AGENT}, info)
    assert result["reason"] == "already_configured"


# --- not offered ------------------------------------------------------------------


def test_an_agent_that_does_not_answer_aborts():
    _, result = _discover([], {})
    assert result["type"] == "abort" and result["reason"] == "cannot_connect"


def test_a_discovery_without_a_host_aborts():
    _, result = _discover([], {f"{APP}:9099": APP_AGENT}, _info(host=""))
    assert result["reason"] == "cannot_connect"


def test_the_apps_replica_agent_is_refused():
    agents = {f"{APP}:9100": (APP, ["MOCK-R1"], "", {"mock": True})}
    _, result = _discover([], agents, _info(port=9100))
    assert result["reason"] == "replicas_moved"


def test_strings_have_the_confirm_step():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"
    strings = json.loads((root / "strings.json").read_text())
    step = strings["config"]["step"]["hassio_confirm"]
    for slot in ("{label}", "{app}", "{host}", "{port}"):
        assert slot in step["description"]
    assert strings["config"]["flow_title"].format(hostname="HAOS") == "SMART Sniffer (HAOS)"
