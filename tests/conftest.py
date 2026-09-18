"""Shared fixtures for the SMART Sniffer integration tests.

Why attention.py is loaded by path
----------------------------------
``attention.py`` imports nothing from Home Assistant, which is what makes these
tests runnable under plain pytest with no harness. Importing it the obvious way
(``from custom_components.smart_sniffer.attention import ...``) does not work,
because Python executes the package's ``__init__.py`` first and
``custom_components/smart_sniffer/__init__.py`` *does* import Home Assistant.

So the module is loaded directly from its file, which runs no package
``__init__``. This mirrors how ``evaluate_attention`` was exercised by hand
during the #27 investigation, where the shipped and fixed modules were loaded
side by side to prove a behaviour change.

The payload wrapper
-------------------
``evaluate_attention`` takes the coordinator's per-drive payload, which nests
the smartctl JSON under a ``smart_data`` key. Passing the bare smartctl JSON
instead does not raise: it returns ``UNSUPPORTED``, which is easy to misread as
a passing test. Every payload here is built by ``drive()`` so the wrapper is
applied in exactly one place.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_ATTENTION_PY = _REPO_ROOT / "custom_components" / "smart_sniffer" / "attention.py"
_FIXTURE_DIR = _TESTS_DIR / "fixtures"


def _load_attention() -> ModuleType:
    """Load attention.py from its path, bypassing the package __init__."""
    spec = importlib.util.spec_from_file_location(
        "smart_sniffer_attention", _ATTENTION_PY
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load attention.py from {_ATTENTION_PY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attention = _load_attention()

# Every fixture file, so the shape canary can walk them without a hardcoded list.
FIXTURE_NAMES = sorted(p.stem for p in _FIXTURE_DIR.glob("*.json"))


@pytest.fixture(scope="session")
def att() -> ModuleType:
    """The attention module under test."""
    return attention


@pytest.fixture(scope="session")
def fixture_names() -> list[str]:
    """Every fixture file stem, so callers need no hardcoded list."""
    return FIXTURE_NAMES


def _load_smart_data(name: str) -> dict[str, Any]:
    path = _FIXTURE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no such fixture: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def drive():
    """Build a coordinator drive payload from a fixture name.

    Returns a fresh deep copy each call, so a test that mutates its payload
    cannot leak into another test.
    """

    def _drive(fixture_name: str) -> dict[str, Any]:
        return {"smart_data": copy.deepcopy(_load_smart_data(fixture_name))}

    return _drive


@pytest.fixture
def set_ata_raw():
    """Set the raw value of a named ATA attribute on a drive payload.

    Raises if the attribute is absent. A silent no-op here would produce a test
    that passes while asserting nothing, which is the failure mode these
    fixtures exist to avoid.
    """

    def _set(payload: dict[str, Any], attr_name: str, raw_value: int) -> None:
        table = (
            payload["smart_data"]
            .get("ata_smart_attributes", {})
            .get("table", [])
        )
        for attr in table:
            if attr.get("name") == attr_name:
                attr["raw"] = {"value": raw_value, "string": str(raw_value)}
                return
        raise KeyError(
            f"{attr_name!r} is not in this fixture's attribute table; "
            f"present names: {[a.get('name') for a in table]}"
        )

    return _set


@pytest.fixture
def add_ata_attr():
    """Append an ATA attribute row to a drive payload."""

    def _add(
        payload: dict[str, Any], attr_id: int, attr_name: str, raw_value: int
    ) -> None:
        table = payload["smart_data"].setdefault(
            "ata_smart_attributes", {"revision": 16, "table": []}
        )["table"]
        table.append(
            {
                "id": attr_id,
                "name": attr_name,
                "value": 100,
                "worst": 100,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": raw_value, "string": str(raw_value)},
            }
        )

    return _add


@pytest.fixture
def set_nvme():
    """Set one or more keys in the NVMe health log of a drive payload."""

    def _set(payload: dict[str, Any], **values: int) -> None:
        log = payload["smart_data"].get("nvme_smart_health_information_log")
        if log is None:
            raise KeyError("this fixture has no nvme_smart_health_information_log")
        log.update(values)

    return _set
