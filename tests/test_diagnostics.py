"""Diagnostics never contain the token."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .fake_gateway import FakeGateway


async def test_diagnostics_redact_the_token_and_the_gateway_name(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    loaded_entry: MockConfigEntry,
    gateway: FakeGateway,
) -> None:
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, loaded_entry)

    dumped = json.dumps(diagnostics)
    assert gateway.token not in dumped
    assert "Haus Nord" not in dumped
    assert diagnostics["entry"]["data"]["token"] == REDACTED
    assert diagnostics["catalog"]["revision"] == "9c1e4b7a2f05d863"
    assert diagnostics["catalog"]["entity_count"] == 6
    assert diagnostics["stream"]["connected"] is True
    assert diagnostics["stream"]["values_known"] == 6
