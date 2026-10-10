"""EcoPulse switches Home Assistant loads (schema 6, #625, ADR-232 S6).

The gateway's catalog lists the loads EcoPulse may switch (a heating rod, a pool pump: devices on
its Home Assistant driver, switched by the PV heater assistant like relays); the options map a
Home Assistant entity to each. The gateway sends a ``command`` event on the stream; this switcher
calls the mapped entity's ``turn_on``/``turn_off`` (blocking, so the state is the result) and
acknowledges with ``POST /commands/{commandId}/ack``: ``ok``, the entity's state after the call
and, when it failed, why. The gateway's apply fails without an ack within its timeout, so a load
nobody mapped is answered at once with ``ok: false``, and so is an entity Home Assistant does not
have or reports unavailable (it completes such a call without switching anything).

- One command per entity at a time, and of those waiting only the latest runs (the others are
  answered as replaced): a slow ON never finishes after a later OFF. A call still running when the
  ack is due is answered as failed and keeps the entity until it ends.
- The stream asks for commands only while this switcher handles them (``?commands=1``): the
  gateway sends none to a client that cannot answer.

- Only over the pinned TLS connection: the coordinator drops a command from any other stream, and
  without a pin no handler is registered at all (a repair issue says why).
- No lease (PO 10.10.): the entity keeps its state when the gateway or the stream goes away; the
  gateway sends its command again after the reconnect.
- A 403 on the ack (the toggle in the EcoPulse web app is off) stops switching and raises a repair
  issue; reading goes on and the entry is not reauthenticated. A reload tries again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .api import (
    EcoPulseApiClient,
    EcoPulseError,
    EcoPulseLoadsRefusedError,
    EcoPulseUnknownCommandError,
)
from .const import (
    ACK_BODY_MAX_BYTES,
    ACK_MAX_ERROR_LENGTH,
    ACK_TIMEOUT_SECONDS,
    COMMAND_ID_PATTERN,
    CONF_LOADS,
    DOMAIN,
    ISSUE_LOAD_LOOP,
    ISSUE_LOADS_NEED_TLS,
    ISSUE_LOADS_REFUSED,
    LOAD_ACTION_OFF,
    LOAD_ACTION_ON,
)
from .coordinator import EcoPulseStreamCoordinator
from .inputs import ecopulse_entity_ids
from .models import CatalogLoad

_LOGGER = logging.getLogger(__name__)

NOT_MAPPED = "No Home Assistant entity is mapped to this load."
SUPERSEDED = "A later command for this load replaced this one before it ran."

# A call still running then is answered as failed: the ack reaches the gateway within its timeout.
SERVICE_TIMEOUT_SECONDS: float = ACK_TIMEOUT_SECONDS - 2

# What the error may take of the ack body; the rest is the envelope ({"ok":false,"state":"off"}).
_ERROR_MAX_BYTES = ACK_BODY_MAX_BYTES - 128

_SERVICE_OF = {LOAD_ACTION_ON: "turn_on", LOAD_ACTION_OFF: "turn_off"}

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def mapping_of(options: Mapping[str, Any], loads: tuple[CatalogLoad, ...]) -> dict[str, str]:
    """The options' mapping, load unique id -> entity id, for the loads the catalog still lists."""
    listed = {load.unique_id for load in loads}
    mapped = options.get(CONF_LOADS) or {}
    return {
        unique_id: entity_id
        for unique_id, entity_id in mapped.items()
        if unique_id in listed and entity_id
    }


def reported_state(hass: HomeAssistant, entity_id: str) -> str | None:
    """The entity's on/off state as the ack reports it; None for anything else (unavailable)."""
    state = hass.states.get(entity_id)
    return state.state if state is not None and state.state in (STATE_ON, STATE_OFF) else None


def error_text(err: Exception | str) -> str:
    """Why a switch failed, for the gateway's report: one plain line, never empty, at most the
    contract length (a device's message may carry line breaks or terminal escapes)."""
    raw = err if isinstance(err, str) else (str(err) or type(err).__name__)
    text = _CONTROL_CHARACTERS.sub(" ", raw)[:ACK_MAX_ERROR_LENGTH]
    # 256 characters may be more bytes than the gateway reads (an emoji is four): the body must fit.
    while len(text.encode()) > _ERROR_MAX_BYTES:
        text = text[:-1]
    return text


