"""HTTP client for the gateway's local Home Assistant API (ADR-230 §4, §6).

The token is only ever sent as ``Authorization: Bearer``; it never goes into a URL, a log line
or an exception message.

With a pinned certificate fingerprint (#610 S8-TLS, ADR-232) every request goes over HTTPS and
aiohttp checks the SHA-256 of the gateway's certificate before the request is written: a
different certificate means no request at all, and a pinned client never speaks plain HTTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import re
import ssl
from typing import Any

import aiohttp
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from yarl import URL

from .const import (
    API_BASE_PATH,
    CONF_TLS_FINGERPRINT,
    CONF_TLS_PORT,
    FINGERPRINT_PATTERN,
    INPUTS_PATH,
    REQUEST_TIMEOUT_SECONDS,
    STREAM_DEAD_AFTER_SECONDS,
)
from .models import Catalog, GatewayInfo
from .sse import SseEvent, SseParser, SseProtocolError


class EcoPulseError(Exception):
    """Base error of the EcoPulse API client."""


class EcoPulseConnectionError(EcoPulseError):
    """The gateway is unreachable, timed out, went silent or answered unexpectedly."""


class EcoPulseAuthError(EcoPulseError):
    """401/403: the token is wrong, rotated or lacks the scope (contract action ``reauth``)."""


class EcoPulseCertificateMismatchError(EcoPulseAuthError):
    """The gateway presented another certificate than the pinned one (contract
    ``tls.client.onMismatch`` = ``reauth``). Nothing was sent; only the user may pin a new one."""

    def __init__(self, seen: str | None) -> None:
        super().__init__("the gateway's TLS certificate is not the pinned one")
        self.seen = seen


class EcoPulseNotEnabledError(EcoPulseError):
    """404: Home Assistant is not enabled on this gateway, or its firmware is too old
    (contract action ``retry-later``)."""


class EcoPulseInvalidRequestError(EcoPulseError):
    """400: the gateway could not read the request (contract action ``drop-request``)."""


class EcoPulseInputsRefusedError(EcoPulseError):
    """403 on ``POST /inputs`` (contract ``client.routeErrors``: ``disable-route``): the token may
    not post values (the toggle in the EcoPulse web app is off) or the request came in the clear.
    Deliberately no :class:`EcoPulseAuthError`: reading goes on, the entry is not reauthenticated.
    """

    def __init__(self, code: str | None) -> None:
        super().__init__(f"the gateway refused the inputs ({code})")
        self.code = code


@dataclass(frozen=True, slots=True)
class InputsResult:
    """A 202 of ``POST /inputs``: every key answered, accepted or rejected with a contract code."""

    accepted: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class EcoPulseRateLimitedError(EcoPulseError):
    """429: the gateway's lockout holds this client (contract action ``honour-retry-after``)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("rate limited by the gateway")
        self.retry_after = retry_after


