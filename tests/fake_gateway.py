"""An in-process EdgeGateway serving the HA API from the contract fixture's examples.

It mirrors the edge's order of checks (ADR-230 §4): disabled -> 404, blocked -> 429, no or
wrong token -> 401, forced status (e.g. 403) -> that status, unknown path -> 404.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

CONTRACT_PATH = Path(__file__).parent / "fixtures" / "ha-api-contract.json"


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@dataclass
class RecordedRequest:
    path: str
    authorization: str | None
    query: dict[str, str]


@dataclass
class FakeGateway:
    """Behaviour knobs are plain attributes; tests flip them between steps."""

    contract: dict[str, Any] = field(default_factory=load_contract)
    enabled: bool = True
    rate_limited_for: int | None = None
    forced_status: int | None = None
    send_snapshot: bool = True
    requests: list[RecordedRequest] = field(default_factory=list)
    stream_connections: int = 0

    def __post_init__(self) -> None:
        examples = self.contract["examples"]
        self.token: str = examples["token"]["token"]
        self.info: dict[str, Any] = copy.deepcopy(examples["info"])
        self.catalog: dict[str, Any] = copy.deepcopy(examples["catalog"])
        self.snapshot: dict[str, Any] = copy.deepcopy(examples["stream"]["snapshot"])
        self._queues: list[asyncio.Queue[str | None]] = []
        self._server: TestServer | None = None
        self._stream_opened = asyncio.Event()
        self._closing = False
        self._streams_ready = 0

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/ha/v1/{tail:.*}", self._handle)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()

    async def close(self) -> None:
        # Refuse new streams first: a client reconnecting during teardown would otherwise open a
        # stream no drop reaches, and the server would wait for it until the client gives up.
        self._closing = True
        self.drop_streams()
        if self._server is not None:
            await self._server.close()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None
        assert self._server.port is not None
        return self._server.port

    # -- stream control ------------------------------------------------------------------------

    @property
    def open_streams(self) -> int:
        return len(self._queues)

    def push(self, event: str, data: dict[str, Any]) -> None:
        frame = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for queue in self._queues:
            queue.put_nowait(frame)

    def ping(self) -> None:
        for queue in self._queues:
            queue.put_nowait(": ping\n\n")

    def drop_streams(self) -> None:
        for queue in self._queues:
            queue.put_nowait(None)

    async def wait_for_stream(self, connection_number: int, timeout: float = 5) -> None:
        """Wait until the n-th stream connection (1-based) is open and its snapshot was sent."""
        async with asyncio.timeout(timeout):
            while self._streams_ready < connection_number:
                self._stream_opened.clear()
                await self._stream_opened.wait()

    # -- HTTP ----------------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            RecordedRequest(
                path=request.path,
                authorization=request.headers.get("Authorization"),
                query=dict(request.query),
            )
        )
        if self._closing:
            return _error(503, "closing", "Shutting down.")
        if not self.enabled:
            return _error(404, "not_found", "Not found.")
        if self.rate_limited_for is not None:
            return _error(
                429,
                "rate_limited",
                "Too many failed attempts.",
                {"Retry-After": str(self.rate_limited_for)},
            )
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            unauthorized = self.contract["examples"]["unauthorized"]
            return web.json_response(
                unauthorized["body"], status=401, headers=unauthorized["headers"]
            )
        if self.forced_status is not None:
            return _error(self.forced_status, "forced", "Forced by the test.")
        tail = request.match_info["tail"]
        if tail == "info":
            return web.json_response(self.info)
        if tail == "catalog":
            return web.json_response(self.catalog)
        if tail == "stream":
            return await self._stream(request)
        return _error(404, "not_found", "Not found.")

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        # Registered before the first await, so drop_streams() always reaches this stream.
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queues.append(queue)
        self.stream_connections += 1
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            await response.prepare(request)
            await response.write(b"retry: 5000\n\n")
            if self.send_snapshot:
                snapshot = json.dumps(self.snapshot, separators=(",", ":"))
                await response.write(f"event: snapshot\ndata: {snapshot}\n\n".encode())
            self._streams_ready += 1
            self._stream_opened.set()
            while (frame := await queue.get()) is not None:
                await response.write(frame.encode())
        except ConnectionResetError:
            pass
        finally:
            self._queues.remove(queue)
        return response


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> web.Response:
    return web.json_response({"code": code, "message": message}, status=status, headers=headers)
