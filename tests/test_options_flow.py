"""Options flow: threshold, hysteresis and delays of "Surplus available" (#623, PO 09.10.)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import (
    CONF_SURPLUS_HYSTERESIS,
    CONF_SURPLUS_OFF_DELAY,
    CONF_SURPLUS_ON_DELAY,
    CONF_SURPLUS_THRESHOLD,
    DEFAULT_SURPLUS_HYSTERESIS_W,
    DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
    DEFAULT_SURPLUS_ON_DELAY_SECONDS,
    DEFAULT_SURPLUS_THRESHOLD_W,
)

VALID = {
    CONF_SURPLUS_THRESHOLD: 1500,
    CONF_SURPLUS_HYSTERESIS: 200,
    CONF_SURPLUS_ON_DELAY: 300,
    CONF_SURPLUS_OFF_DELAY: 60,
}


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """The flow only stores options; applying them is covered in test_surplus_available."""
    with patch("custom_components.ecopulse.async_setup_entry", return_value=True):
        yield


async def _open(hass: HomeAssistant, config_entry: MockConfigEntry) -> dict:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    return result


def _suggested(result: dict) -> dict:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_the_form_starts_with_the_defaults(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    result = await _open(hass, config_entry)

    assert _suggested(result) == {
        CONF_SURPLUS_THRESHOLD: DEFAULT_SURPLUS_THRESHOLD_W,
        CONF_SURPLUS_HYSTERESIS: DEFAULT_SURPLUS_HYSTERESIS_W,
        CONF_SURPLUS_ON_DELAY: DEFAULT_SURPLUS_ON_DELAY_SECONDS,
        CONF_SURPLUS_OFF_DELAY: DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
    }
    assert (
        DEFAULT_SURPLUS_THRESHOLD_W,
        DEFAULT_SURPLUS_HYSTERESIS_W,
        DEFAULT_SURPLUS_ON_DELAY_SECONDS,
        DEFAULT_SURPLUS_OFF_DELAY_SECONDS,
    ) == (500, 100, 120, 120)


async def test_saving_stores_whole_numbers_and_the_form_shows_them_next_time(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    result = await _open(hass, config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**VALID, CONF_SURPLUS_THRESHOLD: 1500.0}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == VALID
    assert all(type(value) is int for value in config_entry.options.values())
    assert _suggested(await hass.config_entries.options.async_init(config_entry.entry_id)) == VALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_SURPLUS_THRESHOLD, -1),
        (CONF_SURPLUS_THRESHOLD, 100_001),
        (CONF_SURPLUS_HYSTERESIS, -1),
        (CONF_SURPLUS_ON_DELAY, -1),
        (CONF_SURPLUS_ON_DELAY, 3601),
        (CONF_SURPLUS_OFF_DELAY, 3601),
    ],
)
async def test_values_out_of_range_are_rejected(
    hass: HomeAssistant, config_entry: MockConfigEntry, field: str, value: int
) -> None:
    result = await _open(hass, config_entry)

    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**VALID, field: value}
        )
    assert config_entry.options == {}
