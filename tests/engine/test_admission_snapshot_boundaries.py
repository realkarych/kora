import asyncio
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import insert, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import node_instance, run_inputs, wait
from kora.engine.admission_snapshot import (
    AdmissionSnapshotRejected,
    acquire_admission_snapshot,
)
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.engine.admission_snapshot_support import LIMITS, acquire, snapshot_case
from tests.engine.test_claim_snapshot import ClaimQueries

__all__ = ["api_capture", "preparer"]
IDENTITY = "01J00000000000000000000000"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "one_graph_allowance"),
    [("native_bytes", 150_000_000), ("raw_bytes", 100_000_000)],
)
async def test_admission_snapshot_one_graph_budget_never_checks_out(
    database: Database, field: str, one_graph_allowance: int
) -> None:
    limits = replace(LIMITS, **{field: one_graph_allowance})
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire_admission_snapshot(
            database,
            IDENTITY,
            limits,
            observed_at=START,
            periodic_recheck=True,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["node_row", "graph_native_bytes", "owner_text_bytes", "native_bytes", "raw_bytes"],
)
@pytest.mark.parametrize("value", [0, -1, True])
async def test_admission_snapshot_invalid_limits_never_check_out(
    database: Database, field: str, value: int
) -> None:
    limits = replace(LIMITS, **{field: value})
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire_admission_snapshot(
            database,
            IDENTITY,
            limits,
            observed_at=START,
            periodic_recheck=True,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["identity", "time", "deadline", "agent-only"])
async def test_admission_snapshot_invalid_request_never_checks_out(
    database: Database, invalid: str
) -> None:
    ledger = ConnectionLedger(database)
    deadline = asyncio.get_running_loop().time() + 10.0

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire_admission_snapshot(
            database,
            "private-invalid" if invalid == "identity" else IDENTITY,
            LIMITS,
            observed_at=START.replace(tzinfo=None) if invalid == "time" else START,
            periodic_recheck=True,
            deadline=deadline if invalid != "deadline" else float("nan"),
            agent_only=cast("bool", "agent") if invalid == "agent-only" else False,
        )

    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape", ["missing", "duplicate", "wrong-kind", "attempt", "fresh"]
)
async def test_admission_snapshot_refuses_incompatible_wait_before_graph_reads(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    shape: str,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        if shape != "fresh":
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == case.node_id)
                .values(status="waiting")
            )
        attempt_id = await seed.attempt(case.node_id) if shape == "attempt" else None
        for _ in range(0 if shape == "missing" else 2 if shape == "duplicate" else 1):
            await writer.execute(
                insert(wait).values(
                    id=seed.new_id(),
                    node_instance_id=case.node_id,
                    attempt_id=attempt_id,
                    kind="timer" if shape == "wrong-kind" else "admission",
                    status="open",
                    wake_at=START,
                    **seed.stamps(),
                )
            )
    ledger = ConnectionLedger(database)

    with ClaimQueries(database) as queries:
        with pytest.raises(AdmissionSnapshotRejected):
            await acquire(database, case)

    assert not any("graph_version_read_admission" in sql for sql in queries.statements)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_snapshot_native_witness_precedes_graph_projection(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    limits = replace(LIMITS, graph_native_bytes=1)
    ledger = ConnectionLedger(database)

    with ClaimQueries(database) as queries:
        with pytest.raises(AdmissionSnapshotRejected):
            await acquire(database, case, limits=limits)

    assert any("graph_version_read_admission" in sql for sql in queries.statements)
    assert not any(
        "AS ir" in sql or "CAST(v.ir AS BLOB)" in sql for sql in queries.statements
    )
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_snapshot_refuses_oversized_run_parameters(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    limits = replace(LIMITS, inputs=replace(LIMITS.inputs, params=2))
    async with database.transaction() as writer:
        await writer.execute(
            update(run_inputs)
            .where(run_inputs.c.run_id == case.promotion.run_id)
            .values(params=b'{"private":"oversized"}')
        )
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire(database, case, limits=limits)

    assert ledger.outstanding == database.units_in_flight() == 0
