"""Agent update notices under Home Assistant (v1.0, #68 phase 1): an update
entity per agent entry (not the App's agent, not the Replica or the App's
old replica), its versions and state, and the two repair notices. Host
names and versions are invented. Skipped without Home Assistant.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.smart_sniffer import const  # noqa: E402
from custom_components.smart_sniffer.agent_update import IRM_COMMAND  # noqa: E402

update = importlib.import_module("custom_components.smart_sniffer.update")
coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")

APP = "a0d7b954-smart-sniffer-agent"


def _entry(source="user", title="SMART Sniffer (nas-01)", **data):
    return SimpleNamespace(entry_id="E1", source=source, title=title,
                           data={"host": "10.0.10.20", "port": 9099, **data})


@pytest.mark.parametrize(("entry", "hostname", "wanted"), [
    (_entry(agent_hostname="nas-01"), "nas-01", True),
    (_entry(source="zeroconf", agent_hostname="nas-01"), None, True),
    # The App's agent, however it was added: the Supervisor updates it.
    (_entry(source="hassio", title="SMART Sniffer (HAOS)", app_slug="x_smart_sniffer_agent"),
     APP, False),
    (_entry(host=APP, title=f"SMART Sniffer ({APP}:9099)"), None, False),
    (_entry(source="zeroconf", agent_hostname=APP), APP, False),
    (_entry(agent_hostname="nas-01"), APP, False),  # its live host name says so
    # The Replica, and the App's old replica agent.
    (_entry(kind="replica", host="replica.invalid"), None, False),
    (_entry(host=APP, agent_mock=True, agent_hostname=APP), APP, False),
])
def test_which_entries_get_an_update_entity(entry, hostname, wanted):
    assert update.has_update_entity(entry, hostname) is wanted


def _entity(health):
    coordinator = SimpleNamespace(data=health, last_update_success=True,
                                  async_add_listener=lambda *a, **k: (lambda: None))
    entity = object.__new__(update.AgentUpdateEntity)
    entity.coordinator = coordinator
    return entity


def test_an_agent_behind_the_latest_is_offered_it_with_its_command():
    entity = _entity({"version": "0.9.2", "os": "windows", "container": False})
    assert entity.installed_version == "0.9.2"
    assert entity.latest_version == const.LATEST_AGENT_VERSION
    assert IRM_COMMAND in entity.release_summary
    assert entity.version_is_newer(const.LATEST_AGENT_VERSION, "0.9.2")
    assert entity.supported_features == 0  # no install, ever
    assert entity.release_url == const.AGENT_RELEASES_URL


@pytest.mark.parametrize("version", [const.LATEST_AGENT_VERSION, "1.0.0-pretest3", "9.9.9", "dev"])
def test_a_current_newer_or_dev_agent_has_no_update(version):
    entity = _entity({"version": version, "os": "linux"})
    assert entity.latest_version == entity.installed_version
    assert entity.release_summary is None


def test_no_version_read_yet_is_unknown_not_an_update():
    entity = _entity({"version": "", "os": "linux"})
    assert entity.installed_version is None and entity.latest_version is None


def test_the_state_under_this_home_assistant():
    """What Home Assistant's own UpdateEntity makes of the two versions."""
    behind = _entity({"version": "0.9.2", "os": "linux"})
    current = _entity({"version": "1.0.0-pretest3", "os": "linux"})
    assert behind.state == "on"
    assert current.state == "off"


# --- the repair notices -------------------------------------------------------------------


def _coord(monkeypatch, entry=None, replica=None):
    issues: dict[str, dict] = {}
    monkeypatch.setattr(coordinator_mod, "async_create_issue",
                        lambda hass, **kw: issues.__setitem__(kw["issue_id"], kw))
    monkeypatch.setattr(coordinator_mod, "async_delete_issue",
                        lambda hass, domain, issue_id: issues.pop(issue_id, None))
    coord = object.__new__(coordinator_mod.SmartSnifferCoordinator)
    coord.hass = None
    coord.host, coord._hostname = "10.0.10.20", "nas-01"
    coord.config_entry = entry or _entry(agent_hostname="nas-01")
    coord._replica = replica
    return coord, issues


def test_below_the_minimum_is_an_error_and_behind_latest_a_warning(monkeypatch):
    from homeassistant.helpers.issue_registry import IssueSeverity

    coord, issues = _coord(monkeypatch)
    coord._check_agent_version("0.4.20", {"hostname": "nas-01"})
    assert set(issues) == {"agent_outdated_10.0.10.20"}
    assert issues["agent_outdated_10.0.10.20"]["severity"] is IssueSeverity.ERROR

    coord._check_agent_version("0.9.2", {"hostname": "nas-01"})
    assert set(issues) == {"agent_update_available_10.0.10.20"}
    notice = issues["agent_update_available_10.0.10.20"]
    assert notice["severity"] is IssueSeverity.WARNING
    assert notice["translation_key"] == "agent_update_available"
    assert notice["translation_placeholders"]["latest_version"] == const.LATEST_AGENT_VERSION
    assert notice["translation_placeholders"]["current_version"] == "0.9.2"

    coord._check_agent_version(const.LATEST_AGENT_VERSION, {"hostname": "nas-01"})
    assert issues == {}


def test_no_version_read_is_still_the_error(monkeypatch):
    coord, issues = _coord(monkeypatch)
    coord._check_agent_version("", None)
    assert set(issues) == {"agent_outdated_10.0.10.20"}


def test_the_apps_agent_never_gets_the_update_notice(monkeypatch):
    coord, issues = _coord(monkeypatch, _entry(source="hassio", app_slug="x_smart_sniffer_agent"))
    coord._check_agent_version("0.9.2", {"hostname": APP})
    assert issues == {}


def test_the_replica_gets_neither(monkeypatch):
    coord, issues = _coord(monkeypatch, replica=object())
    coord._check_agent_version("0.8.0-mock", None)
    assert issues == {}


def test_the_update_notice_strings():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"
    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((root / name).read_text())
        issue = strings["issues"]["agent_update_available"]
        for key in ("{hostname}", "{current_version}", "{latest_version}"):
            assert key in issue["title"] + issue["description"]
        assert "Agent Update" in issue["description"]
        assert strings["entity"]["update"]["agent"]["name"] == "Agent Update"
