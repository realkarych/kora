import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import StrictBool, StrictInt

from kora.api.application import create_application
from kora.api.exchange import COMPONENT
from kora.db.connection import Database, parse_database_url
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.boundary import BoundaryModel
from kora.protocols.time import SystemClock
from tests.api.support import RequestIds, client_for
from tests.db.closing import ConnectionLedger, reached
from tests.plugins.authentication.support import seed_case
from tests.plugins.storage.sink_probe import fill


class SinkReport(BoundaryModel):
    completed: StrictInt
    outstanding: StrictInt
    writer_alive: StrictBool
    drained: StrictBool
    dropped: StrictInt
    failed: StrictBool


def run(sink: str, configuration: Path) -> SinkReport:
    data: object = json.loads(configuration.read_bytes())
    assert isinstance(data, str)
    read_end, write_end = os.pipe()
    if sink == "blocked":
        fill(write_end)
    else:
        os.close(read_end)
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, queue_records=16, queue_bytes=65536),
        "api-sink",
        clock.now,
        write_end,
    )
    database = Database(parse_database_url(data))
    ledger = ConnectionLedger(database)
    app = create_application(
        StaticTokenAuthenticator({}, database), diagnostics, clock, RequestIds()
    )
    completed = 0

    async def exercise() -> None:
        nonlocal completed
        try:
            case = await seed_case(database, "api-sink")
            headers = {"Authorization": str(case.request.authorization)}
            async with client_for(app) as client:
                for _ in range(32):
                    assert (await client.get("/healthz")).status_code == 200
                    assert (
                        await client.get("/api/v1/openapi.json", headers=headers)
                    ).status_code == 200
                    assert (
                        await client.post("/api/v1/openapi.json", headers=headers)
                    ).status_code == 405
                    completed += 3
        finally:
            await database.close()

    asyncio.run(exercise())
    snapshot = diagnostics.snapshot()
    drained = diagnostics.shutdown(1)
    if sink == "blocked":
        os.close(read_end)
    assert asyncio.run(reached(lambda: not diagnostics.writer_is_alive(), 1))
    os.close(write_end)
    return SinkReport(
        completed=completed,
        outstanding=ledger.outstanding,
        writer_alive=diagnostics.writer_is_alive(),
        drained=drained,
        dropped=snapshot.dropped_total(),
        failed=snapshot.sink_failed,
    )


if __name__ == "__main__":
    sys.stdout.write(run(sys.argv[1], Path(sys.argv[2])).model_dump_json() + "\n")
