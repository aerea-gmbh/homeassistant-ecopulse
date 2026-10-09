"""Binary sensors: every catalog entity with ``platform: binary_sensor``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EcoPulseStreamCoordinator
from .entity import EcoPulseEntity
from .models import CatalogDevice, CatalogEntity

if TYPE_CHECKING:
    from . import EcoPulseConfigEntry

PARALLEL_UPDATES = 0  # push only, nothing is polled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoPulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        EcoPulseBinarySensor(coordinator, device, entity)
        for device, entity in coordinator.catalog.entities_for("binary_sensor")
    )


class EcoPulseBinarySensor(EcoPulseEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        device: CatalogDevice,
        entity: CatalogEntity,
    ) -> None:
        super().__init__(coordinator, device, entity)
        try:
            self._attr_device_class = (
                BinarySensorDeviceClass(entity.device_class) if entity.device_class else None
            )
        except ValueError:
            self._attr_device_class = None

    @property
    def is_on(self) -> bool | None:
        # The edge already normalises to a JSON boolean (ADR-230 L1); anything else is unknown.
        value = self._raw_value
        return value if isinstance(value, bool) else None
