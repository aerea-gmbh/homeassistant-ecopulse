"""An in-process EdgeGateway serving the HA API from the contract fixture's examples.

It mirrors the edge's order of checks (ADR-230 §4): disabled -> 404, blocked -> 429, no or
wrong token -> 401, forced status (e.g. 403) -> that status, unknown path -> 404.

With ``tls=True`` it serves HTTPS with a self-signed ECDSA P-256 certificate of its own, like
the gateway's TLS port (#610 S8-TLS); ``fingerprint`` is that certificate's SHA-256 in the
contract format, and ``/info`` names it.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import ssl
import tempfile
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

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
    redirect_to: str | None = None
    send_snapshot: bool = True
    tls: bool = False
    requests: list[RecordedRequest] = field(default_factory=list)
    stream_connections: int = 0
    fingerprint: str | None = None

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

    async def start(self, port: int | None = None) -> None:
        app = web.Application()
        app.router.add_get("/api/ha/v1/{tail:.*}", self._handle)
        self._server = TestServer(app, host="127.0.0.1", port=port)
        context = self._new_certificate() if self.tls else None
        await self._server.start_server(ssl=context)
        if self.tls:
            self.info["transport"] = {
                "tls": True,
                "tlsPort": self.port,
                "certificateSha256": self.fingerprint,
            }

    async def restart_with_a_new_certificate(self) -> None:
        """The gateway made a new certificate (its file was lost): same port, new fingerprint."""
        port = self.port
        assert self._server is not None
        self.drop_streams()  # an open stream would hold the close until the client gives up
        await self._server.close()
        await self.start(port)

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

    def _new_certificate(self) -> ssl.SSLContext:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EcoPulse EdgeGateway (test)")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        der = certificate.public_bytes(serialization.Encoding.DER)
        self.fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        with tempfile.TemporaryDirectory() as directory:
            cert_file, key_file = Path(directory, "cert.pem"), Path(directory, "key.pem")
            cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context.load_cert_chain(cert_file, key_file)
        return context

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
        if self.redirect_to is not None:
            raise web.HTTPFound(self.redirect_to)
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
