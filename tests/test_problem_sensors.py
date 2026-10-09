"""The gateway's own problem sensors and value attributes (contract schema 3, #626, ADR-231).

``cloud_connected``, ``driver_problem`` and ``directive_fallback`` are binary sensors on the
gateway device, named by this integration's translations. A value's ``attributes`` become the
entity's state attributes, replaced with every value, the arrays kept out of the recorder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import UNRECORDED_ATTRIBUTE_KEYS

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import (
    CLOUD_CONNECTED,
    DIRECTIVE_FALLBACK,
    DRIVER_PROBLEM,
    GATEWAY_DEVICE,
    SOC,
    SOLAR_EXCESS,
    device,
    entity_id,
    state,
)

PROBLEM_SENSORS = (CLOUD_CONNECTED, DRIVER_PROBLEM, DIRECTIVE_FALLBACK)

FAILED_DRIVER = {
    "id": "modbus-grid",
    "state": "failed",
    "since": "2026-10-08T09:15:04.000Z",
    "error": "Connection timed out",
}


def _value(unique_id: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {
        "values": [{"id": unique_id, "value": value, "ts": "2026-10-09T12:00:00.000Z", **extra}]
    }


@pytest.mark.parametrize(
    ("unique_id", "name", "device_class"),
    [
        (CLOUD_CONNECTED, "Haus Nord Cloud connection", BinarySensorDeviceClass.CONNECTIVITY),
        (DRIVER_PROBLEM, "Haus Nord Driver problem", BinarySensorDeviceClass.PROBLEM),
        (DIRECTIVE_FALLBACK, "Haus Nord Optimizer fallback", BinarySensorDeviceClass.PROBLEM),
    ],
)
async def test_the_problem_sensors_are_diagnostic_binary_sensors_on_the_gateway_device(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    unique_id: str,
    name: str,
    device_class: BinarySensorDeviceClass,
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, unique_id, "binary_sensor"))
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
    assert registry_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert registry_entry.translation_key == unique_id.split(":")[1]

    current = state(hass, unique_id, "binary_sensor")
    assert current.name == name
    assert current.attributes[ATTR_DEVICE_CLASS] == device_class


async def test_the_snapshot_sets_their_states_and_their_empty_lists(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert state(hass, CLOUD_CONNECTED, "binary_sensor").state == STATE_ON
    driver_problem = state(hass, DRIVER_PROBLEM, "binary_sensor")
    assert driver_problem.state == STATE_OFF
    assert driver_problem.attributes["drivers"] == []
    assert state(hass, DIRECTIVE_FALLBACK, "binary_sensor").attributes["assistants"] == []
    # cloud_connected has no attributes, and an entity without any gets none.
    assert "drivers" not in state(hass, CLOUD_CONNECTED, "binary_sensor").attributes


async def test_a_values_event_carries_the_failed_driver_as_an_attribute(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    gateway.push("values", contract["examples"]["stream"]["values"])

    await wait_until(hass, lambda: state(hass, DRIVER_PROBLEM, "binary_sensor").state == STATE_ON)
    assert state(hass, DRIVER_PROBLEM, "binary_sensor").attributes["drivers"] == [FAILED_DRIVER]


async def test_attributes_are_replaced_never_merged(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.push("values", _value(DRIVER_PROBLEM, True, attributes={"drivers": [FAILED_DRIVER]}))
    await wait_until(hass, lambda: state(hass, DRIVER_PROBLEM, "binary_sensor").state == STATE_ON)

    gateway.push("values", _value(DRIVER_PROBLEM, False, attributes={"drivers": []}))

    await wait_until(hass, lambda: state(hass, DRIVER_PROBLEM, "binary_sensor").state == STATE_OFF)
    assert state(hass, DRIVER_PROBLEM, "binary_sensor").attributes["drivers"] == []


async def test_the_array_attributes_stay_out_of_the_recorder(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    for unique_id, platform in ((DRIVER_PROBLEM, "binary_sensor"), (SOC, "sensor")):
        state_info = state(hass, unique_id, platform).state_info
        assert state_info is not None
        assert state_info["unrecorded_attributes"] >= UNRECORDED_ATTRIBUTE_KEYS


async def test_unknown_value_properties_and_malformed_attributes_are_ignored(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # Tolerant receiver (ADR-230 §10, ADR-231 decision 13): an additive property is ignored,
    # attributes that are no object are dropped, and the value itself still applies.
    gateway.push("values", _value(SOLAR_EXCESS, 640, quality="good", attributes="not an object"))

    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == "640")
    attributes = state(hass, SOLAR_EXCESS).attributes
    assert "quality" not in attributes
    assert "drivers" not in attributes


async def test_the_names_follow_the_home_assistant_language(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    await hass.config.async_update(language="de")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    assert state(hass, CLOUD_CONNECTED, "binary_sensor").name == "Haus Nord Cloud-Verbindung"
    assert state(hass, DRIVER_PROBLEM, "binary_sensor").name == "Haus Nord Treiberproblem"
    assert (
        state(hass, DIRECTIVE_FALLBACK, "binary_sensor").name
        == "Haus Nord Optimierer-Rückfallebene"
    )
    # A catalog-named entity keeps the name the gateway serves.
    assert state(hass, SOLAR_EXCESS).name == "Haus Nord Solar excess power"


async def test_a_schema_2_gateway_without_attributes_or_problem_sensors_still_works(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    # Firmware of #623: solar_excess_power is the one gateway entity, no value has attributes.
    gateway.catalog["gateway"]["entities"] = [
        e for e in gateway.catalog["gateway"]["entities"] if e["uniqueId"] == SOLAR_EXCESS
    ]
    gateway.snapshot["values"] = [
        {k: v for k, v in value.items() if k != "attributes"}
        for value in gateway.snapshot["values"]
        if not value["id"].startswith(f"{GATEWAY_DEVICE}:") or value["id"] == SOLAR_EXCESS
    ]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    gateway_ids = {
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
        if e.unique_id.startswith(f"{GATEWAY_DEVICE}:")
    }
    assert gateway_ids == {SOLAR_EXCESS, f"{SOLAR_EXCESS}:surplus_available"}
    assert state(hass, SOC).state == "87"
    assert "drivers" not in state(hass, SOC).attributes
