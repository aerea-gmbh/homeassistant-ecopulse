"""The alarm sensors of the gateway (contract schema 3, #626 S7.4, ADR-231 §9).

The gateway turns the cloud's ``alerts.state`` push into the aggregate ``alarm`` and one
``alarm_{ruleId}`` per rule of the plant; the plugin shows what the catalog and the stream carry.
``alarm`` is named by this integration's translations, a rule's sensor after the rule (the
catalog's name). A rule that appears, goes or is renamed is a catalog change: the entry reloads
and the registry follows.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import ALARM_KEY, ALARM_RULE_TRANSLATION_KEY

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import GATEWAY_DEVICE, device, entity_id, state

ALARM = f"{GATEWAY_DEVICE}:alarm"
BATTERY_RULE = f"{GATEWAY_DEVICE}:alarm_8b3f6c1e-2d4a-4e5b-9c7d-1a2b3c4d5e6f"
GRID_RULE = f"{GATEWAY_DEVICE}:alarm_9c4a7d2f-3e5b-4f6c-8d9e-2b3c4d5e6f70"
HEAT_PUMP_RULE = f"{GATEWAY_DEVICE}:alarm_ad5b8e3a-4f6c-4a7d-9e0f-3c4d5e6f7081"
NEW_RULE = f"{GATEWAY_DEVICE}:alarm_cf7d0a5c-6b8e-4c9f-9a2b-5e6f708192a3"
NEW_REVISION = "7a1c9e40b2d63f58"
BINARY = "binary_sensor"


def _registry_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


def _catalog_entities(gateway: FakeGateway) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = gateway.catalog["gateway"]["entities"]
    return entities


async def _push_catalog_change(
    hass: HomeAssistant, entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.catalog["catalogRevision"] = NEW_REVISION
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.push("catalog", {"catalogRevision": NEW_REVISION})
    await wait_until(hass, lambda: entry.runtime_data.coordinator.catalog.revision == NEW_REVISION)
    await hass.async_block_till_done()


async def test_the_alarm_aggregate_sits_on_the_gateway_device_with_its_translated_name(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, ALARM, BINARY))
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
    assert registry_entry.translation_key == ALARM_KEY
    assert registry_entry.entity_category is None
    current = state(hass, ALARM, BINARY)
    assert current.name == "Haus Nord Alarm"
    assert current.state == STATE_ON
    assert current.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    assert current.attributes["active_count"] == 3
    assert current.attributes["critical_count"] == 1
    assert current.attributes["alarms"][0] == {
        "rule": "Battery temperature high",
        "severity": "critical",
        "since": "2026-10-08T09:12:41.000Z",
    }


async def test_the_alarm_aggregate_is_named_in_german(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    await hass.config.async_update(language="de")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    assert state(hass, ALARM, BINARY).name == "Haus Nord Alarmmeldung"
    # A rule's sensor keeps the rule's own name in every language.
    assert state(hass, BATTERY_RULE, BINARY).name == "Haus Nord Battery temperature high"


async def test_a_rule_s_sensor_is_named_after_the_rule_and_carries_its_incident(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, HEAT_PUMP_RULE, BINARY))
    assert registry_entry is not None
    assert registry_entry.translation_key == ALARM_RULE_TRANSLATION_KEY
    heat_pump = state(hass, HEAT_PUMP_RULE, BINARY)
    assert heat_pump.name == "Haus Nord Heat pump alarm"
    # D10: acknowledged and in maintenance, but still firing: on.
    assert heat_pump.state == STATE_ON
    assert heat_pump.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    assert heat_pump.attributes["acknowledged"] is True
    assert heat_pump.attributes["in_maintenance"] is True
    assert heat_pump.attributes["message"] == "Heat pump reports alarm 162 (high pressure)"
    assert heat_pump.attributes["severity"] == "warning"

    grid = state(hass, GRID_RULE, BINARY)
    assert grid.state == STATE_OFF
    assert grid.attributes["since"] is None


async def test_the_alarm_list_and_synced_at_stay_out_of_the_recorder(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    aggregate = state(hass, ALARM, BINARY).state_info
    rule = state(hass, BATTERY_RULE, BINARY).state_info
    assert aggregate is not None
    assert rule is not None
    unrecorded = aggregate["unrecorded_attributes"]
    assert {"alarms", "synced_at"} <= unrecorded
    assert not {"active_count", "critical_count", "cloud_synced"} & unrecorded
    # A rule's attributes are scalars and stay in the history: when it fired, why, whether silenced.
    scalars = {"since", "message", "acknowledged", "in_maintenance"}
    assert not scalars & rule["unrecorded_attributes"]


async def test_a_rule_that_appears_is_added_named_after_it(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    template = next(e for e in _catalog_entities(gateway) if e["uniqueId"] == BATTERY_RULE)
    _catalog_entities(gateway).append({**template, "uniqueId": NEW_RULE, "name": "Wallbox fault"})

    await _push_catalog_change(hass, loaded_entry, gateway)

    await wait_until(hass, lambda: NEW_RULE in _registry_ids(hass, loaded_entry))
    assert state(hass, NEW_RULE, BINARY).name == "Haus Nord Wallbox fault"


async def test_a_rule_that_goes_is_removed_and_a_renamed_one_takes_its_new_name(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    assert GRID_RULE in _registry_ids(hass, loaded_entry)
    gateway.catalog["gateway"]["entities"] = [
        e for e in _catalog_entities(gateway) if e["uniqueId"] != GRID_RULE
    ]
    gateway.snapshot["values"] = [v for v in gateway.snapshot["values"] if v["id"] != GRID_RULE]
    for entity in _catalog_entities(gateway):
        if entity["uniqueId"] == BATTERY_RULE:
            entity["name"] = "Batterie zu warm"

    await _push_catalog_change(hass, loaded_entry, gateway)

    ids = _registry_ids(hass, loaded_entry)
    assert GRID_RULE not in ids
    assert ALARM in ids
    await wait_until(
        hass, lambda: state(hass, BATTERY_RULE, BINARY).name == "Haus Nord Batterie zu warm"
    )
