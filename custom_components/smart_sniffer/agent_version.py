"""Read the agent's version string leniently and compare it to a minimum.

Release builds report a plain number such as "0.8.2". Self-built agents
reported what git describe gave them: "v0.8.2" (shown as "vv0.8.2" in the
agent's own log) or "v0.8.2-31-g4439849-dirty"; test builds report
"0.8.3-pretest5". Reading each dot-separated part with int() failed on all
of those, so Home Assistant never warned that such an agent was out of date.

Leading "v"s and spaces are dropped, the leading run of dot-separated
numbers is read, and anything after it (a "-pretest5", "-31-gabc", "-dirty"
or "+meta" suffix) is ignored. Missing parts count as zero, so "0.4" equals
"0.4.0". A string with no leading number ("dev", empty, None) cannot be
read, and such an agent is not reported as out of date.

No Home Assistant imports, so the tests load this module directly.
"""

from __future__ import annotations

import re
from typing import Any

_LEADING_NUMBER = re.compile(r"\s*[vV]*\s*(\d+(?:\.\d+)*)")


def parse_version(value: Any) -> tuple[int, ...] | None:
    """Return the leading numbers of value as a tuple, or None."""
    if not isinstance(value, str):
        return None
    m = _LEADING_NUMBER.match(value)
    if m is None:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def display_version(value: Any) -> str:
    """Return value with leading spaces and "v"s dropped, for messages that
    add their own "v"."""
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("vV").strip()


def agent_is_outdated(agent_version: Any, min_version: str) -> bool:
    """Return True if agent_version is older than min_version.

    False when either cannot be read, as for a "dev" build.
    """
    have = parse_version(agent_version)
    need = parse_version(min_version)
    if have is None or need is None:
        return False
    width = max(len(have), len(need))
    have += (0,) * (width - len(have))
    need += (0,) * (width - len(need))
    return have < need
