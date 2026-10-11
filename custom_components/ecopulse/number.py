"""Numbers: an assistant's battery-support start and stop SoC (schema 7, #627), offered only with
the control scope over TLS. The bounds are the catalog's: the gateway folds the plant's minimum SoC
into ``min`` and still refuses a value outside them (or a stop above the start)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
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
        EcoPulseControlNumber(data.coordinator, data.controls, control)
        for control in data.controls.for_platform("number")
    )


class EcoPulseControlNumber(EcoPulseControlEntity, NumberEntity):
    # A box, not a slider: a slider posts while it is dragged, and the gateway takes one a second.
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        controls: EcoPulseControls,
        control: CatalogControl,
    ) -> None:
        super().__init__(coordinator, controls, control)
        if control.min is not None:
            self._attr_native_min_value = control.min
        if control.max is not None:
            self._attr_native_max_value = control.max
        if control.step is not None:
            self._attr_native_step = control.step
        self._attr_native_unit_of_measurement = control.unit

    @property
    def native_value(self) -> float | None:
        value = self._raw_value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    async def async_set_native_value(self, value: float) -> None:
        # Whole numbers go out as JSON integers (the contract's SoC steps are whole percent).
        await self._async_set(int(value) if float(value).is_integer() else value)
