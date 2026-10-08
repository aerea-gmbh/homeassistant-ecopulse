"""HTTP client for the gateway's local Home Assistant API (ADR-230 §4, §6).

The token is only ever sent as ``Authorization: Bearer``; it never goes into a URL, a log line
or an exception message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
from yarl import URL

from .const import API_BASE_PATH, REQUEST_TIMEOUT_SECONDS, STREAM_DEAD_AFTER_SECONDS
from .models import Catalog, GatewayInfo
from .sse import SseEvent, SseParser, SseProtocolError


class EcoPulseError(Exception):
    """Base error of the EcoPulse API client."""


class EcoPulseConnectionError(EcoPulseError):
    """The gateway is unreachable, timed out, went silent or answered unexpectedly."""


class EcoPulseAuthError(EcoPulseError):
    """401/403: the token is wrong, rotated or lacks the scope (contract action ``reauth``)."""


class EcoPulseNotEnabledError(EcoPulseError):
    """404: Home Assistant is not enabled on this gateway, or its firmware is too old
    (contract action ``retry-later``)."""


class EcoPulseRateLimitedError(EcoPulseError):
    """429: the gateway's lockout holds this client (contract action ``honour-retry-after``)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("rate limited by the gateway")
        self.retry_after = retry_after


class EcoPulseApiClient:
    """One gateway, one token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        *,
        stream_dead_after: float = STREAM_DEAD_AFTER_SECONDS,
    ) -> None:
        self._session = session
        self._base = URL.build(scheme="http", host=host, port=port, path=API_BASE_PATH)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._stream_dead_after = stream_dead_after

    @property
    def base_url(self) -> str:
        return str(self._base)

    async def async_get_info(self) -> GatewayInfo:
        return _parse("info", GatewayInfo.from_dict, await self._get_json("info"))

    async def async_get_catalog(self) -> Catalog:
        return _parse("catalog", Catalog.from_dict, await self._get_json("catalog"))

    async def async_stream(self) -> AsyncIterator[SseEvent]:
        """Yield events until the stream ends; raise on silence, errors or auth failure.

        ``sock_read`` makes aiohttp fail a read that saw no byte for the dead timeout, which is
        exactly the contract's "no byte for 45 s = dead" (pings count as bytes).
        """
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=REQUEST_TIMEOUT_SECONDS, sock_read=self._stream_dead_after
        )
        parser = SseParser()
        try:
            async with self._session.get(
                self._base / "stream",
                headers={**self._headers, "Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                _raise_for_status(response)
                async for chunk in response.content.iter_any():
                    for event in parser.feed(chunk):
                        yield event
        except (aiohttp.ClientError, TimeoutError, SseProtocolError) as err:
            raise EcoPulseConnectionError(f"stream failed: {type(err).__name__}") from err

    async def _get_json(self, path: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.get(
                self._base / path, headers=self._headers, timeout=timeout
            ) as response:
                _raise_for_status(response)
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EcoPulseConnectionError(f"GET {path} failed: {type(err).__name__}") from err
        if not isinstance(data, dict):
            raise EcoPulseConnectionError(f"GET {path} returned no JSON object")
        return data


def _parse[T](path: str, parse: Callable[[dict[str, Any]], T], data: dict[str, Any]) -> T:
    try:
        return parse(data)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise EcoPulseConnectionError(f"GET {path} returned a malformed body") from err


def _raise_for_status(response: aiohttp.ClientResponse) -> None:
    status = response.status
    if status == 200:
        return
    if status in (401, 403):
        raise EcoPulseAuthError(f"gateway answered {status}")
    if status == 404:
        raise EcoPulseNotEnabledError("Home Assistant API not enabled on the gateway")
    if status == 429:
        raise EcoPulseRateLimitedError(parse_retry_after(response.headers.get("Retry-After")))
    raise EcoPulseConnectionError(f"gateway answered {status}")


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date), None if unusable."""
    if not value:
        return None
    try:
        return max(0.0, float(int(value.strip())))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


__all__ = [
    "EcoPulseApiClient",
    "EcoPulseAuthError",
    "EcoPulseConnectionError",
    "EcoPulseError",
    "EcoPulseNotEnabledError",
    "EcoPulseRateLimitedError",
    "parse_retry_after",
]
