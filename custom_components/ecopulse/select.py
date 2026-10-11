"""Selects: an assistant's mode (schema 7, #627), offered only with the control scope over TLS."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control import EcoPulseControlEntity, EcoPulseControls
from .coordinator import EcoPulseStreamCoordinator
from .models import CatalogControl

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
        EcoPulseControlSelect(data.coordinator, data.controls, control)
        for control in data.controls.for_platform("select")
    )


class EcoPulseControlSelect(EcoPulseControlEntity, SelectEntity):
    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        controls: EcoPulseControls,
        control: CatalogControl,
    ) -> None:
        super().__init__(coordinator, controls, control)
        self._attr_options = list(control.options or ())

    @property
    def current_option(self) -> str | None:
        value = self._raw_value
        return value if isinstance(value, str) and value in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set(option)
