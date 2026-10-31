"""Agent update notices (v1.0, #68 phase 1).

- agent_update.py: which agent is behind LATEST_AGENT_VERSION, the install
  command for its OS, and the summary the update entity shows. No Home
  Assistant needed for these.
- update.py and the repair notices are in test_agent_update_under_ha.py.

Host names and versions are invented.
"""

from __future__ import annotations

import pytest

from custom_components.smart_sniffer import const
from custom_components.smart_sniffer.agent_update import (
    CURL_COMMAND,
    IRM_COMMAND,
    SUMMARY_LIMIT,
    agent_behind,
    install_hint,
    release_summary,
)

# --- the version comparisons ----------------------------------------------------------


def test_latest_is_a_plain_release_number_at_or_above_the_minimum():
    from custom_components.smart_sniffer.agent_version import agent_is_outdated, parse_version

    assert parse_version(const.LATEST_AGENT_VERSION) is not None
    assert not agent_is_outdated(const.LATEST_AGENT_VERSION, const.MIN_AGENT_VERSION)


@pytest.mark.parametrize(("installed", "behind"), [
    ("0.9.2", True),
    ("v0.9.2", True),
    ("0.4.28", True),
    ("0.9.3-pretest1", True),
    ("1.0.0", False),
    ("v1.0.0", False),
    ("1.0", False),  # missing parts are zero
    ("1.0.0-pretest2", False),  # a test build of this release
    ("1.0.1", False),  # newer than the latest
    ("1.1.0-31-gabc1234-dirty", False),
    ("dev", False),  # cannot be read: never nagged
    ("", False),
    (None, False),
])
def test_agent_behind(installed, behind):
    assert agent_behind(installed, "1.0.0") is behind


def test_agent_behind_the_shipped_latest():
    assert agent_behind("0.9.2")
    assert not agent_behind(const.LATEST_AGENT_VERSION)


# --- the install command for the machine's OS -----------------------------------------


@pytest.mark.parametrize(("os_name", "container", "kind", "command"), [
    ("linux", False, "unix", CURL_COMMAND),
    ("Linux", None, "unix", CURL_COMMAND),
    ("darwin", False, "unix", CURL_COMMAND),
    ("windows", False, "windows", IRM_COMMAND),
    ("linux", True, "docker", None),
    ("freebsd", False, "unknown", None),
    ("", False, "unknown", None),
    (None, False, "unknown", None),
])
def test_install_hint(os_name, container, kind, command):
    hint = install_hint(os_name, container)
    assert hint["kind"] == kind and hint["command"] == command
    assert hint["docs"].startswith("https://github.com/DAB-LABS/smart-sniffer")


def test_the_commands_are_the_readme_one_liners():
    assert CURL_COMMAND == ("curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer"
                            "/main/install.sh | sudo bash")
    assert IRM_COMMAND == ("irm https://raw.githubusercontent.com/DAB-LABS/smart-sniffer"
                           "/main/install.ps1 | iex")
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert CURL_COMMAND in readme and IRM_COMMAND in readme


@pytest.mark.parametrize(("os_name", "container", "needle"), [
    ("linux", False, CURL_COMMAND),
    ("darwin", False, CURL_COMMAND),
    ("windows", False, IRM_COMMAND),
    ("linux", True, const.AGENT_DOCKER_DOCS_URL),
    ("plan9", False, const.AGENT_INSTALL_DOCS_URL),
])
def test_the_summary_says_what_is_new_and_how(os_name, container, needle):
    text = release_summary(os_name, container)
    assert text.startswith(const.LATEST_AGENT_NOTES)
    assert needle in text
    assert len(text) <= SUMMARY_LIMIT


def test_long_notes_are_shortened_never_the_command():
    text = release_summary("windows", False, notes="x" * 400)
    assert len(text) <= SUMMARY_LIMIT and text.endswith(IRM_COMMAND) and "..." in text


def test_public_text_rules():
    for text in (const.LATEST_AGENT_NOTES, release_summary("linux"), release_summary("windows"),
                 release_summary("linux", True), release_summary(None)):
        assert "—" not in text and "–" not in text
