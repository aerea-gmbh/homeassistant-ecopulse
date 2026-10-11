"""The HTTP client against the fake gateway: auth header, error mapping, the SSE stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.ecopulse.api import (
    EcoPulseApiClient,
    EcoPulseAuthError,
    EcoPulseConnectionError,
    EcoPulseNotEnabledError,
    EcoPulseRateLimitedError,
)

from .fake_gateway import FakeGateway


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def short_dead_timeout() -> Generator[None]:
    """The contract's 45 s "no byte = dead" shrunk to 0.3 s."""
    with patch("custom_components.ecopulse.api.STREAM_DEAD_AFTER_SECONDS", 0.3):
        yield


def _client(
    session: aiohttp.ClientSession, gateway: FakeGateway, token: str | None = None, **kwargs: str
) -> EcoPulseApiClient:
    return EcoPulseApiClient(session, gateway.host, gateway.port, token or gateway.token, **kwargs)


async def test_info_sends_the_token_as_bearer_header_only(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    info = await _client(session, gateway).async_get_info()

    assert info.gateway_id == "gw-7f3a9c"
    assert info.gateway_name == "Haus Nord"
    assert info.catalog_revision == "9c1e4b7a2f05d863"
    request = gateway.requests[-1]
    assert request.path == "/api/ha/v1/info"
    assert request.authorization == f"Bearer {gateway.token}"
    assert request.query == {}  # ADR-230 §2: a token in the query string is never sent


async def test_catalog_is_parsed_with_every_property(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    catalog = await _client(session, gateway).async_get_catalog()

    assert catalog.revision == "9c1e4b7a2f05d863"
    assert [d.id for d in catalog.devices] == [
        "3f1c2a54-8d0e-4b7a-9c61-2e5f4a7b8c90",
        "7d3f8e9b-1b2a-4fac-9d4e-6b8293a41526",
        "4a0c5b6e-8e9d-4c7f-8a1b-3e5f60718293",
    ]
    cell = catalog.devices[0].entities[2]
    assert cell.unique_id == "2d8a3f4e-6c7b-4a5d-8e9f-1c3d4e5f6071"
    assert cell.entity_category == "diagnostic"
    assert cell.enabled_by_default is False


@pytest.mark.parametrize("status", [401, 403])
async def test_401_and_403_are_auth_errors(
    session: aiohttp.ClientSession, gateway: FakeGateway, status: int
) -> None:
    if status == 401:
        client = _client(session, gateway, token="ecp_ha_wrong")
    else:
        gateway.forced_status = 403
        client = _client(session, gateway)

    with pytest.raises(EcoPulseAuthError):
        await client.async_get_info()


async def test_404_means_not_enabled(session: aiohttp.ClientSession, gateway: FakeGateway) -> None:
    gateway.enabled = False

    with pytest.raises(EcoPulseNotEnabledError):
        await _client(session, gateway).async_get_info()


async def test_429_carries_retry_after(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    gateway.rate_limited_for = 17

    with pytest.raises(EcoPulseRateLimitedError) as caught:
        await _client(session, gateway).async_get_info()

    assert caught.value.retry_after == 17


async def test_a_server_error_is_a_connection_error(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    gateway.forced_status = 500

    with pytest.raises(EcoPulseConnectionError):
        await _client(session, gateway).async_get_info()


async def test_a_closed_port_is_a_connection_error(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    port = gateway.port
    await gateway.close()

    with pytest.raises(EcoPulseConnectionError):
        await EcoPulseApiClient(session, "127.0.0.1", port, gateway.token).async_get_info()


async def test_stream_yields_the_snapshot_then_pushed_events(
    session: aiohttp.ClientSession, gateway: FakeGateway, contract: dict
) -> None:
    received = []

    async def consume() -> None:
        async for event in _client(session, gateway).async_stream():
            received.append(event)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await gateway.wait_for_stream(1)
    gateway.ping()
    gateway.push("values", contract["examples"]["stream"]["values"])
    await asyncio.wait_for(task, 5)

    assert [e.name for e in received] == ["snapshot", "values"]
    assert received[0].data == contract["examples"]["stream"]["snapshot"]
    assert received[1].data == contract["examples"]["stream"]["values"]
    assert gateway.requests[-1].authorization == f"Bearer {gateway.token}"


# Review r2 LOW-A: the client's own rule, behind the switcher's (which asks only with a pin): a
# stream in the clear never asks for commands, whatever the caller says.
async def test_a_stream_in_the_clear_never_asks_for_commands_even_when_told_to(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    async def consume() -> None:
        async for _event in _client(session, gateway).async_stream(commands=True):
            return

    await asyncio.wait_for(consume(), 5)

    assert gateway.requests[-1].path.endswith("/stream")
    assert gateway.requests[-1].query == {}


@pytest.mark.usefixtures("short_dead_timeout")
async def test_stream_silence_beyond_the_dead_timeout_raises(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    async def consume() -> None:
        async for _ in _client(session, gateway).async_stream():
            pass

    with pytest.raises(EcoPulseConnectionError):
        await asyncio.wait_for(consume(), 5)


@pytest.mark.usefixtures("short_dead_timeout")
async def test_pings_keep_a_quiet_stream_alive(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    # Positive counterpart to the silence test: the same short timeout does NOT fire while
    # the edge sends ": ping" heartbeats.
    stop = asyncio.Event()

    async def pinger() -> None:
        await gateway.wait_for_stream(1)
        for _ in range(6):
            await asyncio.sleep(0.1)
            gateway.ping()
        stop.set()
        gateway.drop_streams()

    async def consume() -> None:
        async for _ in _client(session, gateway).async_stream():
            pass

    ping_task = asyncio.create_task(pinger())
    await asyncio.wait_for(consume(), 5)  # ends normally when the server drops the stream
    await ping_task
    assert stop.is_set()


async def test_stream_401_is_an_auth_error(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    async def consume() -> None:
        async for _ in _client(session, gateway, token="ecp_ha_wrong").async_stream():
            pass

    with pytest.raises(EcoPulseAuthError):
        await consume()


async def test_a_malformed_catalog_is_a_connection_error_not_a_crash(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    del gateway.catalog["gateway"]

    with pytest.raises(EcoPulseConnectionError):
        await _client(session, gateway).async_get_catalog()


async def test_a_redirect_is_not_followed(
    session: aiohttp.ClientSession, gateway: FakeGateway
) -> None:
    # The token must only ever reach the gateway it was typed in for.
    gateway.redirect_to = "/elsewhere"

    with pytest.raises(EcoPulseConnectionError):
        await _client(session, gateway).async_get_info()

    assert [r.path for r in gateway.requests] == ["/api/ha/v1/info"]
