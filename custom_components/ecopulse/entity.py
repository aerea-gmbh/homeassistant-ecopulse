"""Base entity: one catalog entity, fed by the stream coordinator.

A signal sits on its aggregate device; a gateway entity (``device`` None, schema 2, #623) sits on
the gateway device and is available exactly while the stream is connected.

Schema 3 (#626): a value's ``attributes`` become the entity's state attributes, replaced with
every value; the contract's unrecorded keys (arrays such as ``drivers``) stay out of the recorder.
A rule's alarm sensor (``alarm_{ruleId}``, S7.4) is named after the rule, the catalog's name, and
takes only its attribute names from the translations.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import (
    ALARM_RULE_KEY_PREFIX,
    ALARM_RULE_TRANSLATION_KEY,
    DOMAIN,
    ENTITY_CATEGORY_DIAGNOSTIC,
    GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE,
    TRANSLATED_GATEWAY_ENTITY_KEYS,
    UNRECORDED_ATTRIBUTE_KEYS,
)
from .coordinator import EcoPulseStreamCoordinator
from .models import CatalogDevice, CatalogEntity


class EcoPulseEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = UNRECORDED_ATTRIBUTE_KEYS

    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        device: CatalogDevice | None,
        entity: CatalogEntity,
    ) -> None:
        self.coordinator = coordinator
        self._device_id = device.id if device else None
        self._attr_unique_id = entity.unique_id
        key = _gateway_entity_key(coordinator, entity) if device is None else None
        if key in TRANSLATED_GATEWAY_ENTITY_KEYS:
            self._attr_translation_key = key
        else:
            self._attr_name = entity.name
            if key is not None and key.startswith(ALARM_RULE_KEY_PREFIX):
                # The explicit name wins over the translation's; the key still names the attributes.
                self._attr_translation_key = ALARM_RULE_TRANSLATION_KEY
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if entity.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
            else None
        )
        self._attr_entity_registry_enabled_default = entity.enabled_by_default
        self._attr_device_info = (
            DeviceInfo(
                identifiers={(DOMAIN, device.id)},
                name=device.name,
                manufacturer=device.manufacturer,
                model=device.model,
                via_device=(DOMAIN, coordinator.catalog.gateway_id),
            )
            if device
            else gateway_device_info(coordinator)
        )

    @property
    def available(self) -> bool:
        if self._device_id is None:
            return self.coordinator.connected
        return self.coordinator.is_device_available(self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        assert self.unique_id is not None
        current = self.coordinator.value(self.unique_id)
        return None if current is None or current.attributes is None else dict(current.attributes)

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


def _gateway_entity_key(
    coordinator: EcoPulseStreamCoordinator, entity: CatalogEntity
) -> str | None:
    """The contract key of a gateway entity ("{gatewayId}:{key}"), None for anything else."""
    prefix = GATEWAY_ENTITY_UNIQUE_ID_TEMPLATE.format(
        gatewayId=coordinator.catalog.gateway_id, key=""
    )
    return entity.unique_id.removeprefix(prefix) if entity.unique_id.startswith(prefix) else None


def gateway_device_info(coordinator: EcoPulseStreamCoordinator) -> DeviceInfo:
    """Links to the gateway device that setup registers with its full details."""
    return DeviceInfo(identifiers={(DOMAIN, coordinator.catalog.gateway_id)})
