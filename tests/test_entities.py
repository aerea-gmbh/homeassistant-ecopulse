"""Devices and entities built from the catalog, and their values from the stream."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import (
    BATTERY_DEVICE,
    CELL_TEMPERATURE,
    CHARGE_ENABLE,
    CHARGE_ENERGY,
    EXAMPLE_GATEWAY_ENTITIES,
    GRID_POWER,
    METER_DEVICE,
    SOC,
    SOLAR_EXCESS,
    SURPLUS_AVAILABLE,
    WALLBOX_DEVICE,
    WALLBOX_SETPOINT,
    device,
    entity_id,
    state,
)


async def test_one_device_per_aggregate_under_the_gateway(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    gateway = device(hass, "gw-7f3a9c")
    assert gateway is not None
    assert gateway.name == "Haus Nord"
    assert gateway.sw_version == "2026.10.1"

    battery = device(hass, BATTERY_DEVICE)
    assert battery is not None
    assert (battery.name, battery.manufacturer, battery.model) == (
        "Home battery",
        "Seplos",
        "SEPLOS_V3",
    )
    assert battery.via_device_id == gateway.id
    for device_id in (WALLBOX_DEVICE, METER_DEVICE):
        child = device(hass, device_id)
        assert child is not None
        assert child.via_device_id == gateway.id
        assert child.manufacturer is None


async def test_every_catalog_entity_is_registered_with_its_unique_id(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    entries = er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id)

    assert sorted((e.domain, e.unique_id) for e in entries) == sorted(
        [
            ("sensor", SOC),
            ("sensor", CHARGE_ENERGY),
            ("sensor", CELL_TEMPERATURE),
            ("binary_sensor", CHARGE_ENABLE),
            ("sensor", WALLBOX_SETPOINT),
            ("sensor", GRID_POWER),
            ("binary_sensor", SURPLUS_AVAILABLE),
            # Every gateway entity of the example, solar_excess_power and the schema 3 ones (#626).
            *EXAMPLE_GATEWAY_ENTITIES,
        ]
    )
    assert ("sensor", SOLAR_EXCESS) in EXAMPLE_GATEWAY_ENTITIES


async def test_sensor_metadata_and_snapshot_values(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    soc = state(hass, SOC)
    assert soc.state == "87"
    assert soc.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.BATTERY
    assert soc.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert soc.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert soc.name == "Home battery State of charge"

    energy = state(hass, CHARGE_ENERGY)
    assert energy.state == "1234.56"
    assert energy.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
    assert energy.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"

    registry = er.async_get(hass)
    soc_entry = registry.async_get(entity_id(hass, SOC))
    assert soc_entry is not None
    assert soc_entry.options["sensor"]["suggested_display_precision"] == 0
    assert soc_entry.entity_category is None
    energy_entry = registry.async_get(entity_id(hass, CHARGE_ENERGY))
    assert energy_entry is not None
    assert energy_entry.options["sensor"]["suggested_display_precision"] == 2


async def test_diagnostic_enabled_entity(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    entry = er.async_get(hass).async_get(entity_id(hass, WALLBOX_SETPOINT))
    assert entry is not None
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.disabled_by is None
    # value null + ts null = never read (ADR-230 L1) -> unknown, not unavailable
    assert state(hass, WALLBOX_SETPOINT).state == STATE_UNKNOWN


async def test_cell_and_bms_internals_are_diagnostic_and_disabled_by_default(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for unique_id, platform in ((CELL_TEMPERATURE, "sensor"), (CHARGE_ENABLE, "binary_sensor")):
        entry = registry.async_get(entity_id(hass, unique_id, platform))
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entry.entity_id) is None


async def test_a_non_finite_value_with_a_timestamp_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert state(hass, GRID_POWER).state == STATE_UNKNOWN


async def test_values_events_update_the_state_live(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    gateway.push("values", contract["examples"]["stream"]["values"])

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "-2310")
    assert state(hass, SOC).state == "87"  # untouched by the batch


async def test_a_value_of_the_wrong_type_for_a_numeric_sensor_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.push("values", {"values": [{"id": SOC, "value": "full", "ts": "2026-10-08T09:15:06Z"}]})
    gateway.push(
        "values", {"values": [{"id": GRID_POWER, "value": 5, "ts": "2026-10-08T09:15:06Z"}]}
    )

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "5")
    assert state(hass, SOC).state == STATE_UNKNOWN


async def test_unknown_value_ids_are_ignored(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    unknown = "99999999-0000-4000-8000-000000000000"
    gateway.push(
        "values",
        {
            "values": [
                {"id": unknown, "value": 1, "ts": "2026-10-08T09:15:06Z"},
                {"id": GRID_POWER, "value": 7, "ts": "2026-10-08T09:15:06Z"},
            ]
        },
    )

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "7")
    assert er.async_get(hass).async_get_entity_id("sensor", "ecopulse", unknown) is None
    assert unknown not in loaded_entry.runtime_data.coordinator.values


async def test_binary_sensor_follows_the_stream_once_enabled(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id(hass, CHARGE_ENABLE, "binary_sensor"), disabled_by=None)
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: loaded_entry.runtime_data.coordinator.connected)

    assert state(hass, CHARGE_ENABLE, "binary_sensor").state == STATE_ON
    gateway.push("values", contract["examples"]["stream"]["values"])
    await wait_until(hass, lambda: state(hass, CHARGE_ENABLE, "binary_sensor").state == STATE_OFF)


async def test_availability_event_marks_only_that_device_unavailable(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    gateway.push("availability", contract["examples"]["stream"]["availability"])

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "unavailable")
    assert state(hass, SOC).state == "87"

    gateway.push("availability", {"devices": [{"id": METER_DEVICE, "available": True}]})
    await wait_until(hass, lambda: state(hass, GRID_POWER).state == STATE_UNKNOWN)


async def test_a_snapshot_that_reports_a_device_unavailable_is_honoured(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    gateway.snapshot["devices"][0]["available"] = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    assert state(hass, SOC).state == "unavailable"
    assert state(hass, GRID_POWER).state == STATE_UNKNOWN


async def test_malformed_event_payloads_are_ignored_and_the_stream_lives_on(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.push("values", {"values": 5})
    gateway.push("values", {"values": ["x", {"value": 1}]})
    gateway.push("values", {"values": [{"id": ["not", "hashable"], "value": 1}]})
    gateway.push("values", {"values": [{"id": {"an": "object"}, "value": 1}]})
    gateway.push("availability", {"devices": [{"id": ["x"], "available": False}]})
    gateway.push("availability", {"devices": 7})
    gateway.push("values", {"values": [{"id": GRID_POWER, "value": 9, "ts": "2026-10-08T09:17Z"}]})

    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "9")
    assert loaded_entry.runtime_data.coordinator.stream_task is not None
    assert not loaded_entry.runtime_data.coordinator.stream_task.done()
    assert gateway.stream_connections == 1
