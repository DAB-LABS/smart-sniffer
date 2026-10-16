"""Reading the agent's version string for the outdated-agent checks.

Release builds report "0.8.2"; self-built agents reported git describe
output such as "v0.8.2-31-g4439849-dirty", and test builds report
"0.8.3-pretest5". All of them must compare by their leading numbers.
"""

from __future__ import annotations

import pytest

from custom_components.smart_sniffer.agent_version import (
    agent_is_outdated,
    display_version,
    parse_version,
)


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("0.8.2", (0, 8, 2)),
        ("v0.8.2", (0, 8, 2)),
        ("V0.8.2", (0, 8, 2)),
        ("vv0.8.2", (0, 8, 2)),
        (" v0.8.2 ", (0, 8, 2)),
        ("0.8.3-pretest5", (0, 8, 3)),
        ("v0.8.2-31-g4439849-dirty", (0, 8, 2)),
        ("v0.8.2-31-g4439849", (0, 8, 2)),
        ("0.8.2+meta", (0, 8, 2)),
        ("0.4", (0, 4)),
        ("1", (1,)),
        ("dev", None),
        ("vdev", None),
        ("4439849", (4439849,)),
        ("", None),
        ("v", None),
        (None, None),
        (82, None),
    ],
)
def test_parse_version(raw, parsed):
    assert parse_version(raw) == parsed


@pytest.mark.parametrize(
    ("agent", "minimum", "outdated"),
    [
        ("0.8.2", "0.4.28", False),
        ("v0.8.2", "0.4.28", False),
        ("vv0.8.2", "0.4.28", False),
        ("0.4.27", "0.4.28", True),
        ("v0.4.27", "0.4.28", True),
        ("vv0.4.27", "0.4.28", True),
        ("0.4.27-pretest5", "0.4.28", True),
        ("v0.4.27-31-g4439849-dirty", "0.4.28", True),
        ("0.4.28-pretest1", "0.4.28", False),
        ("0.8.3-pretest5", "0.8.3", False),
        ("v0.8.2-31-g4439849-dirty", "0.8.3", True),
        ("0.4", "0.4.28", True),
        ("0.4", "0.4.0", False),
        ("0.5", "0.4.28", False),
        ("0.4.28", "0.4.28", False),
        ("0.4.28.1", "0.4.28", False),
        ("dev", "0.4.28", False),
        ("", "0.4.28", False),
        (None, "0.4.28", False),
        ("0.1.0", "dev", False),
    ],
)
def test_agent_is_outdated(agent, minimum, outdated):
    assert agent_is_outdated(agent, minimum) is outdated


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("0.8.2", "0.8.2"),
        ("v0.8.2", "0.8.2"),
        ("vv0.8.2", "0.8.2"),
        (" V0.8.3-pretest5 ", "0.8.3-pretest5"),
        ("dev", "dev"),
        ("", ""),
        (None, ""),
    ],
)
def test_display_version(raw, shown):
    assert display_version(raw) == shown


def test_one_parser():
    """config_flow and coordinator use this module, not copies of their own."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "custom_components" / "smart_sniffer"
    for name in ("config_flow.py", "coordinator.py"):
        src = (pkg / name).read_text()
        assert "from .agent_version import agent_is_outdated" in src
        assert "def _agent_is_outdated" not in src
        assert "def _version_tuple" not in src
