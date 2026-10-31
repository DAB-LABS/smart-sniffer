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

from types import SimpleNamespace  # noqa: E402

from homeassistant.config_entries import SOURCE_HASSIO, ConfigEntryState  # noqa: E402

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


def _own_entry(host=APP, port=9099, state=ConfigEntryState.LOADED, **data):
    """The entry the App's discovery made: source hassio, with the App slug."""
    entry = _entry("e-app", f"{host}:{port}", host, port, title="SMART Sniffer (HAOS)",
                   source=SOURCE_HASSIO, app_slug="0449a086_smart_sniffer_agent", **data)
    entry.state = state
    entry.disabled_by = None
    return entry


class _Flows:
    """The flow manager's reauth flows and reloads, as _update_token uses them."""

    def __init__(self, flow, reauth_of=()):
        self.aborted: list[str] = []
        self.reloads: list[str] = []
        self.progress = [{"flow_id": f"reauth-{e}", "context": {"source": "reauth", "entry_id": e}}
                         for e in reauth_of]
        flow.hass.config_entries.flow = SimpleNamespace(
            async_progress_by_handler=self.by_handler, async_abort=self.aborted.append)
        flow.hass.config_entries.async_schedule_reload = self.reloads.append

    def by_handler(self, handler, include_uninitialized=False, match_context=None):
        match_context = match_context or {}
        return [f for f in self.progress
                if all(f["context"].get(k) == v for k, v in match_context.items())]


def _rediscover(entries, agents, info, reauth_of=()):
    """The App posts its discovery again (it does after a password Save)."""
    flow = _flow(entries, SOURCE_HASSIO, agents)
    net = flow.net

    async def health(host, port, token):
        agent = net.agents.get(f"{host}:{port}")
        return await net.test_connection(host, port, agent[2] if agent else token)

    flow._test_connection = health
    flows = _Flows(flow, reauth_of)
    return flow, flows, _run(flow.async_step_hassio(info))


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
    entry = _own_entry(agent_id=APP_ID, token="old")
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
    entry = _own_entry(token="old")
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


# --- the App's password (v1.0, plan A4) ---------------------------------------------
#
# What an older integration (0.9.x) does with the same discovery, for the
# record (read from its code at v0.9.2): a new App entry is made with the
# token and no prompt, as here. A token that changes is written into ANY
# entry the discovery matched (by agent_id, as the App's entry, or by
# address), including one added by hand or by zeroconf for the same agent;
# an empty token is ignored. A loaded entry reloads from its update listener
# and recovers. One whose setup was refused stays down until Home Assistant
# restarts or the user enters the password, and its reauth notice stays.


def test_a_new_app_entry_is_made_with_the_apps_password_and_no_prompt():
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "spy-cipher-otter-0042", {"agent_id": APP_ID})}
    flow, result = _discover([], agents, _info(token="spy-cipher-otter-0042", agent_id=APP_ID))
    assert result["type"] == "form" and result["data_schema"].schema == {}
    created = _run(flow.async_step_hassio_confirm({}))
    assert created["type"] == "create_entry"
    assert created["data"]["token"] == "spy-cipher-otter-0042"
    assert created["data"]["app_slug"] == "0449a086_smart_sniffer_agent"


@pytest.mark.parametrize("state", [ConfigEntryState.LOADED, ConfigEntryState.SETUP_ERROR])
def test_a_changed_password_updates_the_apps_entry_and_it_recovers(state):
    entry = _own_entry(agent_id=APP_ID, token="old-pass", state=state)
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "new-pass", {"agent_id": APP_ID})}
    flow, flows, result = _rediscover([entry], agents, _info(token="new-pass", agent_id=APP_ID),
                                      reauth_of=["e-app", "e-other"])
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "new-pass"
    # The reauth the old password started ends; another entry's stays.
    assert flows.aborted == ["reauth-e-app"]
    # Loaded: its update listener reloads it. Refused at setup: reloaded here.
    assert flows.reloads == ([] if state is ConfigEntryState.LOADED else ["e-app"])


def test_a_cleared_password_clears_the_apps_entry():
    entry = _own_entry(agent_id=APP_ID, token="old-pass")
    flow, flows, result = _rediscover([entry], {f"{APP}:9099": APP_AGENT},
                                      _info(token="", agent_id=APP_ID))
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == ""


def test_an_app_that_sends_no_password_changes_nothing():
    """App 0.4.x posts host and port only."""
    entry = _own_entry(agent_id=APP_ID, token="typed-by-hand")
    flow, flows, result = _rediscover([entry], {f"{APP}:9099": APP_AGENT}, _info(agent_id=APP_ID))
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "typed-by-hand"
    assert flow.hass.config_entries.updates == [] and flows.reloads == []


def test_the_same_password_again_changes_nothing():
    entry = _own_entry(agent_id=APP_ID, token="same-pass")
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "same-pass", {"agent_id": APP_ID})}
    flow, flows, _ = _rediscover([entry], agents, _info(token="same-pass", agent_id=APP_ID),
                                 reauth_of=["e-app"])
    assert flow.hass.config_entries.updates == [] and flows.aborted == [] and flows.reloads == []


@pytest.mark.parametrize("source", ["zeroconf", "user", "import"])
def test_never_applied_to_an_entry_found_by_zeroconf_or_added_by_hand(source):
    """The same agent at the box's LAN address (zeroconf, the user step, the
    panel's Add), matched by agent_id: its password stays its own."""
    entry = _entry("e-lan", f"{LAN}:9099", LAN, 9099, source=source, agent_id=APP_ID,
                   agent_hostname=APP, token="lan-pass")
    agents = {f"{APP}:9099": (APP, ["SER-H1"], "", {"agent_id": APP_ID})}
    flow, flows, result = _rediscover([entry], agents, _info(token="app-pass", agent_id=APP_ID))
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "lan-pass"
    assert flow.hass.config_entries.updates == [] and flows.reloads == []


@pytest.mark.parametrize("source", ["zeroconf", "user"])
def test_never_applied_by_address_to_an_entry_added_by_hand(source):
    """Added by hand at the App's own container address: still not the
    App's discovery entry, so it keeps its own password (and asks for a new
    one through reauth, like any other entry)."""
    entry = _entry("e-hand", f"{APP}:9099", APP, 9099, source=source, token="typed")
    flow, flows, result = _rediscover([entry], {f"{APP}:9099": (APP, ["SER-H1"], "")},
                                      _info(token="app-pass"))
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "typed"


def test_the_apps_old_replica_never_takes_the_password():
    replica = _entry("e-rep", f"{APP}:9100", APP, 9100, source=SOURCE_HASSIO, agent_mock=True,
                     app_slug="0449a086_smart_sniffer_agent", token="")
    flow, flows, result = _rediscover([replica], {f"{APP}:9100": (APP, ["MOCK-1"], "")},
                                      _info(port=9100, token="app-pass"))
    assert replica.data["token"] == ""


def test_is_app_own_entry():
    from tests.test_config_flow_under_ha import config_flow

    own = _own_entry()
    assert config_flow.is_app_own_entry(own)
    slug_only = _entry("e", "x", APP, source="user", app_slug="0449a086_smart_sniffer_agent")
    assert config_flow.is_app_own_entry(slug_only)
    assert not config_flow.is_app_own_entry(_entry("e", "x", APP, source="user"))
    assert not config_flow.is_app_own_entry(_entry("e", "x", LAN, source="zeroconf",
                                                   agent_hostname=APP))
    assert not config_flow.is_app_own_entry(
        _entry("r", "replica", "replica.invalid", source=SOURCE_HASSIO, kind="replica"))
