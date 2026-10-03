"""Devstat temperature attributes follow the sensor's unit (bench finding 1).

Devstat page 5 reports lifetime_max and lifetime_min in Celsius. Home Assistant
converts a temperature sensor's state to the user's unit but leaves attributes
alone, so on a Fahrenheit install the state read 95.0 F beside raw 46 and 17.
`temperature_attrs_in_unit` converts those two attributes to match the state.
"""

from __future__ import annotations

from custom_components.smart_sniffer.devstat import (
    TEMPERATURE_ATTRIBUTES,
    temperature_attrs_in_unit,
)

PAGE = {
    "current": 35,
    "lifetime_max": 46,
    "lifetime_min": 17,
    "time_over_limit_minutes": 0,
}


def test_celsius_leaves_the_page_alone():
    assert temperature_attrs_in_unit(PAGE, "°C") is PAGE


def test_fahrenheit_converts_the_two_lifetime_values():
    out = temperature_attrs_in_unit(PAGE, "°F")
    assert out["lifetime_max"] == 114.8
    assert out["lifetime_min"] == 62.6
    assert TEMPERATURE_ATTRIBUTES == ("lifetime_max", "lifetime_min")


def test_other_keys_and_the_input_are_untouched():
    before = dict(PAGE)
    out = temperature_attrs_in_unit(PAGE, "°F")
    assert out["current"] == 35
    assert out["time_over_limit_minutes"] == 0
    assert PAGE == before


def test_non_numeric_values_are_left_as_they_are():
    attrs = {"lifetime_max": None, "lifetime_min": True, "current": "n/a"}
    out = temperature_attrs_in_unit(attrs, "°F")
    assert out == {"lifetime_max": None, "lifetime_min": True, "current": "n/a"}
    assert temperature_attrs_in_unit({}, "°F") == {}
