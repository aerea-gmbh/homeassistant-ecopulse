"""Switches: whether an assistant is enabled (schema 7, #627), offered only with the control scope
over TLS."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control import EcoPulseControlEntity

if TYPE_CHECKING:
    from . import EcoPulseConfigEntry

PARALLEL_UPDATES = 1  # one control at a time: the gateway takes one per second


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoPulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    async_add_entities(
        EcoPulseControlSwitch(data.coordinator, data.controls, control)
        for control in data.controls.for_platform("switch")
    )


class EcoPulseControlSwitch(EcoPulseControlEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        value = self._raw_value
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
