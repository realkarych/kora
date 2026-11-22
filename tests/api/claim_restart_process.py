import asyncio
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from kora.conformance.capture import captured
from kora.db.connection import Database, DatabaseSettings, parse_database_url
from kora.ir.graph_json import ObjectReader
from kora.models.worker_claims import WorkerClaimResponse, decoded_claim
from kora.observability import LoggingSettings
from kora.protocols.deadline_records import duration_us
from tests.api.claim_support import claim_application
from tests.client.support import BOUND, Reader, Writer, peer, read_request, running_app
from tests.plugins.authentication.support import AuthorityClock


@dataclass(frozen=True)
class ClaimProcess:
    pid: int
    origin: str


async def wait_for_exit(child: asyncio.subprocess.Process) -> None:
    try:
        status = await asyncio.wait_for(child.wait(), BOUND)
    finally:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                await asyncio.wait_for(child.wait(), BOUND)
            else:
                await asyncio.wait_for(child.wait(), BOUND)
    assert status == 0


@asynccontextmanager
async def claim_process(
    settings: DatabaseSettings, clock: datetime, default_stall: timedelta
) -> AsyncGenerator[ClaimProcess]:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.api.claim_restart_process",
        clock.isoformat(),
        str(duration_us(default_stall)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    try:
        child.stdin.write(
            settings.url.render_as_string(hide_password=False).encode() + b"\n"
        )
        await asyncio.wait_for(child.stdin.drain(), BOUND)
        ready = await asyncio.wait_for(child.stdout.readline(), BOUND)
        pid, origin = ready.decode().split()
        assert int(pid) == child.pid != os.getpid()
        assert child.returncode is None
        yield ClaimProcess(child.pid, origin)
    finally:
        child.stdin.close()
        await wait_for_exit(child)


@dataclass(frozen=True)
class LostClaim:
    origin: str
    original: asyncio.Future[WorkerClaimResponse]


@asynccontextmanager
async def lose_claim_response(origin: str) -> AsyncGenerator[LostClaim]:
    endpoint = urlsplit(origin)
    host, port = endpoint.hostname, endpoint.port
    assert host == "127.0.0.1"
    assert port is not None
    original = asyncio.get_running_loop().create_future()

    async def relay(reader: Reader, _client: Writer) -> None:
        async with asyncio.timeout(BOUND):
            request = await read_request(reader)
            server_reader, server_writer = await asyncio.open_connection(host, port)
            try:
                server_writer.write(request)
                await server_writer.drain()
                response = await read_request(server_reader)
                headers, _, body = response.partition(b"\r\n\r\n")
                assert headers.splitlines()[0] == b"HTTP/1.1 200 OK"
                original.set_result(decoded_claim(ObjectReader(body.decode()).object()))
            finally:
                server_writer.close()
                await server_writer.wait_closed()

    async with peer(relay) as listener:
        yield LostClaim(listener.url, original)


async def serve(
    settings: DatabaseSettings, clock: AuthorityClock, stall: timedelta
) -> None:
    database = Database(settings)
    try:
        with captured("api", LoggingSettings(), "claim-restart", clock.now) as capture:
            app = claim_application(database, capture, clock, default_stall=stall)
            async with running_app(app) as origin:
                sys.stdout.write(f"{os.getpid()} {origin}\n")
                sys.stdout.flush()
                assert await asyncio.to_thread(sys.stdin.read, 1) == ""
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(
        serve(
            parse_database_url(sys.stdin.readline().strip()),
            AuthorityClock(datetime.fromisoformat(sys.argv[1])),
            timedelta(microseconds=int(sys.argv[2])),
        )
    )
