"""Which devices a user may delete, and which the integration refuses.

Home Assistant puts the Delete button on every device of the entry once the
integration defines async_remove_config_entry_device, so these rules are the
only thing standing between a user and deleting a drive that is still attached.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"


def _load(filename: str):
    """Import one module of the integration by path, under a stand-in package so
    its relative imports resolve, since the real package __init__ imports Home
    Assistant."""
    package = "smart_sniffer_standalone"
    if package not in sys.modules:
        parent = ModuleType(package)
        parent.__path__ = [str(_COMPONENT)]
        sys.modules[package] = parent
    name = f"{package}.{Path(filename).stem}"
    spec = importlib.util.spec_from_file_location(name, _COMPONENT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


removal = _load("device_removal.py")

ENTRY = "01KRS6Q8KDEN7SQJFHWCZAQ6EM"
DOMAIN = removal.DOMAIN

# What the agent reports on a healthy poll: one drive and one filesystem.
LIVE = {
    "s6pxns0l100992m": {"model": "Samsung SSD 870 EVO 500GB"},
    "_filesystems": [{"mount": "/", "used_percent": 41}],
}


def _device(identifier: str) -> set[tuple[str, str]]:
    return {(DOMAIN, identifier)}


def _decide(identifiers, data=LIVE, agent_reporting=True):
    return removal.removable(
        identifiers,
        ENTRY,
        removal.reported_drive_ids(data),
        removal.reported_filesystem_count(data),
        agent_reporting,
    )


# --- Rule 1: the agent device ------------------------------------------------


def test_the_agent_device_is_never_removable():
    allowed, reason = _decide(_device(f"{ENTRY}_agent"))
    assert allowed is False
    assert "config entry" in reason


def test_the_agent_device_is_not_removable_even_when_nothing_is_reported():
    allowed, _ = _decide(_device(f"{ENTRY}_agent"), data=None, agent_reporting=False)
    assert allowed is False


# --- Rule 2: an agent that is not reporting ----------------------------------


def test_nothing_is_removable_before_the_first_poll():
    """data is None until a poll succeeds. A drive absent from nothing is not
    absent."""
    for identifier in ("dev-sda", "s6pxns0l100992m", f"{ENTRY}_filesystems"):
        allowed, reason = _decide(_device(identifier), data=None, agent_reporting=False)
        assert allowed is False, identifier
        assert "not reporting" in reason


def test_nothing_is_removable_when_the_last_poll_failed():
    """The coordinator keeps the previous payload when an update fails, so the
    data alone would happily call a drive stale. An unreachable agent must not
    become a licence to delete."""
    stale_payload = {"_filesystems": []}
    allowed, reason = _decide(
        _device("s6pxns0l100992m"), data=stale_payload, agent_reporting=False
    )
    assert allowed is False
    assert "not reporting" in reason


# --- Rule 3: drives ----------------------------------------------------------


def test_a_reported_drive_refuses():
    allowed, reason = _decide(_device("s6pxns0l100992m"))
    assert allowed is False
    assert "still reports" in reason


def test_a_drive_the_agent_no_longer_reports_is_removable():
    allowed, reason = _decide(_device("nvme-gone-9000"))
    assert allowed is True
    assert "no longer reports" in reason


def test_an_old_identifier_scheme_is_removable_without_being_named():
    """The live orphan on the bench box: dev-sda, from a scheme the agent has
    never reported. No rule mentions it; it is simply not in the payload."""
    allowed, _ = _decide(_device("dev-sda"))
    assert allowed is True


def test_an_agent_reporting_no_drives_at_all_frees_every_drive():
    allowed, _ = _decide(_device("s6pxns0l100992m"), data={"_filesystems": []})
    assert allowed is True


# --- Rule 4: the filesystem device -------------------------------------------


def test_the_filesystem_device_refuses_while_filesystems_are_reported():
    allowed, reason = _decide(_device(f"{ENTRY}_filesystems"))
    assert allowed is False
    assert "filesystem" in reason


def test_the_filesystem_device_is_removable_when_none_are_reported():
    allowed, reason = _decide(
        _device(f"{ENTRY}_filesystems"),
        data={"s6pxns0l100992m": {}, "_filesystems": []},
    )
    assert allowed is True
    assert "no filesystems" in reason


def test_a_missing_filesystems_key_counts_as_none():
    allowed, _ = _decide(_device(f"{ENTRY}_filesystems"), data={"s6pxns0l100992m": {}})
    assert allowed is True


# --- Devices that are not ours -----------------------------------------------


def test_a_device_from_another_integration_refuses():
    allowed, reason = _decide({("other_domain", "whatever")})
    assert allowed is False
    assert "does not belong" in reason


def test_a_device_with_no_identifiers_refuses():
    allowed, _ = _decide(set())
    assert allowed is False


def test_our_identifier_is_picked_out_of_a_mixed_set():
    allowed, _ = _decide({("other_domain", "x"), (DOMAIN, "s6pxns0l100992m")})
    assert allowed is False


# --- The payload helpers -----------------------------------------------------


def test_internal_keys_are_not_drives():
    assert removal.reported_drive_ids(LIVE) == ["s6pxns0l100992m"]
    assert removal.reported_drive_ids(None) is None
    assert removal.reported_filesystem_count(LIVE) == 1
    assert removal.reported_filesystem_count(None) is None


def test_no_decision_is_returned_without_a_reason():
    """The reason is the only thing the log can show, since core replaces it
    with a fixed error for the user."""
    for identifiers, data, reporting in (
        (_device(f"{ENTRY}_agent"), LIVE, True),
        (_device("dev-sda"), LIVE, True),
        (_device("s6pxns0l100992m"), None, False),
        ({("other", "x")}, LIVE, True),
    ):
        _, reason = _decide(identifiers, data=data, agent_reporting=reporting)
        assert reason and reason[0].islower() and not reason.endswith(".")


# --- The integration wires it up ---------------------------------------------


def test_the_removal_hook_exists_and_delegates():
    """Without this function name in __init__.py, Home Assistant shows no
    Delete button at all, which is the whole bug."""
    source = (_COMPONENT / "__init__.py").read_text()
    assert "async def async_remove_config_entry_device(" in source
    assert re.search(r"removable\(\s*\n\s+device_entry\.identifiers", source)
