"""Binary sensors: every catalog entity with ``platform: binary_sensor``, and "Surplus available".

"Surplus available" is the plugin's own (#623, PO 09.10.): derived from the gateway's
``solar_excess_power`` with the threshold, hysteresis and delays of the entry's options; the
rules are in ``surplus.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.dt import utcnow

from .coordinator import EcoPulseStreamCoordinator
from .entity import EcoPulseEntity, gateway_device_info
from .models import CatalogDevice, CatalogEntity
from .surplus import (
    SurplusDetector,
    SurplusSettings,
    surplus_available_unique_id,
    surplus_source,
)

if TYPE_CHECKING:
    from . import EcoPulseConfigEntry

PARALLEL_UPDATES = 0  # push only, nothing is polled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoPulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    catalog = coordinator.catalog
    entities: list[BinarySensorEntity] = [
        *(
            EcoPulseBinarySensor(coordinator, d, e)
            for d, e in catalog.entities_for("binary_sensor")
        ),
        *(
            EcoPulseBinarySensor(coordinator, None, e)
            for e in catalog.gateway_entities_for("binary_sensor")
        ),
    ]
    if (source := surplus_source(catalog)) is not None:
        entities.append(EcoPulseSurplusAvailableSensor(coordinator, entry, source))
    async_add_entities(entities)


class EcoPulseBinarySensor(EcoPulseEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        device: CatalogDevice | None,
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


class EcoPulseSurplusAvailableSensor(BinarySensorEntity, RestoreEntity):
    """On while the solar excess is enough to start a load; never unknown, never unavailable.

    Always available on purpose: a lost stream must turn it *off* (after the off-delay), since an
    automation waiting for "to: off" would not fire on a change to "unavailable".
    ``power``: on means (surplus) power is present.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "surplus_available"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        entry: ConfigEntry,
        source: CatalogEntity,
    ) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._source_id = source.unique_id
        self._attr_unique_id = surplus_available_unique_id(source)
        self._attr_device_info = gateway_device_info(coordinator)
        self._detector = SurplusDetector(SurplusSettings.from_options(entry.options))
        self._timer_due: datetime | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        return self._detector.is_on

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == STATE_ON:
            # Held across a restart, then judged by the off rule like any on state: a restart
            # alone does not cycle the load, a missing or low excess still ends it.
            self._detector = SurplusDetector(
                SurplusSettings.from_options(self._entry.options), is_on=True
            )
        self.async_on_remove(
            self.coordinator.async_add_listener(self._source_id, self._handle_excess)
        )
        self.async_on_remove(self._entry.add_update_listener(self._async_options_updated))
        self.async_on_remove(self._unschedule)
        self._schedule(self._detector.update(self._excess(), utcnow()))

    def _excess(self) -> Any:
        """None while the stream is down: then nothing is known about the surplus. Whatever
        else the edge sent goes to the detector as is; it treats non-numbers as unknown."""
        if not self.coordinator.connected:
            return None
        current = self.coordinator.value(self._source_id)
        return None if current is None else current.value

    @callback
    def _handle_excess(self) -> None:
        was_on = self._detector.is_on
        self._settle(was_on, self._detector.update(self._excess(), utcnow()))

    async def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        was_on = self._detector.is_on
        settings = SurplusSettings.from_options(entry.options)
        self._settle(was_on, self._detector.reconfigure(settings, utcnow()))

    @callback
    def _handle_due(self, now: datetime) -> None:
        self._timer_due = None
        self._cancel_timer = None
        was_on = self._detector.is_on
        self._settle(was_on, self._detector.update(self._excess(), now))

    @callback
    def _settle(self, was_on: bool, due: datetime | None) -> None:
        self._schedule(due)
        if self._detector.is_on != was_on:
            self.async_write_ha_state()

    @callback
    def _schedule(self, due: datetime | None) -> None:
        """One timer for the pending switch; a values event every 2 s keeps the same one."""
        if due == self._timer_due:
            return
        self._unschedule()
        if due is not None:
            self._timer_due = due
            self._cancel_timer = async_track_point_in_utc_time(self.hass, self._handle_due, due)

    @callback
    def _unschedule(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = None
        self._timer_due = None
