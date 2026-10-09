"""The gateway's own entities (catalog ``gateway.entities``, contract schema 2, #623).

They sit on the gateway device, belong to no device of the ``availability`` event and are
available exactly while the stream is connected.
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
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import (
    BATTERY_DEVICE,
    GATEWAY_DEVICE,
    GRID_POWER,
    METER_DEVICE,
    SOC,
    SOLAR_EXCESS,
    SURPLUS_AVAILABLE,
    WALLBOX_DEVICE,
    device,
    entity_id,
    state,
)


def _excess(value: Any) -> dict[str, Any]:
    return {"values": [{"id": SOLAR_EXCESS, "value": value, "ts": "2026-10-09T12:00:00Z"}]}


async def test_solar_excess_power_is_a_signed_power_sensor_on_the_gateway_device(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    registry_entry = er.async_get(hass).async_get(entity_id(hass, SOLAR_EXCESS))
    gateway = device(hass, GATEWAY_DEVICE)
    assert registry_entry is not None
    assert gateway is not None
    assert registry_entry.device_id == gateway.id
    assert registry_entry.entity_category is None
    assert registry_entry.disabled_by is None
    assert registry_entry.options["sensor"]["suggested_display_precision"] == 0

    excess = state(hass, SOLAR_EXCESS)
    assert excess.name == "Haus Nord Solar excess power"
    assert excess.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
    assert excess.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert excess.attributes[ATTR_UNIT_OF_MEASUREMENT] == "W"
    # The snapshot's null with a timestamp: no usable grid reading on the edge.
    assert excess.state == STATE_UNKNOWN


async def test_values_update_it_live_with_their_sign(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway, contract: dict
) -> None:
    gateway.push("values", contract["examples"]["stream"]["values"])
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == "2160")

    gateway.push("values", _excess(-350))  # a deficit: grid import or battery discharge
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == "-350")

    gateway.push("values", _excess(None))
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == STATE_UNKNOWN)


async def test_device_availability_never_touches_it(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.push(
        "availability",
        {
            "devices": [
                {"id": device_id, "available": False}
                for device_id in (BATTERY_DEVICE, WALLBOX_DEVICE, METER_DEVICE, GATEWAY_DEVICE)
            ]
        },
    )

    await wait_until(hass, lambda: state(hass, SOC).state == STATE_UNAVAILABLE)
    # A value after the event writes the state again, so availability is re-read too.
    gateway.push("values", _excess(800))
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == "800")


async def test_it_is_unavailable_while_the_stream_is_down(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.push("values", _excess(800))
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == "800")

    gateway.enabled = False
    gateway.drop_streams()
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == STATE_UNAVAILABLE)

    gateway.enabled = True
    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: state(hass, SOLAR_EXCESS).state == STATE_UNKNOWN)


async def test_a_schema_1_gateway_without_gateway_entities_still_works(
    hass: HomeAssistant, gateway: FakeGateway, config_entry: MockConfigEntry, fast_backoff: Any
) -> None:
    # Firmware before #623: no "entities" under "gateway", no value for it in the stream.
    del gateway.catalog["gateway"]["entities"]
    gateway.snapshot["values"] = [v for v in gateway.snapshot["values"] if v["id"] != SOLAR_EXCESS]
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)

    registry = er.async_get(hass)
    ids = {
        (e.domain, e.unique_id)
        for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert len(ids) == 6
    assert ("sensor", SOLAR_EXCESS) not in ids
    assert ("binary_sensor", SURPLUS_AVAILABLE) not in ids

    gateway.push(
        "values", {"values": [*_excess(5)["values"], {"id": GRID_POWER, "value": 3, "ts": None}]}
    )
    await wait_until(hass, lambda: state(hass, GRID_POWER).state == "3")
    assert SOLAR_EXCESS not in config_entry.runtime_data.coordinator.values
    assert device(hass, GATEWAY_DEVICE) is not None


async def test_a_gateway_that_stops_serving_it_drops_both_entities(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # The edge lists it only while a PV assistant binds gridPower (ADR-230 S4).
    new_revision = "4d2a8e61b7c3f095"
    gateway.catalog["gateway"]["entities"] = []
    gateway.catalog["catalogRevision"] = new_revision
    gateway.snapshot["catalogRevision"] = new_revision
    gateway.push("catalog", {"catalogRevision": new_revision})
    await wait_until(
        hass, lambda: loaded_entry.runtime_data.coordinator.catalog.revision == new_revision
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", "ecopulse", SOLAR_EXCESS) is None
    assert registry.async_get_entity_id("binary_sensor", "ecopulse", SURPLUS_AVAILABLE) is None
    assert registry.async_get_entity_id("sensor", "ecopulse", SOC) is not None


async def test_customisations_of_both_entities_survive_a_reload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # The stale-entry cleanup on setup must count them as current, or every reload would drop
    # and recreate them with the user's names and settings gone.
    registry = er.async_get(hass)
    excess_id = entity_id(hass, SOLAR_EXCESS)
    surplus_id = entity_id(hass, SURPLUS_AVAILABLE, "binary_sensor")
    registry.async_update_entity(excess_id, name="PV spare")
    registry.async_update_entity(surplus_id, name="Pool may run")
    removed: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: (
            removed.append(event.data["entity_id"]) if event.data["action"] == "remove" else None
        ),
    )

    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await gateway.wait_for_stream(2)
    await wait_until(hass, lambda: loaded_entry.runtime_data.coordinator.connected)

    excess_entry = registry.async_get(excess_id)
    surplus_entry = registry.async_get(surplus_id)
    assert excess_entry is not None
    assert surplus_entry is not None
    assert (excess_entry.name, surplus_entry.name) == ("PV spare", "Pool may run")
    # Newer HA versions restore a deleted entry's name on re-creation, so the names alone would
    # not show a remove-and-recreate.
    assert removed == []
