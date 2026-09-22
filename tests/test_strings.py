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

import copy
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
#                20240103.3, which is what HA 2024.1.0 ships, and every release
#                since.
_NEVER = None
_ALWAYS = (0, 0, 0)
_FILLED_FROM = {
    "menu_title": _NEVER,
    "section_name": (2025, 5, 0),
    "section_description": (2025, 5, 0),
    "field_label": (2025, 5, 0),
    "always": _ALWAYS,
}

# Below this release a field with no data.<field> translation renders with a
# blank label. The "|| field.name" fallback in renderShowFormStepFieldLabel
# arrived in frontend 20240828.0; HA 2024.9.0 ships 20240904.0 and 2024.8.3
# ships 20240809.0. At 20240103.3 a missing key localizes to "".
_LABEL_FALLBACK_FROM = (2024, 9, 0)

# section() options forms first render in frontend 20240904.0 (HA 2024.9).
_SECTIONS_FROM = (2024, 9, 0)


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


def test_the_guard_bites_on_a_field_label():
    """The guard has to bite, not just pass. Field labels are not filled at the
    declared floor, so a placeholder put into one of the real labels must be
    caught. Menu titles are covered by the next test."""
    strings = copy.deepcopy(_load("strings.json"))
    form = strings["options"]["step"]["threshold_form"]
    form["data"]["Command Timeout"] = "Command Timeout ({command_timeout})"
    assert _unfilled(strings, _floor()) == [
        "options.step.threshold_form.data.Command Timeout"
    ]
    assert _unfilled(strings, (2025, 5, 0)) == []


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
        att.reading_placeholders(att.current_readings(drive("ata_healthy")), "d")
    )
    assert wanted - supplied == set()


def test_every_threshold_field_has_a_label_below_the_fallback(att):
    """Below 2024.9 a field without a data.<field> translation has no label at
    all. The threshold form's fields are built at runtime from the labels, so
    each one needs an entry."""
    if _floor() >= _LABEL_FALLBACK_FROM:
        return
    form = _load("strings.json")["options"]["step"]["threshold_form"]
    assert set(att.threshold_labels()) <= set(form.get("data", {}))


def test_every_threshold_field_has_a_description(att):
    form = _load("strings.json")["options"]["step"]["threshold_form"]
    assert set(att.threshold_labels()) <= set(form.get("data_description", {}))


def test_no_sections_below_the_release_that_renders_them():
    strings = _load("strings.json")
    with_sections = [
        f"{flow}.step.{step_id}"
        for flow in ("config", "options")
        for step_id, step in strings.get(flow, {}).get("step", {}).items()
        if "sections" in step
    ]
    if _floor() < _SECTIONS_FROM:
        assert with_sections == []
