from decimal import Decimal

import pytest

from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.engine.approval_cost import ApprovalCostUnavailable
from kora.engine.approval_cost_totals import ApprovalCostOverflow
from kora.engine.locks import COMPONENT
from kora.observability import Level, LoggingSettings
from tests.db.seed import FakeClock, Seeder
from tests.engine.approval_cost_support import (
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
)
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    wait_until,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["prepared", "unavailable", "overflow"])
async def test_approval_cost_diagnostics_exclude_content(
    database: Database, outcome: str
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        if outcome == "unavailable":
            await Seeder(writer).usage(identity, "private-model", Decimal("1"))
        else:
            await cost_usage(writer, identity, "private-model", amount="99999999999999")
            if outcome == "overflow":
                await cost_usage(writer, identity, "private-second-model", amount="1")

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "cost-test", FakeClock().now
    ) as capture:
        if outcome == "unavailable":
            with pytest.raises(ApprovalCostUnavailable):
                await case.calculate(database)
        elif outcome == "overflow":
            with pytest.raises(ApprovalCostOverflow):
                await case.calculate(database)
        else:
            await case.calculate(database)
        records = capture.finish()

    assert [
        record["outcome"]
        for record in records
        if record["event"] == "engine.approval_cost.calculated"
    ] == [outcome]
    assert all(
        value not in repr(records)
        for value in (
            case.run_id,
            case.namespace_id,
            "private-model",
            "99999999999999",
            "must-not-be-read",
        )
    )
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_approval_cost_survives_unavailable_diagnostic_sink(
    database: Database, configure: Configure, pipe: Pipe, broken: bool
) -> None:
    diagnostics = configure(
        LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65_536),
        pipe.write_end,
    )
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(writer, identity)

    result = await case.calculate(database)
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert result.usd_estimate == "0.1"
    assert snapshot.dropped_total() > 0
    assert drained
    assert not diagnostics.writer_is_alive()
    assert database.units_in_flight() == 0
