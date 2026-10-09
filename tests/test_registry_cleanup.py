"""What happens to registry entries when the catalog changes (signal or aggregate removed)."""

from __future__ import annotations

import attr
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse import async_remove_config_entry_device
from custom_components.ecopulse.const import DOMAIN

from .conftest import wait_until
from .fake_gateway import FakeGateway
from .helpers import (
    BATTERY_DEVICE,
    CHARGE_ENABLE,
    CHARGE_ENERGY,
    EXAMPLE_GATEWAY_ENTITIES,
    EXAMPLE_SIGNALS,
    GRID_POWER,
    METER_DEVICE,
    SOC,
    device,
    state,
)

NEW_REVISION = "4d2a8e61b7c3f095"


def _registry_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[tuple[str, str]]:
    return {
        (e.domain, e.unique_id)
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def _push_catalog_change(hass: HomeAssistant, entry: MockConfigEntry, gateway: FakeGateway):
    gateway.catalog["catalogRevision"] = NEW_REVISION
    gateway.snapshot["catalogRevision"] = NEW_REVISION
    gateway.push("catalog", {"catalogRevision": NEW_REVISION})
    await wait_until(hass, lambda: entry.runtime_data.coordinator.catalog.revision == NEW_REVISION)
    await hass.async_block_till_done()


def _battery(gateway: FakeGateway) -> dict:
    return next(d for d in gateway.catalog["devices"] if d["id"] == BATTERY_DEVICE)


async def test_a_removed_signal_removes_its_entity_and_keeps_the_device(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    battery = _battery(gateway)
    battery["entities"] = [e for e in battery["entities"] if e["uniqueId"] != CHARGE_ENERGY]

    await _push_catalog_change(hass, loaded_entry, gateway)

    assert ("sensor", CHARGE_ENERGY) not in _registry_ids(hass, loaded_entry)
    assert ("sensor", SOC) in _registry_ids(hass, loaded_entry)
    assert device(hass, BATTERY_DEVICE) is not None


async def test_a_removed_aggregate_removes_its_device_and_entities(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    gateway.catalog["devices"] = [d for d in gateway.catalog["devices"] if d["id"] != METER_DEVICE]

    await _push_catalog_change(hass, loaded_entry, gateway)

    assert device(hass, METER_DEVICE) is None
    assert ("sensor", GRID_POWER) not in _registry_ids(hass, loaded_entry)
    assert device(hass, "gw-7f3a9c") is not None


async def test_an_empty_catalog_removes_nothing(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A push that renders no devices (or a projection bug) must not wipe the user's entity
    # customisations and the cell entities they enabled by hand.
    before = _registry_ids(hass, loaded_entry)
    gateway.catalog["devices"] = []
    gateway.snapshot["devices"] = []
    gateway.snapshot["values"] = []

    await _push_catalog_change(hass, loaded_entry, gateway)

    # The signals, every gateway entity, surplus_available.
    assert len(before) == len(EXAMPLE_SIGNALS) + len(EXAMPLE_GATEWAY_ENTITIES) + 1
    assert _registry_ids(hass, loaded_entry) == before
    assert device(hass, BATTERY_DEVICE) is not None
    assert state(hass, SOC).state == "unavailable"
    assert "serves no devices" in caplog.text  # counterpart of the pending-catalog test


async def test_a_platform_move_removes_the_entry_on_the_old_platform(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, gateway: FakeGateway
) -> None:
    # ADR-230 Consequences: a role/data-type change moves a signal sensor <-> binary_sensor with
    # the same unique id; the old-platform entry must not linger as an unavailable orphan.
    for entity in _battery(gateway)["entities"]:
        if entity["uniqueId"] == SOC:
            entity.update(
                platform="binary_sensor",
                unit=None,
                deviceClass=None,
                stateClass=None,
                suggestedDisplayPrecision=None,
            )

    await _push_catalog_change(hass, loaded_entry, gateway)

    ids = _registry_ids(hass, loaded_entry)
    assert ("sensor", SOC) not in ids
    assert ("binary_sensor", SOC) in ids
    assert ("binary_sensor", CHARGE_ENABLE) in ids


async def test_only_devices_missing_from_the_catalog_can_be_deleted_by_hand(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    battery = device(hass, BATTERY_DEVICE)
    gateway_device = device(hass, "gw-7f3a9c")
    assert battery is not None
    assert gateway_device is not None

    assert not await async_remove_config_entry_device(hass, loaded_entry, battery)
    assert not await async_remove_config_entry_device(hass, loaded_entry, gateway_device)

    stale = attr.evolve(battery, identifiers={(DOMAIN, "aggregate-gone")})  # not in the catalog
    assert await async_remove_config_entry_device(hass, loaded_entry, stale)
