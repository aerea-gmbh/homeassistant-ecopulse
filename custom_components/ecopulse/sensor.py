"""Sensors: every catalog entity with ``platform: sensor``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import EcoPulseStreamCoordinator
from .entity import EcoPulseEntity
from .models import CatalogDevice, CatalogEntity

if TYPE_CHECKING:
    from . import EcoPulseConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # push only, nothing is polled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoPulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        EcoPulseSensor(coordinator, device, entity)
        for device, entity in coordinator.catalog.entities_for("sensor")
    )


class EcoPulseSensor(EcoPulseEntity, SensorEntity):
    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        device: CatalogDevice,
        entity: CatalogEntity,
    ) -> None:
        super().__init__(coordinator, device, entity)
        self._attr_device_class = _enum_or_none(SensorDeviceClass, entity.device_class, entity)
        self._attr_state_class = _enum_or_none(SensorStateClass, entity.state_class, entity)
        self._attr_native_unit_of_measurement = entity.unit
        self._attr_suggested_display_precision = entity.suggested_display_precision
        # A sensor with a unit, class or precision is numeric for Home Assistant, which rejects
        # a string state there; the categorical rows of the mapping table have none of them.
        self._numeric = (
            any((entity.unit, entity.device_class, entity.state_class))
            or entity.suggested_display_precision is not None
        )

    @property
    def native_value(self) -> StateType:
        value = self._raw_value
        if value is None:
            return None
        if self._numeric and (isinstance(value, bool) or not isinstance(value, int | float)):
            return None
        if isinstance(value, int | float | str):
            return value
        return None


def _enum_or_none[E: (SensorDeviceClass, SensorStateClass)](
    enum: type[E], value: str | None, entity: CatalogEntity
) -> E | None:
    """A class this Home Assistant does not know drops to None (a wrong class is worse)."""
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        _LOGGER.warning(
            "Entity %s: %s '%s' is unknown to this Home Assistant version, ignored",
            entity.unique_id,
            enum.__name__,
            value,
        )
        return None
