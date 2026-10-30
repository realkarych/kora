import asyncio
import json
import os
import sys
from pathlib import Path

from kora.db.connection import Database, parse_database_url
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.authentication.static_token import COMPONENT, StaticTokenAuthenticator
from kora.protocols.activity import NoActivity
from kora.protocols.authentication import RequestInfo
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import SystemClock
from tests.db.closing import ConnectionLedger, reached
from tests.plugins.authentication.support import seed_case
from tests.plugins.storage.sink_probe import fill


def run(sink: str, configuration: Path) -> dict[str, object]:
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
        "authentication-sink",
        clock.now,
        write_end,
    )
    context = ProtocolContext(
        "authentication-sink",
        None,
        None,
        diagnostics.logger(COMPONENT),
        None,
        None,
        None,
        None,
        clock,
        NoActivity(),
    )
    database = Database(parse_database_url(data))
    provider = StaticTokenAuthenticator({}, database)
    ledger = ConnectionLedger(database)
    completed = 0

    async def exercise() -> None:
        nonlocal completed
        try:
            case = await seed_case(database, "sink")
            for _ in range(150):
                assert (
                    await provider.authenticate(case.request, context) == case.principal
                )
                refusal: KoraProtocolError | None = None
                try:
                    await provider.authenticate(
                        RequestInfo("Basic private-sink-marker"), context
                    )
                except KoraProtocolError as error:
                    refusal = error
                assert refusal is not None
                assert refusal.code == "authentication_rejected"
                completed += 1
        finally:
            await database.close()

    asyncio.run(exercise())
    state = diagnostics.snapshot()
    drained = diagnostics.shutdown(1)
    if sink == "blocked":
        os.close(read_end)
    assert asyncio.run(reached(lambda: not diagnostics.writer_is_alive(), 1))
    os.close(write_end)
    return {
        "completed": completed,
        "outstanding": ledger.outstanding,
        "drained": drained,
        "dropped": state.dropped_total(),
        "failed": state.sink_failed,
        "writer_alive": diagnostics.writer_is_alive(),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(sys.argv[1], Path(sys.argv[2]))) + "\n")
