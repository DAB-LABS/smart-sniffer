"""Checks on the translation files behind the options flow.

These strings are rendered by the Home Assistant frontend, which the test suite
cannot run. The frontend fills {placeholders} in some render slots and not in
others, and which slots it fills depends on the release. A placeholder in a slot
that is not filled does not show its braces: formatjs throws, the heading renders
as "Translation [formatjs Error: MISSING_VALUE] ...", and an error is written to
the Home Assistant log on every render.

So the property is: for the minimum version this integration declares in
hacs.json, no string sits in a slot that version does not fill. Lower the floor
and these tests start failing on their own.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "smart_sniffer"
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")

# First Home Assistant release whose frontend passes description_placeholders
# into each slot. Read from src/dialogs/config-flow/show-dialog-options-flow.ts
# and show-dialog-config-flow.ts in home-assistant/frontend.
#
#   menu_title   renderMenuHeader. Never filled, 20241002.0 through current dev.
#   section_*    renderShowFormStepFieldLabel / FieldHelper, "expandable" branch.
#   field_label  renderShowFormStepFieldLabel, data.<field> and
#                sections.<id>.data.<field>.
#                All three were first filled in frontend 20250430.0, the 2025.5
#                beta. HA 2025.5.0 ships 20250507.0; 2025.4.x ships 20250401.0.
#   always       Step title and description, menu description and options,
#                data_description (nested or not), errors, aborts. Filled at
#                every release this integration has declared.
_NEVER = None
_ALWAYS = (0, 0, 0)
_FILLED_FROM = {
    "menu_title": _NEVER,
    "section_name": (2025, 5, 0),
    "section_description": (2025, 5, 0),
    "field_label": (2025, 5, 0),
    "always": _ALWAYS,
}


def _load(relative: str) -> dict:
    with (_COMPONENT / relative).open(encoding="utf-8") as handle:
        return json.load(handle)


def _floor() -> tuple[int, ...]:
    declared = json.loads((_COMPONENT.parents[1] / "hacs.json").read_text())
    return tuple(int(part) for part in declared["homeassistant"].split("."))


def _walk(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, (*path, key))
    elif isinstance(node, str):
        yield path, node


def _slot(step: dict, rest: tuple[str, ...]) -> str:
    if rest == ("title",) and "menu_options" in step:
        return "menu_title"
    if len(rest) == 3 and rest[0] == "sections" and rest[2] == "name":
        return "section_name"
    if len(rest) == 3 and rest[0] == "sections" and rest[2] == "description":
        return "section_description"
    if rest[0] == "data" or (rest[0] == "sections" and rest[2] == "data"):
        return "field_label"
    return "always"


def _unfilled(strings: dict, floor: tuple[int, ...]) -> list[str]:
    """Every string carrying a placeholder in a slot `floor` does not fill."""
    found = []
    for flow in ("config", "options"):
        for step_id, step in strings.get(flow, {}).get("step", {}).items():
            for rest, text in _walk(step):
                since = _FILLED_FROM[_slot(step, rest)]
                if _PLACEHOLDER.search(text) and (since is _NEVER or since > floor):
                    found.append(".".join((flow, "step", step_id, *rest)))
    return found


def test_strings_and_english_translation_are_identical():
    """Home Assistant reads translations/en.json at runtime, not strings.json.
    A change made to one and not the other ships silently."""
    assert _load("strings.json") == _load("translations/en.json")


def test_no_placeholder_in_a_slot_the_declared_floor_does_not_fill():
    assert _unfilled(_load("strings.json"), _floor()) == []


def test_the_guard_follows_the_floor():
    """The guard has to bite, not just pass. The clean section's count sits in
    its name, which is fine from 2025.5 and broken on anything older."""
    strings = _load("strings.json")
    clean_name = "options.step.threshold_form.sections.clean.name"
    assert clean_name not in _unfilled(strings, (2025, 5, 0))
    assert clean_name in _unfilled(strings, (2025, 4, 0))
    assert clean_name in _unfilled(strings, (2024, 10, 0))


def test_a_menu_title_placeholder_fails_at_any_floor():
    menu = {
        "options": {
            "step": {
                "m": {"title": "For {drive}", "menu_options": {"a": "A"}},
                "f": {"title": "For {drive}", "data": {}},
            }
        }
    }
    assert _unfilled(menu, (2099, 1, 0)) == ["options.step.m.title"]


def test_every_threshold_form_placeholder_is_supplied(att, drive):
    """Every {slot} in the form's strings must be filled at runtime, or it
    renders as a translation error. This catches a label renamed in
    attention.py whose translation slug was not renamed with it."""
    form = _load("strings.json")["options"]["step"]["threshold_form"]
    wanted = {slot for _, text in _walk(form) for slot in _PLACEHOLDER.findall(text)}
    supplied = set(
        att.reading_placeholders(att.current_readings(drive("ata_healthy")), "d", 0)
    )
    assert wanted - supplied == set()


def test_declared_floor_supports_sectioned_options_forms():
    """section() options forms first render in frontend 20240904.0 (HA 2024.9).
    This holds even if the count ever leaves the section name."""
    assert _floor() >= (2024, 9, 0)
