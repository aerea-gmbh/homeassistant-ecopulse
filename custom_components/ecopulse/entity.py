"""Base entity: one catalog signal on its aggregate device, fed by the stream coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, ENTITY_CATEGORY_DIAGNOSTIC
from .coordinator import EcoPulseStreamCoordinator
from .models import CatalogDevice, CatalogEntity


class EcoPulseEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        device: CatalogDevice,
        entity: CatalogEntity,
    ) -> None:
        self.coordinator = coordinator
        self._device_id = device.id
        self._attr_unique_id = entity.unique_id
        self._attr_name = entity.name
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if entity.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
            else None
        )
        self._attr_entity_registry_enabled_default = entity.enabled_by_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model,
            via_device=(DOMAIN, coordinator.catalog.gateway_id),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.is_device_available(self._device_id)

    @property
    def _raw_value(self) -> Any:
        assert self.unique_id is not None
        current = self.coordinator.value(self.unique_id)
        return None if current is None else current.value

    async def async_added_to_hass(self) -> None:
        assert self.unique_id is not None
        self.async_on_remove(
            self.coordinator.async_add_listener(self.unique_id, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
