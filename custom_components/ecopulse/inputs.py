"""Values from Home Assistant into EcoPulse (schema 5, #624, ADR-232 S5).

The gateway's catalog lists its input slots; the options map a Home Assistant entity to each.
This forwarder posts the mapped states to ``POST /inputs``: on every state change (coalesced, so
a chatty sensor cannot exceed the gateway's per-token rate) and every ``INPUTS_REPOST_SECONDS``
(the contract's re-post: the gateway stamps its own receive time and counts a slot silent for its
``staleAfterSeconds`` as missing, so a constant value must be sent again).

- Units: converted into the slot's unit with Home Assistant's own unit converters; a unit that no
  converter turns into the slot's is never posted, and a repair issue names the entity.
- ``unavailable``/``unknown`` is posted as null: the gateway takes no sample, the slot goes stale.
- Only over the pinned TLS connection: the scope works only there, so a plain-HTTP entry posts
  nothing and says so in a repair issue.
- A 403 (the toggle in the EcoPulse web app is off, contract ``client.routeErrors``) disables the
  forwarder and raises a repair issue; reading goes on and the entry is not reauthenticated. A
  reload of the entry (e.g. after saving the options) tries again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
import inspect
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_OFF, STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import unit_conversion

from .api import (
    EcoPulseApiClient,
    EcoPulseConnectionError,
    EcoPulseError,
    EcoPulseInputsRefusedError,
    EcoPulseInvalidRequestError,
    EcoPulseRateLimitedError,
)
from .const import (
    CONF_INPUTS,
    DOMAIN,
    INPUT_DATA_TYPE_BOOLEAN,
    INPUTS_MAX_PER_REQUEST,
    INPUTS_REPOST_SECONDS,
    ISSUE_INPUT_LOOP,
    ISSUE_INPUT_UNIT,
    ISSUE_INPUTS_NEED_TLS,
    ISSUE_INPUTS_REFUSED,
)
from .models import CatalogInput

_LOGGER = logging.getLogger(__name__)

# State changes within this window go out as one post (the gateway allows 5 posts/s per token).
CHANGE_FLUSH_DELAY_SECONDS = 0.5

_CONVERTERS: tuple[type[unit_conversion.BaseUnitConverter], ...] = tuple(
    converter
    for _, converter in inspect.getmembers(unit_conversion, inspect.isclass)
    if issubclass(converter, unit_conversion.BaseUnitConverter)
    and converter is not unit_conversion.BaseUnitConverter
)


@dataclass(frozen=True, slots=True)
class Converted:
    """A state as the slot takes it: the value (None = Home Assistant has none), or ``unit_error``
    when the entity's unit cannot become the slot's (then nothing is posted for it)."""

    value: float | bool | None
    unit_error: str | None = None


def convert(state: State | None, slot: CatalogInput) -> Converted:
    """The state of a mapped entity in the slot's type and unit. Pure, so it is unit-tested."""
    if state is None or state.state in ("unavailable", "unknown", ""):
        return Converted(None)
    if slot.data_type == INPUT_DATA_TYPE_BOOLEAN:
        return Converted({STATE_ON: True, STATE_OFF: False}.get(state.state))
    try:
        number = float(state.state)
    except ValueError:
        return Converted(None)
    if not math.isfinite(number):
        # Review r1 LOW-2: "nan"/"inf" parse; neither is a value, and NaN on the wire would fail
        # the whole batch.
        return Converted(None)
    return _in_unit(number, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT), slot.unit)


def _in_unit(number: float, source_unit: str | None, slot_unit: str | None) -> Converted:
    # An entity without a unit (an input_number) is taken in the slot's unit: the user mapped it.
    if source_unit is None or source_unit == slot_unit:
        return Converted(number)
    if slot_unit is None:
        return Converted(None, source_unit)
    for converter in _CONVERTERS:
        if source_unit in converter.VALID_UNITS and slot_unit in converter.VALID_UNITS:
            converted = converter.convert(number, source_unit, slot_unit)
            return Converted(converted if math.isfinite(converted) else None)
    return Converted(None, source_unit)


def ecopulse_entity_ids(hass: HomeAssistant) -> set[str]:
    """Every EcoPulse entity, of any gateway's entry: feeding EcoPulse its own value is a loop."""
    return {
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
    }


def mapping_of(options: Mapping[str, Any], slots: tuple[CatalogInput, ...]) -> dict[str, str]:
    """The options' mapping, slot key -> entity id, for the slots the catalog still lists."""
    by_unique_id = {slot.unique_id: slot.key for slot in slots}
    mapped = options.get(CONF_INPUTS) or {}
    return {
        by_unique_id[unique_id]: entity_id
        for unique_id, entity_id in mapped.items()
        if unique_id in by_unique_id and entity_id
    }