class EcoPulseApiClient:
    """One gateway, one token; pinned to one certificate when ``fingerprint`` is given."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        *,
        fingerprint: str | None = None,
    ) -> None:
        self._session = session
        # The scheme follows the pin and nothing else: no fallback, no upgrade, no redirect.
        scheme = "https" if fingerprint else "http"
        self._base = URL.build(scheme=scheme, host=host, port=port, path=API_BASE_PATH)
        self._ssl: aiohttp.Fingerprint | bool = (
            aiohttp.Fingerprint(fingerprint_digest(fingerprint)) if fingerprint else True
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    @property
    def base_url(self) -> str:
        return str(self._base)

    @property
    def pinned(self) -> bool:
        return isinstance(self._ssl, aiohttp.Fingerprint)

    async def async_get_info(self) -> GatewayInfo:
        return _parse("info", GatewayInfo.from_dict, await self._get_json("info"))

    async def async_get_catalog(self) -> Catalog:
        return _parse("catalog", Catalog.from_dict, await self._get_json("catalog"))

    async def async_post_inputs(self, items: list[dict[str, Any]]) -> InputsResult:
        """``POST /inputs`` (schema 5, #624). A 403 is the route's
        (:class:`EcoPulseInputsRefusedError`), never a reauth; the token goes only over the pinned
        connection the caller checked for."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                self._base / INPUTS_PATH,
                headers=self._headers,
                json={"inputs": items},
                timeout=timeout,
                allow_redirects=False,
                ssl=self._ssl,
            ) as response:
                if response.status == 403:
                    raise EcoPulseInputsRefusedError(await _error_code(response))
                _raise_for_status(response, success=202)
                data = await response.json()
        except aiohttp.ServerFingerprintMismatch as err:
            raise _mismatch(err) from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EcoPulseConnectionError(f"POST inputs failed: {type(err).__name__}") from err
        return _parse("inputs", _inputs_result, data if isinstance(data, dict) else {})

    async def async_stream(self) -> AsyncIterator[SseEvent]:
        """Yield events until the stream ends; raise on silence, errors or auth failure.

        ``sock_read`` makes aiohttp fail a read that saw no byte for the dead timeout, which is
        exactly the contract's "no byte for 45 s = dead" (pings count as bytes).
        """
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=REQUEST_TIMEOUT_SECONDS, sock_read=STREAM_DEAD_AFTER_SECONDS
        )
        parser = SseParser()
        try:
            async with self._session.get(
                self._base / "stream",
                headers={**self._headers, "Accept": "text/event-stream"},
                timeout=timeout,
                allow_redirects=False,
                ssl=self._ssl,
            ) as response:
                _raise_for_status(response)
                async for chunk in response.content.iter_any():
                    for event in parser.feed(chunk):
                        yield event
        except aiohttp.ServerFingerprintMismatch as err:
            raise _mismatch(err) from err
        except (aiohttp.ClientError, TimeoutError, SseProtocolError) as err:
            raise EcoPulseConnectionError(f"stream failed: {type(err).__name__}") from err

    async def _get_json(self, path: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.get(
                self._base / path,
                headers=self._headers,
                timeout=timeout,
                allow_redirects=False,  # the token only ever goes to the gateway typed in
                ssl=self._ssl,
            ) as response:
                _raise_for_status(response)
                data = await response.json()
        except aiohttp.ServerFingerprintMismatch as err:
            raise _mismatch(err) from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EcoPulseConnectionError(f"GET {path} failed: {type(err).__name__}") from err
        if not isinstance(data, dict):
            raise EcoPulseConnectionError(f"GET {path} returned no JSON object")
        return data


def client_for(session: aiohttp.ClientSession, data: Mapping[str, Any]) -> EcoPulseApiClient:
    """The client of a config entry's data: pinned HTTPS on the TLS port when the entry holds a
    fingerprint, plain HTTP on the web port only when it never had one."""
    fingerprint = data.get(CONF_TLS_FINGERPRINT)
    port = data[CONF_TLS_PORT] if fingerprint else data[CONF_PORT]
    return EcoPulseApiClient(
        session, data[CONF_HOST], port, data[CONF_TOKEN], fingerprint=fingerprint
    )


def format_fingerprint(digest: bytes) -> str:
    """A SHA-256 digest in the contract's format: uppercase hex pairs separated by colons."""
    return ":".join(f"{byte:02X}" for byte in digest)


def fingerprint_digest(fingerprint: str) -> bytes:
    if not re.fullmatch(FINGERPRINT_PATTERN, fingerprint):
        raise ValueError("not a certificate fingerprint in the contract format")
    return bytes.fromhex(fingerprint.replace(":", ""))


async def async_fetch_certificate_fingerprint(host: str, port: int, context: ssl.SSLContext) -> str:
    """The SHA-256 of the certificate the gateway presents, read from the TLS handshake.

    Nothing is sent over the connection: the token goes out only after the user confirmed this
    fingerprint against the one the EcoPulse web app shows (contract ``tls.client.firstPin``).
    ``context`` must not verify (the certificate is self-signed; trust is the pin).
    """
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            _, writer = await asyncio.open_connection(host, port, ssl=context)
    except (OSError, TimeoutError, ssl.SSLError) as err:
        raise EcoPulseConnectionError(f"TLS handshake failed: {type(err).__name__}") from err
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        # The fingerprint is read; how the close went does not matter.
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
    if not der:
        raise EcoPulseConnectionError("the gateway presented no certificate")
    return format_fingerprint(hashlib.sha256(der).digest())


def _mismatch(err: aiohttp.ServerFingerprintMismatch) -> EcoPulseCertificateMismatchError:
    got = getattr(err, "got", None)
    return EcoPulseCertificateMismatchError(format_fingerprint(got) if got else None)


def _parse[T](path: str, parse: Callable[[dict[str, Any]], T], data: dict[str, Any]) -> T:
    try:
        return parse(data)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise EcoPulseConnectionError(f"GET {path} returned a malformed body") from err


def _inputs_result(data: dict[str, Any]) -> InputsResult:
    return InputsResult(
        accepted=tuple(str(key) for key in data["accepted"]),
        rejected=tuple((str(r["key"]), str(r["code"])) for r in data["rejected"]),
    )


async def _error_code(response: aiohttp.ClientResponse) -> str | None:
    try:
        body = await response.json()
    except (aiohttp.ClientError, ValueError):
        return None
    return body.get("code") if isinstance(body, dict) else None


def _raise_for_status(response: aiohttp.ClientResponse, success: int = 200) -> None:
    status = response.status
    if status == success:
        return
    if status == 400:
        raise EcoPulseInvalidRequestError("gateway answered 400")
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
    "EcoPulseCertificateMismatchError",
    "EcoPulseConnectionError",
    "EcoPulseError",
    "EcoPulseInputsRefusedError",
    "EcoPulseInvalidRequestError",
    "EcoPulseNotEnabledError",
    "EcoPulseRateLimitedError",
    "InputsResult",
    "async_fetch_certificate_fingerprint",
    "client_for",
    "fingerprint_digest",
    "format_fingerprint",
    "parse_retry_after",
]