class EcoPulseLoadSwitcher:
    """Switches the mapped entities on the gateway's commands while the entry is loaded."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EcoPulseApiClient,
        coordinator: EcoPulseStreamCoordinator,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._client = client
        self._coordinator = coordinator
        self._loads = coordinator.catalog.loads
        # The mapping as configured (to tell a changed one on an options update) and as used.
        self._configured = mapping_of(entry.options, self._loads)
        self._mapping = dict(self._configured)
        self._disabled = False
        # Review r1 LOW-1: one command per entity at a time; of those waiting, only the latest.
        self._entity_locks: dict[str, asyncio.Lock] = {}
        self._waiting: dict[str, str] = {}

    @property
    def disabled(self) -> bool:
        return self._disabled

    @callback
    def async_start(self) -> None:
        """Take commands, but only over the pinned TLS connection; nothing without loads."""
        self._entry.async_on_unload(self._entry.add_update_listener(self._async_options_updated))
        self._drop_ecopulse_entities()
        if not self._loads:
            self._delete_issue(ISSUE_LOADS_NEED_TLS)
            return
        if not self._client.pinned:
            # The scope works only over TLS; a command in the clear could be anyone's.
            if self._mapping:
                self._create_issue(ISSUE_LOADS_NEED_TLS, {})
            return
        self._delete_issue(ISSUE_LOADS_NEED_TLS)
        self._entry.async_on_unload(
            self._coordinator.async_set_command_handler(self._async_command)
        )

    @callback
    def _drop_ecopulse_entities(self) -> None:
        """The options flow offers no EcoPulse entity; a mapping edited by hand may still name one,
        and switching EcoPulse's own read-only entities is no load."""
        own = ecopulse_entity_ids(self.hass)
        looped = sorted({entity_id for entity_id in self._mapping.values() if entity_id in own})
        self._mapping = {
            unique_id: entity_id
            for unique_id, entity_id in self._mapping.items()
            if entity_id not in own
        }
        if looped:
            self._create_issue(ISSUE_LOAD_LOOP, {"entities": ", ".join(looped)})
        else:
            self._delete_issue(ISSUE_LOAD_LOOP)

    async def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if mapping_of(entry.options, self._loads) != self._configured:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _async_command(self, data: dict[str, Any]) -> None:
        command_id = data.get("commandId")
        action = data.get("action")
        load = data.get("load")
        if not isinstance(command_id, str) or not re.fullmatch(COMMAND_ID_PATTERN, command_id):
            _LOGGER.debug("Ignoring a command without a valid id")
            return
        if self._disabled:
            return
        self._entry.async_create_background_task(
            self.hass,
            self._async_execute(command_id, action, load),
            f"{DOMAIN} command {command_id}",
        )

    async def _async_execute(self, command_id: str, action: Any, load: Any) -> None:
        entity_id = self._mapping.get(load) if isinstance(load, str) else None
        service = _SERVICE_OF.get(action) if isinstance(action, str) else None
        if service is None:
            await self._async_ack(command_id, False, None, f"Unknown action {action!r}.")
            return
        if entity_id is None:
            await self._async_ack(command_id, False, None, NOT_MAPPED)
            return
        if (replaced := self._waiting.get(entity_id)) is not None:
            await self._async_ack(replaced, False, None, SUPERSEDED)
        self._waiting[entity_id] = command_id
        async with self._entity_locks.setdefault(entity_id, asyncio.Lock()):
            if self._waiting.get(entity_id) != command_id:
                return  # a later command replaced it while it waited, and answered it
            del self._waiting[entity_id]
            await self._async_switch(command_id, entity_id, service)

    async def _async_switch(self, command_id: str, entity_id: str, service: str) -> None:
        """Call the service, acknowledge the outcome, and hold the entity until the call ended.

        A call still running after :data:`SERVICE_TIMEOUT_SECONDS` is answered as failed, but the
        entity stays taken until it ends: a later OFF runs after a slow ON, never beside it.
        """
        call = self.hass.async_create_background_task(
            self.hass.services.async_call(
                entity_id.split(".", 1)[0], service, {"entity_id": entity_id}, blocking=True
            ),
            f"{DOMAIN} {service} {entity_id}",
        )
        done, _ = await asyncio.wait({call}, timeout=SERVICE_TIMEOUT_SECONDS)
        if not done:
            await self._async_ack(
                command_id,
                False,
                None,
                f"{entity_id} did not finish {service} within {SERVICE_TIMEOUT_SECONDS:g} s.",
            )
            with suppress(HomeAssistantError, vol.Invalid):
                await call
            return
        try:
            call.result()
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.debug("Switching %s %s failed: %s", entity_id, service, err)
            await self._async_ack(
                command_id, False, reported_state(self.hass, entity_id), error_text(err)
            )
            return
        state = reported_state(self.hass, entity_id)
        if state is None:
            # Review r1 MED-2: Home Assistant completes a call that skipped a missing or
            # unavailable entity; that switched nothing.
            await self._async_ack(
                command_id, False, None, error_text(f"{entity_id} is missing or unavailable.")
            )
            return
        _LOGGER.debug("Switched %s %s for EcoPulse", entity_id, service)
        await self._async_ack(command_id, True, state)

    async def _async_ack(
        self, command_id: str, ok: bool, state: str | None, error: str | None = None
    ) -> None:
        try:
            await self._client.async_ack_command(command_id, ok, state, error)
        except EcoPulseLoadsRefusedError as err:
            self._disable(err.code)
        except EcoPulseUnknownCommandError:
            _LOGGER.debug("Command %s was answered already or timed out", command_id)
        except EcoPulseError as err:
            # The gateway times the command out and its assistant retries on the next tick.
            _LOGGER.debug("Acknowledging command %s failed (%s)", command_id, type(err).__name__)

    @callback
    def _disable(self, code: str | None) -> None:
        self._disabled = True
        _LOGGER.warning("The gateway refuses switching loads (%s); commands stopped", code)
        self._create_issue(ISSUE_LOADS_REFUSED, {"code": code or "403"})

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
