"""The optimizers' plan sensors of the gateway (contract schema 3, #626, ADR-231 S7.3).

The gateway computes them from the cloud's ``battery.plan`` and ``heatpump.plan`` pushes; the
plugin shows what the catalog and the stream carry. They sit on the gateway device, are named
by this integration's translations (their states too), and carry the cloud attributes plus the
running slot, the next change and the ``plan`` runs, the runs and ``synced_at`` kept out of the
recorder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import PLAN_ENTITY_KEYS

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import GATEWAY_DEVICE, device, entity_id, state

BATTERY_STATE = f"{GATEWAY_DEVICE}:battery_plan_state"
BATTERY_SOC = f"{GATEWAY_DEVICE}:battery_plan_soc"
HEATPUMP_STATE = f"{GATEWAY_DEVICE}:heatpump_plan_state"
NEW_REVISION = "6a4c0e83d9e5b2b7"
TRANSLATIONS_PATH = Path(__file__).parents[1] / "custom_components" / "ecopulse" / "translations"

ENGLISH_NAMES = {
    "battery_plan_state": "Battery plan",
    "battery_plan_soc": "Battery plan state of charge",
    "heatpump_plan_state": "Heat pump plan",
}
GERMAN_NAMES = {
    "battery_plan_state": "Batterieplan",
    "battery_plan_soc": "Batterieplan Ladezustand",
    "heatpump_plan_state": "Wärmepumpenplan",
}


def _unique_id(key: str) -> str:
    return f"{GATEWAY_DEVICE}:{key}"


def _registry_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


def test_the_plan_keys_are_the_ones_this_test_names() -> None:
    assert frozenset(ENGLISH_NAMES) == PLAN_ENTITY_KEYS


@pytest.mark.parametrize("key", sorted(ENGLISH_NAMES))
async def test_every_plan_sensor_sits_on_the_gateway_device_with_its_translated_name(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, key: str
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, _unique_id(key)))
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
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


async def test_the_plan_state_is_the_contract_value_with_its_slot_next_change_and_runs(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    battery = state(hass, BATTERY_STATE)
    assert battery.state == "zero_export"
    assert ATTR_DEVICE_CLASS not in battery.attributes
    assert battery.attributes["cloud_synced"] is True
    assert battery.attributes["computed_at"] == "2026-10-08T09:01:30.000Z"
    assert battery.attributes["slot_start"] == "2026-10-08T09:15:00.000Z"
    assert battery.attributes["next_state"] == "discharge_to_grid"
    assert battery.attributes["next_change_at"] == "2026-10-08T11:45:00.000Z"
    assert battery.attributes["plan"][1] == {
        "start": "2026-10-08T11:45:00.000Z",
        "state": "discharge_to_grid",
        "soc": 60.8,
    }
    heat_pump = state(hass, HEATPUMP_STATE)
    assert heat_pump.state == "normal"
    assert all("soc" not in run for run in heat_pump.attributes["plan"])


async def test_the_planned_soc_is_a_battery_percentage_from_the_catalog(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    soc = state(hass, BATTERY_SOC)
    assert soc.state == "64.5"
    assert soc.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.BATTERY
    assert soc.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert soc.attributes["state_class"] == SensorStateClass.MEASUREMENT


async def test_every_plan_state_and_next_state_has_a_name_in_every_language() -> None:
    # The contract's state table (cloud-facts "states") is what the gateway streams.
    expected = {
        "battery_plan_state": {
            "zero_export",
            "zero_export_with_no_excess_to_grid",
            "charge_from_grid",
            "discharge_to_grid",
        },
        "heatpump_plan_state": {"blocked", "normal", "recommended", "forced"},
    }
    for language in ("en", "de"):
        sensors = json.loads((TRANSLATIONS_PATH / f"{language}.json").read_text(encoding="utf-8"))[
            "entity"
        ]["sensor"]
        for key, states in expected.items():
            assert set(sensors[key]["state"]) == states, (language, key)
            assert set(sensors[key]["state_attributes"]["next_state"]["state"]) == states
            assert all(sensors[key]["state"].values()), (language, key)


async def test_a_plan_beyond_its_horizon_is_unknown_and_stays_available(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-231 decision 8: past the pushed horizon the state is null, never the last one.
    attributes = {
        "cloud_synced": False,
        "synced_at": "2026-10-08T09:01:31.388Z",
        "computed_at": "2026-10-08T09:01:30.000Z",
        "slot_start": None,
        "slot_end": None,
        "next_state": None,
        "next_change_at": None,
        "plan": [],
    }
    gateway.push(
        "values",
        {
            "values": [
                {
                    "id": BATTERY_STATE,
                    "value": None,
                    "ts": "2026-10-09T12:00:00.000Z",
                    "attributes": attributes,
                }
            ]
        },
    )

    await wait_until(hass, lambda: state(hass, BATTERY_STATE).state == STATE_UNKNOWN)
    assert state(hass, BATTERY_STATE).attributes["plan"] == []
    assert state(hass, BATTERY_STATE).attributes["cloud_synced"] is False


async def test_the_runs_and_synced_at_stay_out_of_the_recorder(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    for unique_id in (BATTERY_STATE, HEATPUMP_STATE):
        state_info = state(hass, unique_id).state_info
        assert state_info is not None
        unrecorded = state_info["unrecorded_attributes"]
        assert {"plan", "synced_at"} <= unrecorded
        assert not {"next_state", "next_change_at", "slot_start", "computed_at"} & unrecorded


async def test_a_plan_the_gateway_no_longer_lists_removes_its_sensors(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-231 decisions 2 and 11: a plan whose assistant left the gateway's config is dropped;
    # the catalog change removes its entities, the other kind stays.
    battery = {BATTERY_STATE, BATTERY_SOC}
    gateway.catalog["gateway"]["entities"] = [
        e for e in gateway.catalog["gateway"]["entities"] if e["uniqueId"] not in battery
    ]
    gateway.snapshot["values"] = [v for v in gateway.snapshot["values"] if v["id"] not in battery]
    gateway.catalog["catalogRevision"] = NEW_REVISION
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.push("catalog", {"catalogRevision": NEW_REVISION})
    await wait_until(
        hass, lambda: loaded_entry.runtime_data.coordinator.catalog.revision == NEW_REVISION
    )
    await hass.async_block_till_done()

    ids = _registry_ids(hass, loaded_entry)
    assert not battery & ids
    assert HEATPUMP_STATE in ids
