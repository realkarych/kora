from dataclasses import replace
from datetime import timedelta

import pytest

from kora.db.connection import Database
from kora.engine.claim_material import ClaimRuntimeSelection, prepare_claim_material
from kora.observability import Level, LoggingSettings
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.closing import ConnectionLedger
from tests.engine.test_claim_material import PRICE, source
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level", [Level.DEBUG, Level.INFO, Level.ERROR, Level.CRITICAL]
)
async def test_claim_material_diagnostics_are_content_free(
    configure: Configure,
    pipe: Pipe,
    database: Database,
    level: Level,
) -> None:
    checked, params, _, _ = await source(database)
    selection = ClaimRuntimeSelection(
        "selected-runtime", "selected-vcs", timedelta(seconds=1)
    )
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))

    material = prepare_claim_material(checked, params, "producer", {}, PRICE, selection)
    with pytest.raises(ClaimWireRejected):
        prepare_claim_material(
            checked,
            params,
            "producer",
            {},
            PRICE,
            replace(selection, runtime="private-marker"),
        )
    finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert [
        record["prepared"]
        for record in records
        if record["event"] == "engine.claim_material.prepared"
    ] == ([True] if level is Level.DEBUG else [])
    for private in (
        params.binding.run_id,
        checked.pin.execution_contract_digest,
        "private-marker",
        "selected-runtime",
        "params.integer",
    ):
        assert private.encode() not in pipe.reader.data
    assert material.immutable.payload
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_claim_material_survives_unavailable_sink(
    configure: Configure,
    pipe: Pipe,
    database: Database,
    broken: bool,
) -> None:
    checked, params, _, _ = await source(database)
    selection = ClaimRuntimeSelection(
        "selected-runtime", "selected-vcs", timedelta(seconds=1)
    )
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536)
    diagnostics = configure(settings, pipe.write_end)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    first = prepare_claim_material(checked, params, "producer", {}, PRICE, selection)
    second = prepare_claim_material(checked, params, "producer", {}, PRICE, selection)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert first == second
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
