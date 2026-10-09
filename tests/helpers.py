"""Small lookups shared by the entity tests."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

SOC = "0b6e1d2c-4a5f-4e3b-8c7d-9a1b2c3d4e5f"
CHARGE_ENERGY = "1c7f2e3d-5b6a-4f4c-9d8e-0b2c3d4e5f60"
CELL_TEMPERATURE = "2d8a3f4e-6c7b-4a5d-8e9f-1c3d4e5f6071"
CHARGE_ENABLE = "3e9b4a5f-7d8c-4b6e-9f0a-2d4e5f607182"
WALLBOX_SETPOINT = "6c2e7d8a-0a1f-4e9b-8c3d-5a7182930415"
GRID_POWER = "5b1d6c7f-9f0e-4d8a-9b2c-4f6071829304"
SOLAR_EXCESS = "gw-7f3a9c:solar_excess_power"  # gateway entity, schema 2 (#623)
SURPLUS_AVAILABLE = "gw-7f3a9c:solar_excess_power:surplus_available"  # derived by the plugin

GATEWAY_DEVICE = "gw-7f3a9c"

BATTERY_DEVICE = "3f1c2a54-8d0e-4b7a-9c61-2e5f4a7b8c90"
WALLBOX_DEVICE = "7d3f8e9b-1b2a-4fac-9d4e-6b8293a41526"
METER_DEVICE = "4a0c5b6e-8e9d-4c7f-8a1b-3e5f60718293"


def entity_id(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    found = er.async_get(hass).async_get_entity_id(platform, "ecopulse", unique_id)
    assert found is not None, f"no {platform} entity with unique id {unique_id}"
    return found


def state(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> State:
    current = hass.states.get(entity_id(hass, unique_id, platform))
    assert current is not None
    return current


def device(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    """Look a device up by its ecopulse identifier on any supported HA version.

    ``async_get_device(identifiers=...)`` and mapping access to ``devices`` are deprecated from
    2026.x on, and their replacements do not exist in the minimum version (2025.10).
    """
    registry = dr.async_get(hass)
    for config_entry in hass.config_entries.async_entries("ecopulse"):
        for entry in dr.async_entries_for_config_entry(registry, config_entry.entry_id):
            if ("ecopulse", identifier) in entry.identifiers:
                return entry
    return None
