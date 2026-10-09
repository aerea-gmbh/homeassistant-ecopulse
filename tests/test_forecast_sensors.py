"""The forecast and price sensors of the gateway (contract schema 3, #626, ADR-231 S7.2).

The gateway computes them from the cloud's ``forecast.summary`` push; the plugin shows what the
catalog and the stream carry. They sit on the gateway device, are named by this integration's
translations and carry ``cloud_synced``/``synced_at``/``computed_at`` plus their own attributes,
the arrays and ``synced_at`` kept out of the recorder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import FORECAST_ENTITY_KEYS

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import GATEWAY_DEVICE, device, entity_id, state

PV_TODAY = f"{GATEWAY_DEVICE}:pv_forecast_today"
PV_POWER_NOW = f"{GATEWAY_DEVICE}:pv_forecast_power_now"
CONSUMPTION_TODAY = f"{GATEWAY_DEVICE}:consumption_forecast_today"
PRICE_NOW = f"{GATEWAY_DEVICE}:price_now"
NEW_REVISION = "5e3b9f72c8d4a1a6"

ENGLISH_NAMES = {
    "pv_forecast_today": "PV forecast today",
    "pv_forecast_remaining_today": "PV forecast remaining today",
    "pv_forecast_tomorrow": "PV forecast tomorrow",
    "pv_forecast_power_now": "PV forecast power now",
    "pv_forecast_power_next_hour": "PV forecast power next hour",
    "consumption_forecast_today": "Consumption forecast today",
    "consumption_forecast_remaining_today": "Consumption forecast remaining today",
    "consumption_forecast_tomorrow": "Consumption forecast tomorrow",
    "price_now": "Electricity price",
}
GERMAN_NAMES = {
    "pv_forecast_today": "PV-Prognose heute",
    "pv_forecast_remaining_today": "PV-Prognose Rest heute",
    "pv_forecast_tomorrow": "PV-Prognose morgen",
    "pv_forecast_power_now": "PV-Prognose Leistung jetzt",
    "pv_forecast_power_next_hour": "PV-Prognose Leistung nächste Stunde",
    "consumption_forecast_today": "Verbrauchsprognose heute",
    "consumption_forecast_remaining_today": "Verbrauchsprognose Rest heute",
    "consumption_forecast_tomorrow": "Verbrauchsprognose morgen",
    "price_now": "Strompreis",
}


def _unique_id(key: str) -> str:
    return f"{GATEWAY_DEVICE}:{key}"


def _value(unique_id: str, value: Any, attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        "values": [
            {
                "id": unique_id,
                "value": value,
                "ts": "2026-10-09T12:00:00.000Z",
                "attributes": attributes,
            }
        ]
    }


async def _push_catalog_change(
    hass: HomeAssistant, entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.catalog["catalogRevision"] = NEW_REVISION
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.push("catalog", {"catalogRevision": NEW_REVISION})
    await wait_until(hass, lambda: entry.runtime_data.coordinator.catalog.revision == NEW_REVISION)
    await hass.async_block_till_done()


def _registry_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


def test_the_forecast_keys_are_the_ones_this_test_names() -> None:
    assert frozenset(ENGLISH_NAMES) == FORECAST_ENTITY_KEYS


@pytest.mark.parametrize("key", sorted(ENGLISH_NAMES))
async def test_every_forecast_sensor_sits_on_the_gateway_device_with_its_translated_name(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, key: str
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, _unique_id(key)))
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
    assert registry_entry.entity_category is None
    assert registry_entry.translation_key == key
    assert state(hass, _unique_id(key)).name == f"Haus Nord {ENGLISH_NAMES[key]}"


async def test_the_names_follow_the_home_assistant_language(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    await hass.config.async_update(language="de")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    for key, name in GERMAN_NAMES.items():
        assert state(hass, _unique_id(key)).name == f"Haus Nord {name}", key


async def test_the_energy_and_power_sensors_take_their_classes_from_the_catalog(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    today = state(hass, PV_TODAY)
    assert today.state == "19.506"
    assert today.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENERGY
    assert today.attributes[ATTR_UNIT_OF_MEASUREMENT] == "kWh"
    # A forecast is no meter: no state class, so Home Assistant invents no statistics.
    assert "state_class" not in today.attributes

    power = state(hass, PV_POWER_NOW)
    assert power.state == "2600.5"
    assert power.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
    assert power.attributes["state_class"] == SensorStateClass.MEASUREMENT


async def test_the_forecast_carries_the_cloud_attributes_and_its_hourly_curve(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    attributes = state(hass, CONSUMPTION_TODAY).attributes
    assert attributes["cloud_synced"] is True
    assert attributes["synced_at"] == "2026-10-08T09:15:00.412Z"
    assert attributes["computed_at"] == "2026-10-08T03:10:27.000Z"
    assert len(attributes["hourly"]) == 24
    assert attributes["hourly"][0] == {"start": "2026-10-07T22:00:00.000Z", "wh": 350.25}


async def test_the_price_unit_is_the_currency_the_catalog_resolved(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    price = state(hass, PRICE_NOW)
    assert price.state == "0.312"
    assert price.attributes[ATTR_UNIT_OF_MEASUREMENT] == "EUR/kWh"
    assert ATTR_DEVICE_CLASS not in price.attributes  # monetary takes no "per kWh"
    assert price.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert price.attributes["export_now"] == 0.081
    assert price.attributes["slot_start"] == "2026-10-08T09:15:00.000Z"
    assert price.attributes["slot_end"] == "2026-10-08T09:30:00.000Z"
    assert price.attributes["prices"][7] == {
        "start": "2026-10-08T11:00:00.000Z",
        "import": None,
        "export": None,
    }


async def test_no_gateway_sensor_shows_a_unit_placeholder(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    for entry in er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id):
        if entry.disabled_by is not None:
            continue  # a cell entity the catalog serves disabled has no state
        current = hass.states.get(entry.entity_id)
        assert current is not None, entry.unique_id
        unit = current.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        assert unit is None or "{" not in unit, (entry.unique_id, unit)


async def test_another_currency_from_the_catalog_reaches_the_unit(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    for entity in gateway.catalog["gateway"]["entities"]:
        if entity["uniqueId"] == PRICE_NOW:
            entity["unit"] = "CHF/kWh"
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    assert state(hass, PRICE_NOW).attributes[ATTR_UNIT_OF_MEASUREMENT] == "CHF/kWh"


async def test_a_fixed_price_has_no_slot_and_an_empty_price_list(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-231 decision 11: a flat tariff shows one value, no slot, "prices" empty.
    fixed = {
        "cloud_synced": True,
        "synced_at": "2026-10-09T11:59:58.000Z",
        "computed_at": "2026-10-09T11:59:58.000Z",
        "slot_start": None,
        "slot_end": None,
        "export_now": None,
        "prices": [],
    }
    gateway.push("values", _value(PRICE_NOW, 0.3189, fixed))

    await wait_until(hass, lambda: state(hass, PRICE_NOW).state == "0.3189")
    attributes = state(hass, PRICE_NOW).attributes
    assert attributes["prices"] == []
    assert attributes["slot_start"] is None
    assert attributes["export_now"] is None


async def test_a_value_beyond_the_horizon_is_unknown_and_an_unsynced_one_stays_available(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-231 decisions 7 and 8: past the pushed horizon the value is null ("unknown"); a kind
    # the cloud no longer delivers keeps its value and only the flag says so.
    unsynced = {
        "cloud_synced": False,
        "synced_at": "2026-10-08T09:15:00.412Z",
        "computed_at": "2026-10-08T09:05:12.000Z",
    }
    gateway.push("values", _value(PV_POWER_NOW, 1234.5, unsynced))
    await wait_until(hass, lambda: state(hass, PV_POWER_NOW).state == "1234.5")
    assert state(hass, PV_POWER_NOW).attributes["cloud_synced"] is False

    gateway.push("values", _value(PV_POWER_NOW, None, unsynced))

    await wait_until(hass, lambda: state(hass, PV_POWER_NOW).state == STATE_UNKNOWN)
    assert state(hass, PV_POWER_NOW).state != STATE_UNAVAILABLE


async def test_the_curves_and_synced_at_stay_out_of_the_recorder(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    for unique_id in (PV_TODAY, PRICE_NOW):
        state_info = state(hass, unique_id).state_info
        assert state_info is not None
        unrecorded = state_info["unrecorded_attributes"]
        assert {"hourly", "prices", "synced_at"} <= unrecorded
        # The scalars stay in the history: when it was computed, whether it was synced.
        assert not {"computed_at", "cloud_synced", "slot_start", "export_now"} & unrecorded


async def test_a_section_that_turns_null_removes_its_sensors(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-231 decision 11: the gateway lists the price entity only while the forecast carries
    # a price; a later push without one is a catalog change, and the entity goes.
    assert PRICE_NOW in _registry_ids(hass, loaded_entry)
    gateway.catalog["gateway"]["entities"] = [
        e for e in gateway.catalog["gateway"]["entities"] if e["uniqueId"] != PRICE_NOW
    ]
    gateway.snapshot["values"] = [v for v in gateway.snapshot["values"] if v["id"] != PRICE_NOW]

    await _push_catalog_change(hass, loaded_entry, gateway)

    ids = _registry_ids(hass, loaded_entry)
    assert PRICE_NOW not in ids
    assert PV_TODAY in ids


async def test_a_section_that_appears_adds_its_sensors(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    removed = [
        e
        for e in gateway.catalog["gateway"]["entities"]
        if e["uniqueId"].endswith("_forecast_today")
    ]
    gateway.catalog["gateway"]["entities"] = [
        e for e in gateway.catalog["gateway"]["entities"] if e not in removed
    ]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)
    assert PV_TODAY not in _registry_ids(hass, config_entry)

    gateway.catalog["gateway"]["entities"].extend(removed)
    await _push_catalog_change(hass, config_entry, gateway)

    await wait_until(hass, lambda: PV_TODAY in _registry_ids(hass, config_entry))
    assert state(hass, PV_TODAY).name == "Haus Nord PV forecast today"