class EcoPulseInputForwarder:
    """Posts the mapped entities' states to the gateway while the entry is loaded."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EcoPulseApiClient,
        slots: tuple[CatalogInput, ...],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._client = client
        self._slots = {slot.key: slot for slot in slots}
        self._mapping = mapping_of(entry.options, slots)
        self._pending: set[str] = set()
        self._cancel_flush: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        self._disabled = False
        self._unit_issues: set[str] = set()
        self._reported_rejections: set[tuple[str, str]] = set()

    @property
    def disabled(self) -> bool:
        return self._disabled

    @callback
    def async_start(self) -> None:
        """Subscribe and post every mapped value once; nothing at all without a mapping."""
        # A changed mapping needs new subscriptions: reload. Other options (the surplus sensor's)
        # apply live and leave the stream alone.
        self._entry.async_on_unload(self._entry.add_update_listener(self._async_options_updated))
        self._entry.async_on_unload(self._async_stop)
        self._drop_ecopulse_entities()
        if not self._mapping:
            self._delete_issue(ISSUE_INPUTS_NEED_TLS)
            return
        if not self._client.pinned:
            # The scope works only over TLS; never send the token's values in the clear.
            self._create_issue(ISSUE_INPUTS_NEED_TLS, {})
            return
        self._delete_issue(ISSUE_INPUTS_NEED_TLS)
        self._entry.async_on_unload(
            async_track_state_change_event(
                self.hass, list(set(self._mapping.values())), self._async_state_changed
            )
        )
        self._entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_repost, timedelta(seconds=INPUTS_REPOST_SECONDS)
            )
        )
        self._schedule(set(self._mapping))

    @callback
    def _drop_ecopulse_entities(self) -> None:
        """Review r1 LOW-5: the options flow offers no EcoPulse entity, but a mapping may predate
        it or be edited by hand."""
        own = ecopulse_entity_ids(self.hass)
        looped = sorted({entity_id for entity_id in self._mapping.values() if entity_id in own})
        self._mapping = {
            key: entity_id for key, entity_id in self._mapping.items() if entity_id not in own
        }
        if looped:
            self._create_issue(ISSUE_INPUT_LOOP, {"entities": ", ".join(looped)})
        else:
            self._delete_issue(ISSUE_INPUT_LOOP)

    async def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if mapping_of(entry.options, tuple(self._slots.values())) != self._mapping:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _async_stop(self) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        self._schedule({key for key, mapped in self._mapping.items() if mapped == entity_id})

    @callback
    def _async_repost(self, _now: Any) -> None:
        self._schedule(set(self._mapping))

    @callback
    def _schedule(self, keys: set[str]) -> None:
        if self._disabled or not keys:
            return
        self._pending |= keys
        if self._cancel_flush is None:
            self._cancel_flush = async_call_later(
                self.hass, CHANGE_FLUSH_DELAY_SECONDS, self._async_flush_later
            )

    @callback
    def _async_flush_later(self, _now: Any) -> None:
        self._cancel_flush = None
        self._entry.async_create_background_task(
            self.hass, self._async_flush(), f"{DOMAIN} inputs {self._entry.entry_id}"
        )

    async def _async_flush(self) -> None:
        async with self._lock:
            keys, self._pending = sorted(self._pending), set()
            items = self._items(keys)
            for start in range(0, len(items), INPUTS_MAX_PER_REQUEST):
                if self._disabled:
                    return
                await self._async_post(items[start : start + INPUTS_MAX_PER_REQUEST])

    def _items(self, keys: list[str]) -> list[dict[str, Any]]:
        items = []
        for key in keys:
            slot = self._slots[key]
            entity_id = self._mapping[key]
            converted = convert(self.hass.states.get(entity_id), slot)
            self._report_unit(slot, entity_id, converted.unit_error)
            if converted.unit_error is not None:
                continue
            item: dict[str, Any] = {"key": key, "value": converted.value}
            if slot.unit is not None:
                item["unit"] = slot.unit
            items.append(item)
        return items

    async def _async_post(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        try:
            result = await self._client.async_post_inputs(items)
        except EcoPulseInputsRefusedError as err:
            self._disable(err.code)
            return
        except EcoPulseRateLimitedError:
            _LOGGER.debug("Gateway rate-limited the inputs; the next re-post sends them again")
            return
        except EcoPulseInvalidRequestError:
            _LOGGER.warning("The gateway could not read the inputs request; dropped")
            return
        except EcoPulseConnectionError as err:
            _LOGGER.debug("Posting inputs failed (%s); the next re-post retries", err)
            return
        except EcoPulseError as err:
            # The stream owns auth and certificate failures (it starts the reauth); here it is
            # enough not to post until then.
            _LOGGER.debug("Posting inputs failed (%s)", type(err).__name__)
            return
        self._delete_issue(ISSUE_INPUTS_REFUSED)
        for key, code in result.rejected:
            if (key, code) not in self._reported_rejections:
                self._reported_rejections.add((key, code))
                _LOGGER.warning("The gateway rejected input %s: %s", key, code)

    @callback
    def _disable(self, code: str | None) -> None:
        self._disabled = True
        self._pending.clear()
        _LOGGER.warning("The gateway refuses values from Home Assistant (%s); inputs stopped", code)
        self._create_issue(ISSUE_INPUTS_REFUSED, {"code": code or "403"})

    @callback
    def _report_unit(self, slot: CatalogInput, entity_id: str, unit: str | None) -> None:
        issue = f"{ISSUE_INPUT_UNIT}_{slot.unique_id}"
        if unit is None:
            if issue in self._unit_issues:
                self._unit_issues.discard(issue)
                ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self._entry.entry_id}")
            return
        if issue not in self._unit_issues:
            self._unit_issues.add(issue)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{issue}_{self._entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_INPUT_UNIT,
                translation_placeholders={
                    "entity": entity_id,
                    "unit": unit,
                    "slot": slot.name,
                    "slot_unit": slot.unit or "-",
                },
            )

    @callback
    def _create_issue(self, issue: str, placeholders: dict[str, str]) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{issue}_{self._entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            translation_placeholders={"name": self._entry.title, **placeholders},
        )

    @callback
    def _delete_issue(self, issue: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self._entry.entry_id}")
