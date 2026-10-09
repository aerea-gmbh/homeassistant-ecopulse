"""Shared fixtures: the fake gateway, a config entry pointing at it, fast backoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecopulse.const import DOMAIN

from .fake_gateway import FakeGateway, load_contract


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/ecopulse in every test."""


@pytest.fixture(autouse=True)
def allow_loopback_sockets(socket_enabled: None) -> None:
    """The fake gateway is a real HTTP server on 127.0.0.1 (still the only allowed host)."""


@pytest.fixture
def contract() -> dict[str, Any]:
    return load_contract()


@pytest.fixture
async def gateway() -> AsyncGenerator[FakeGateway]:
    fake = FakeGateway()
    await fake.start()
    yield fake
    await fake.close()


@pytest.fixture
def fast_backoff() -> Generator[None]:
    """Shrink the contract's seconds so reconnect tests run in milliseconds."""
    with (
        patch("custom_components.ecopulse.coordinator.BACKOFF_INITIAL_SECONDS", 0.01),
        patch("custom_components.ecopulse.coordinator.BACKOFF_MAX_SECONDS", 0.05),
        patch("custom_components.ecopulse.coordinator.RETRY_LATER_SECONDS", 0.05),
    ):
        yield


@pytest.fixture
def config_entry(gateway: FakeGateway) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=gateway.info["gatewayName"],
        unique_id=gateway.info["gatewayId"],
        data={CONF_HOST: gateway.host, CONF_PORT: gateway.port, CONF_TOKEN: gateway.token},
    )


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, gateway: FakeGateway, fast_backoff: None
) -> MockConfigEntry:
    """Set up the entry and wait until the first snapshot has been applied."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await gateway.wait_for_stream(1)
    await wait_until(hass, lambda: config_entry.runtime_data.coordinator.connected)
    return config_entry


async def wait_until(hass: HomeAssistant, predicate: Any, timeout: float = 5) -> None:
    """Let the event loop run until ``predicate()`` holds (stream I/O is real, not mocked)."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()
