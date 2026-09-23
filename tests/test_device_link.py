"""The link that nests a drive device under its agent (#53).

Home Assistant 2026.8 added via_device_id and 2026.9 deprecates via_device,
removing it in 2027.8.0. The declared floor, 2024.4.0, has via_device only and
treats an unknown device info key as invalid, so the integration has to send
whichever one the installed core understands.

These tests cover the choice itself. They cannot cover the registry doing the
right thing with the result, which is what the bench is for.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import TypedDict

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"


def _load(filename: str):
    """Import one module of the integration by path, under a stand-in package so
    its relative imports resolve. The real package __init__ imports Home
    Assistant, which this suite does not have; device_link.py and const.py do
    not."""
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


device_link = _load("device_link.py")


class DeviceInfoOld(TypedDict, total=False):
    """DeviceInfo as it is at the declared floor, 2024.4.0."""

    identifiers: set
    name: str
    via_device: tuple


class DeviceInfoNew(TypedDict, total=False):
    """DeviceInfo from 2026.8.0, which carries both."""

    identifiers: set
    name: str
    via_device: tuple
    via_device_id: str


# --- Which key the installed core understands --------------------------------


def test_new_device_info_supports_via_device_id():
    assert device_link.supports_via_device_id(DeviceInfoNew) is True


def test_floor_device_info_does_not():
    assert device_link.supports_via_device_id(DeviceInfoOld) is False


def test_an_unreadable_device_info_is_treated_as_old():
    """Detection must never raise: a shape this code cannot read falls back to
    the form every supported release accepts."""

    class Awkward:
        @property
        def __annotations__(self):
            raise RuntimeError("annotations are not readable here")

    assert device_link.supports_via_device_id(Awkward) is False
    assert device_link.supports_via_device_id(object()) is False


# --- The link itself ---------------------------------------------------------


def test_link_uses_the_registry_id_when_supported():
    assert device_link.link_for(True, "abc123", "dev-42") == {"via_device_id": "dev-42"}


def test_link_falls_back_to_the_identifier_tuple():
    assert device_link.link_for(False, "abc123", "dev-42") == {
        "via_device": (device_link.DOMAIN, "abc123_agent")
    }


def test_a_missing_agent_device_id_falls_back():
    """Core raises on a via_device_id it cannot resolve, so no id means the old
    form, which core resolves by lookup."""
    for agent_device_id in (None, ""):
        assert device_link.link_for(True, "abc123", agent_device_id) == {
            "via_device": (device_link.DOMAIN, "abc123_agent")
        }


def test_the_link_is_never_both_at_once():
    """Core rejects device info carrying via_device and via_device_id together."""
    for supported in (True, False):
        for agent_device_id in (None, "dev-42"):
            link = device_link.link_for(supported, "abc123", agent_device_id)
            assert len(link) == 1
            assert {"via_device", "via_device_id"} & set(link) != set()


# --- Every site goes through the helper --------------------------------------

_VIA_DEVICE_LITERAL = re.compile(r"""["']via_device["']|via_device\s*=""")


def test_no_module_writes_a_via_device_link_of_its_own():
    """Six sites used to carry their own copy of the link. A seventh added
    later must go through the helper, or it will keep the deprecated form after
    the rest have moved on."""
    offenders = sorted(
        path.name
        for path in _COMPONENT.rglob("*.py")
        if path.name != "device_link.py" and _VIA_DEVICE_LITERAL.search(path.read_text())
    )
    assert offenders == []


def test_the_platforms_use_the_helper():
    """The counterpart to the test above: the link is still applied, six times,
    rather than having been dropped."""
    calls = sum(
        len(re.findall(r"\*\*agent_link\(", (_COMPONENT / name).read_text()))
        for name in ("sensor.py", "binary_sensor.py")
    )
    assert calls == 6
