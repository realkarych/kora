import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.types import ASGIApp

from kora.client import KoraClient
from kora.client.transport import openapi_document
from kora.ir.json import JsonObject

BOUND: float = 10.0
CREDENTIAL: str = "private-http-client-credential"
SCHEMA: bytes = b'{"openapi":"3.1.0","info":{"title":"kora","version":"1"},"paths":{}}'
SCHEMA_REPLY: bytes = b"HTTP/1.1 200 OK"
Reader = asyncio.StreamReader
Writer = asyncio.StreamWriter


class ProbeClient(KoraClient):
    async def probe(
        self,
        method: str,
        *,
        response_limit: int,
        body: JsonObject | None = None,
        decode: Callable[[JsonObject], JsonObject] = openapi_document,
    ) -> JsonObject:
        return await self._request_json(
            method,
            "/api/v1/probe",
            decode,
            response_limit=response_limit,
            body=body,
        )

    def request_owners(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(self._active)


async def read_request(reader: Reader) -> bytes:
    headers = await reader.readuntil(b"\r\n\r\n")
    for line in headers.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            return headers + await reader.readexactly(int(value))
    return headers


def reply(
    writer: Writer,
    body: bytes = SCHEMA,
    status: bytes = SCHEMA_REPLY,
    headers: tuple[bytes, ...] = (),
) -> None:
    writer.write(
        b"\r\n".join(
            (
                status,
                b"Content-Type: application/json",
                b"Content-Length: " + str(len(body)).encode(),
                *headers,
                b"",
                body,
            )
        )
    )


@dataclass(frozen=True)
class Peer:
    port: int
    tls: bool

    @property
    def url(self) -> str:
        return self.origin()

    def origin(self, host: str = "127.0.0.1") -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{host}:{self.port}"


@asynccontextmanager
async def peer(
    handle: Callable[[Reader, Writer], Awaitable[None]],
    context: ssl.SSLContext | None = None,
) -> AsyncGenerator[Peer]:
    tasks: list[asyncio.Task[None]] = []
    writers: set[Writer] = set()

    async def run(reader: Reader, writer: Writer) -> None:
        try:
            await handle(reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                assert writer.is_closing()
            finally:
                writers.remove(writer)

    def accepted(reader: Reader, writer: Writer) -> None:
        writers.add(writer)
        tasks.append(asyncio.create_task(run(reader, writer)))

    server = await asyncio.start_server(accepted, "127.0.0.1", 0, ssl=context)
    try:
        yield Peer(int(server.sockets[0].getsockname()[1]), context is not None)
    finally:
        server.close()
        for writer in writers:
            writer.close()
        async with asyncio.timeout(BOUND):
            await asyncio.gather(*tasks)
            await server.wait_closed()


class ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


@asynccontextmanager
async def running_app(app: ASGIApp) -> AsyncGenerator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        server = ReadyServer(
            uvicorn.Config(
                app, log_config=None, access_log=False, interface="asgi3", http="h11"
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(server.ready.wait(), BOUND)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, BOUND)


def tls_pair(
    directory: Path, *, include_localhost: bool = False
) -> tuple[ssl.SSLContext, Path]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Client test CA")])
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Client test peer")]
    )
    beginning = datetime(2020, 1, 1, tzinfo=UTC)
    expiry = datetime(2099, 1, 1, tzinfo=UTC)
    ca = (
        x509.CertificateBuilder()
        .issuer_name(ca_name)
        .subject_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(beginning)
        .not_valid_after(expiry)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    names: list[x509.GeneralName] = [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
    if include_localhost:
        names.append(x509.DNSName("localhost"))
    server = (
        x509.CertificateBuilder()
        .issuer_name(ca_name)
        .subject_name(server_name)
        .public_key(server_key.public_key())
        .serial_number(2)
        .not_valid_before(beginning)
        .not_valid_after(expiry)
        .add_extension(
            x509.SubjectAlternativeName(names),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certificate = directory / "peer.pem"
    certificate.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key = directory / "peer.key"
    key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context, ca_path
