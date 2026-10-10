"""Home Assistant controls EcoPulse's assistants (schema 7, #627, ADR-232 S8).

The catalog lists ``controls``: an assistant's mode (a select), its enabled state (a switch) and its
battery-support start and stop SoC (two numbers). They become entities only when both hold:

- the token has the ``control`` scope (``/info.scopes``; the plant's opt-in in the EcoPulse web
  app), and
- the entry is pinned to the gateway's TLS certificate: the scope works only over TLS, and a
  control is never sent in the clear (the client refuses it without the pin).

A control's state is the stream value its ``stateValueId`` names; an action posts
``{action, assistantId, value}`` to ``POST /control`` and shows the 202's state until the stream
sends the next one. The gateway checks every value (options, bounds, the plant's minimum SoC) and
the cloud adopts what it applied. A refusal is an error of that action, never retried. A 403 (the
opt-in went off) disables control: the entities go unavailable and a repair issue says why; reading
goes on and the entry is not reauthenticated. A reload tries again.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .api import (
    EcoPulseApiClient,
    EcoPulseControlForbiddenError,
    EcoPulseControlNeedsTlsError,
    EcoPulseControlRefusedError,
    EcoPulseError,
    EcoPulseInvalidRequestError,
    EcoPulseRateLimitedError,
    EcoPulseUnavailableError,
)
from .const import DOMAIN, ISSUE_CONTROL_REFUSED, SCOPE_CONTROL
from .coordinator import EcoPulseStreamCoordinator
from .models import CatalogControl

_LOGGER = logging.getLogger(__name__)


async def async_served_controls(
    client: EcoPulseApiClient, controls: tuple[CatalogControl, ...]
) -> tuple[CatalogControl, ...]:
    """The catalog's controls this entry offers: none without the pin or without the scope.

    ``/info`` is asked only over the pinned connection; without the pin there is nothing to ask.
    """
    if not controls or not client.pinned:
        return ()
    try:
        info = await client.async_get_info()
    except EcoPulseError as err:
        # The stream reports and handles the gateway's state; without the scope's word, no control.
        _LOGGER.debug("No control entities: /info failed (%s)", type(err).__name__)
        return ()
    return controls if SCOPE_CONTROL in info.scopes else ()


class EcoPulseControls:
    """Posts the control entities' actions while the entry is loaded; disabled by a 403."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EcoPulseApiClient,
        coordinator: EcoPulseStreamCoordinator,
        controls: tuple[CatalogControl, ...],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._client = client
        self._coordinator = coordinator
        self.controls = controls
        self.disabled = False

    def for_platform(self, platform: str) -> list[CatalogControl]:
        return [c for c in self.controls if c.platform == platform]

    @callback
    def async_start(self) -> None:
        # A reload with the scope back (or without any control) clears an earlier refusal.
        self._delete_issue()

    async def async_set(self, control: CatalogControl, value: Any) -> None:
        """Post one action; raise :class:`HomeAssistantError` when it was not applied."""
        if self.disabled:
            raise _error("control_disabled", {})
        try:
            result = await self._client.async_post_control(
                control.action, control.assistant_id, value
            )
        except EcoPulseControlForbiddenError as err:
            self._disable(err.code)
            raise _error("control_disabled", {}) from err
        except EcoPulseControlRefusedError as err:
            raise _error("control_refused", {"code": err.code or "422"}) from err
        except EcoPulseUnavailableError as err:
            raise _error("control_unavailable", {}) from err
        except EcoPulseInvalidRequestError as err:
            raise _error("control_invalid", {}) from err
        except EcoPulseRateLimitedError as err:
            wait = "" if err.retry_after is None else f"{err.retry_after:g}"
            raise _error("control_rate_limited", {"seconds": wait or "?"}) from err
        except EcoPulseControlNeedsTlsError as err:
            raise _error("control_needs_tls", {}) from err
        except EcoPulseError as err:
            raise _error("control_failed", {"error": type(err).__name__}) from err
        _LOGGER.debug(
            "Control %s of %s: applied=%s", control.action, control.assistant_id, result.applied
        )
        self._coordinator.async_set_value(control.state_value_id, result.state)

    @callback
    def _disable(self, code: str | None) -> None:
        self.disabled = True
        _LOGGER.warning("The gateway refuses control from Home Assistant (%s); stopped", code)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CONTROL_REFUSED,
            translation_placeholders={"name": self._entry.title, "code": code or "403"},
        )
        # The entities go unavailable at once, not only with the next stream value.
        self._coordinator.async_notify_all()

    @callback
    def _delete_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_CONTROL_REFUSED}_{self._entry.entry_id}"


def _error(key: str, placeholders: dict[str, str]) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


class EcoPulseControlEntity(Entity):
    """One control on its assistant's device; its state is the stream value of ``stateValueId``."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: EcoPulseStreamCoordinator,
        controls: EcoPulseControls,
        control: CatalogControl,
    ) -> None:
        self.coordinator = coordinator
        self._controls = controls
        self._control = control
        self._attr_unique_id = control.unique_id
        self._attr_name = control.name
        self._attr_device_info = _device_info(coordinator, control)

    @property
    def available(self) -> bool:
        return not self._controls.disabled and self.coordinator.is_device_available(
            self._control.device_id
        )

    @property
    def _raw_value(self) -> Any:
        current = self.coordinator.value(self._control.state_value_id)
        return None if current is None else current.value

    async def _async_set(self, value: Any) -> None:
        await self._controls.async_set(self._control, value)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(
                self._control.state_value_id, self.async_write_ha_state
            )
        )


def _device_info(coordinator: EcoPulseStreamCoordinator, control: CatalogControl) -> DeviceInfo:
    """The assistant's device: the catalog's device with that id if it lists one (its outputs),
    else one of its own under the gateway."""
    listed = next((d for d in coordinator.catalog.devices if d.id == control.device_id), None)
    if listed is not None:
        return DeviceInfo(identifiers={(DOMAIN, listed.id)})
    return DeviceInfo(
        identifiers={(DOMAIN, control.device_id)},
        name=control.device_name,
        via_device=(DOMAIN, coordinator.catalog.gateway_id),
    )
