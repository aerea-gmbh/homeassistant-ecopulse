"""Server-Sent Events framing of the EcoPulse HA stream (ADR-230 §6).

Pure, network-free: the API client feeds raw chunks and gets complete events back. The parser
splits lines itself instead of using aiohttp's ``readline()``, because a snapshot is one
``data:`` line whose size grows with the plant and aiohttp refuses lines above 128 kB.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_EVENT_NAME = "message"
# A generous cap on one event (its pending line plus its data lines) so a broken peer cannot grow
# memory without bound; a real snapshot of a large plant is ~100 kB.
DEFAULT_MAX_EVENT_BYTES = 8 * 1024 * 1024


class SseProtocolError(Exception):
    """The peer sent something that is not a usable event stream."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched event: its name and its decoded JSON object."""

    name: str
    data: dict[str, Any]


class SseParser:
    """Incremental parser; ``feed`` returns the events completed by the chunk."""

    def __init__(self, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> None:
        self._max_event_bytes = max_event_bytes
        self._buffer = bytearray()
        self._event_name: str | None = None
        self._data_lines: list[str] = []
        self._data_bytes = 0

    def feed(self, chunk: bytes) -> list[SseEvent]:
        self._buffer.extend(chunk)
        events: list[SseEvent] = []
        while (newline := self._buffer.find(b"\n")) >= 0:
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            event = self._process_line(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
            if event is not None:
                events.append(event)
            self._check_size()
        self._check_size()
        return events

    def _check_size(self) -> None:
        if self._data_bytes + len(self._buffer) > self._max_event_bytes:
            raise SseProtocolError("event stream event exceeds the size limit")

    def _process_line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment, e.g. the ": ping" heartbeat
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            self._event_name = value
        elif field == "data":
            self._data_lines.append(value)
            self._data_bytes += len(value) + 1
        # "retry" is deliberately ignored (the integration has its own backoff), "id" is never
        # sent by the edge, and unknown fields are ignored as the SSE spec requires.
        return None

    def _dispatch(self) -> SseEvent | None:
        name = self._event_name or DEFAULT_EVENT_NAME
        data_lines = self._data_lines
        self._event_name = None
        self._data_lines = []
        self._data_bytes = 0
        if not data_lines:
            return None
        try:
            data = json.loads("\n".join(data_lines))
        except ValueError:
            _LOGGER.warning("Ignoring '%s' event with malformed JSON", name)
            return None
        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring '%s' event whose data is not a JSON object", name)
            return None
        return SseEvent(name, data)
