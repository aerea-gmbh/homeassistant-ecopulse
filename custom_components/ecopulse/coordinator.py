"""Push coordinator: keeps one SSE stream to the gateway open and fans values out to entities.

Client behaviour per ADR-230 §4/§6 and the contract's ``client`` block:
- snapshot = full state; ``values`` batches update, ``availability`` flips devices;
- a snapshot or ``catalog`` event with a revision other than the loaded catalog's -> refetch
  ``/catalog`` and reload the entry (only if the refetched revision really differs, so an edge
  that is briefly inconsistent cannot drive a reload loop);
- unknown value ids are ignored;
- 401/403 -> reauth, no further reconnects; 404 -> retry after 300 s; 429 -> Retry-After;
  a drop -> backoff from 1 s doubling to 60 s, reset by a snapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .api import (
    EcoPulseApiClient,
    EcoPulseAuthError,
    EcoPulseConnectionError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
)
from .const import (
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    EVENT_AVAILABILITY,
    EVENT_CATALOG,
    EVENT_SNAPSHOT,
    EVENT_VALUES,
    RETRY_LATER_SECONDS,
)
from .models import Catalog
from .sse import SseEvent

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalValue:
    """A value as the edge sent it; ``ts`` is None exactly when it was never read.

    ``attributes`` (schema 3, #626) come complete with every value of an entity that has any
    (contract semantics ``replace``); None when the edge sent none.
    """

    value: Any
    ts: str | None
    attributes: dict[str, Any] | None = None


def next_backoff(delay: float) -> float:
    return min(delay * 2, BACKOFF_MAX_SECONDS)


def wait_after_failure(error: Exception | None, backoff: float) -> float:
    """Seconds to wait before the next connection attempt (``None`` = the stream just ended)."""
    if isinstance(error, EcoPulseNotEnabledError):
        return RETRY_LATER_SECONDS
    if isinstance(error, EcoPulseRateLimitedError) and error.retry_after is not None:
        return error.retry_after
    return backoff


def _list(value: Any) -> list[Any]:
    """A malformed event property is ignored instead of ending the stream."""
    return value if isinstance(value, list) else []


class _ReloadScheduled(Exception):
    """The catalog changed; the entry reloads and this stream must end."""


class EcoPulseStreamCoordinator:
    """Owns the stream task, the latest values and device availability."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EcoPulseApiClient,
        catalog: Catalog,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._client = client
        self.catalog = catalog
        self._device_of = catalog.device_of()
        # Gateway entities take values like signals but belong to no device (#623).
        self._value_ids = set(self._device_of) | {e.unique_id for e in catalog.gateway_entities}
        self.values: dict[str, SignalValue] = {}
        self._device_available: dict[str, bool] = {}
        self.connected = False
        self.stream_task: asyncio.Task[None] | None = None
        self._listeners: dict[str, list[Callable[[], None]]] = {}

    # -- entity side -----------------------------------------------------------------------------

    def value(self, unique_id: str) -> SignalValue | None:
        return self.values.get(unique_id)

    def is_device_available(self, device_id: str) -> bool:
        # The snapshot lists every device; one it leaves out is not reported unavailable.
        return self.connected and self._device_available.get(device_id, True)

    @callback
    def async_add_listener(self, unique_id: str, update: Callable[[], None]) -> CALLBACK_TYPE:
        listeners = self._listeners.setdefault(unique_id, [])
        listeners.append(update)

        @callback
        def remove() -> None:
            listeners.remove(update)

        return remove

    # -- stream ----------------------------------------------------------------------------------

    @callback
    def async_start(self, initial_wait: float = 0) -> None:
        """Start the stream; ``initial_wait`` honours a 404/429 seen during setup."""
        self.stream_task = self._entry.async_create_background_task(
            self.hass,
            self._async_run(initial_wait),
            f"{self._entry.domain} stream {self.catalog.gateway_id}",
        )

    async def _async_run(self, initial_wait: float) -> None:
        try:
            if initial_wait:
                await asyncio.sleep(initial_wait)
            await self._async_stream_forever()
        finally:
            # Whatever ends the task (reauth, reload, unload, a bug), its entities must not
            # keep showing the last values as available (ADR-230 §7).
            self._set_connected(False)

    async def _async_stream_forever(self) -> None:
        backoff = float(BACKOFF_INITIAL_SECONDS)
        while True:
            error: Exception | None = None
            try:
                async with aclosing(self._client.async_stream()) as events:
                    async for event in events:
                        if await self._async_handle(event):
                            backoff = float(BACKOFF_INITIAL_SECONDS)
            except EcoPulseAuthError:
                _LOGGER.warning("The gateway rejected the token; reauthentication required")
                self._entry.async_start_reauth(self.hass)
                return
            except _ReloadScheduled:
                return
            except (
                EcoPulseConnectionError,
                EcoPulseNotEnabledError,
                EcoPulseRateLimitedError,
            ) as err:
                error = err
            except Exception as err:
                # Best effort by design: a bug in handling one event must not end the stream
                # for good; log it, go unavailable and resync from a fresh snapshot.
                _LOGGER.exception("Unexpected error in the EcoPulse stream, reconnecting")
                error = err
            self._set_connected(False)
            wait = wait_after_failure(error, backoff)
            _LOGGER.debug("Stream down (%s), reconnecting in %.1f s", type(error).__name__, wait)
            await asyncio.sleep(wait)
            backoff = next_backoff(backoff)

    async def _async_handle(self, event: SseEvent) -> bool:
        """Apply one event; True when it was a snapshot (which resets the backoff)."""
        if event.name == EVENT_SNAPSHOT:
            await self._async_ensure_catalog(event.data.get("catalogRevision"))
            self._apply_snapshot(event.data)
            return True
        if event.name == EVENT_VALUES:
            self._apply_values(_list(event.data.get("values")))
        elif event.name == EVENT_AVAILABILITY:
            self._apply_availability(_list(event.data.get("devices")))
        elif event.name == EVENT_CATALOG:
            await self._async_ensure_catalog(event.data.get("catalogRevision"))
        # Unknown event types are ignored (additive within v1, ADR-230 §10).
        return False

    async def _async_ensure_catalog(self, revision: Any) -> None:
        if revision == self.catalog.revision:
            return
        catalog = await self._client.async_get_catalog()
        if catalog.revision == self.catalog.revision:
            _LOGGER.debug(
                "Stream announced revision %s, /catalog still serves the loaded one", revision
            )
            return
        _LOGGER.info(
            "Catalog changed (%s -> %s), reloading", self.catalog.revision, catalog.revision
        )
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
        raise _ReloadScheduled

    @callback
    def _apply_snapshot(self, data: dict[str, Any]) -> None:
        self.values = {}
        self._device_available = {}
        self._store_availability(_list(data.get("devices")))
        self._store_values(_list(data.get("values")))
        self.connected = True
        self._notify(self._listeners)

    @callback
    def _apply_values(self, values: Any) -> None:
        self._notify(self._store_values(values))

    @callback
    def _apply_availability(self, devices: Any) -> None:
        changed = self._store_availability(devices)
        self._notify(uid for uid, device_id in self._device_of.items() if device_id in changed)

    def _store_values(self, values: Any) -> list[str]:
        stored = []
        for item in values:
            unique_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(unique_id, str) or unique_id not in self._value_ids:
                continue  # contract onUnknownValueId: ignore, never create an entity
            # Tolerant receiver: unknown properties are ignored, malformed attributes too.
            attributes = item.get("attributes")
            self.values[unique_id] = SignalValue(
                item.get("value"),
                item.get("ts"),
                attributes if isinstance(attributes, dict) else None,
            )
            stored.append(unique_id)
        return stored

    def _store_availability(self, devices: Any) -> set[str]:
        changed = set()
        for item in devices:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self._device_available[item["id"]] = bool(item.get("available"))
                changed.add(item["id"])
        return changed

    @callback
    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            self._notify(self._listeners)

    @callback
    def _notify(self, unique_ids: Any) -> None:
        for unique_id in list(unique_ids):
            for update in list(self._listeners.get(unique_id, ())):
                update()
