from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import delete, insert, select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    graph_version,
    graph_version_read_admission,
    node_instance,
    wait,
)
from kora.engine.admission_snapshot import AdmissionSnapshotRejected
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.run_input_values import decode_run_input_value
from kora.ir.cost import PriceRevision
from kora.ir.json import JsonObject
from tests.api.deployment_cases import preparer, skeleton_ir
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, narrow_database
from tests.db.seed import Seeder
from tests.engine.admission_snapshot_support import (
    LIMITS,
    acquire,
    checked,
    snapshot_case,
)
from tests.engine.test_claim_snapshot import ClaimQueries

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_admission_snapshot_composes_published_sources(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    before = await case.promotion.snapshot(database)
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)

    try:
        acquired = await acquire(reader, case)
        assert ledger.outstanding == reader.units_in_flight() == 0
        graph = checked(acquired.graph, case.promotion.pin, case.promotion.scope)
        encoding = acquired.graph.physical.encoding
        params = decode_run_input_value(acquired.params, max_bytes=LIMITS.inputs.params)
        price = PriceRevision.model_validate(
            {
                "id": acquired.price_table_id,
                "namespace_id": acquired.price.namespace_id,
                "effective_from": acquired.price.effective_from.decode(encoding),
                "version": 1,
                "normalization_version": 1,
                "prices": decode_claim_source(acquired.price.prices, encoding),
            }
        )
        policy = NamespacePolicyConfiguration.prepare(
            acquired.candidate.run.namespace_id,
            acquired.policy.revision,
            decode_claim_source(acquired.policy.payload, encoding),
        )
    finally:
        await reader.close()
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as connection:
        attempts = (await connection.execute(select(attempt.c.id))).all()

    assert ledger.checkouts == 1
    assert acquired.graph is acquired.cap_graph
    assert acquired.candidate.node_id == case.node_id
    assert acquired.candidate.run.version_id == case.promotion.scope.version_id
    assert acquired.wait_id is None
    assert acquired.cap_source.number == 1
    assert graph.scope.version_id == case.promotion.scope.version_id
    assert params == {}
    assert price.id == case.price_id
    assert policy == case.policy
    assert before == after
    assert attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("active", ["both", "older", "none"])
async def test_admission_snapshot_uses_live_cap_version(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    active: str,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
    deployed = await case.publish(ir)
    version_id = deployed.graph_version_id.removeprefix("gv_")
    if active != "both":
        async with database.transaction() as writer:
            await writer.execute(
                update(graph_version)
                .where(
                    graph_version.c.id == version_id
                    if active == "older"
                    else graph_version.c.graph_id == case.promotion.scope.graph_id
                )
                .values(status="inactive")
            )

    acquired = await acquire(database, case)
    if active == "older":
        assert acquired.graph is acquired.cap_graph
        assert acquired.cap_source.version_id == case.promotion.scope.version_id
        assert acquired.cap_source.number == 1
        return
    cap_scope = GraphResourceScope(
        case.promotion.scope.namespace_id, case.promotion.scope.graph_id, version_id
    )
    cap_pin = GraphVersionPin(
        cap_scope.graph_id, cap_scope.version_id, deployed.execution_contract_digest
    )
    execution = checked(acquired.graph, case.promotion.pin, case.promotion.scope)
    cap = checked(acquired.cap_graph, cap_pin, cap_scope)

    assert acquired.graph is not acquired.cap_graph
    assert acquired.cap_source.version_id == version_id
    assert acquired.cap_source.number == 2
    assert execution.scope.version_id == case.promotion.scope.version_id
    assert cap.scope.version_id == version_id


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["pinned", "cap"])
async def test_admission_snapshot_refuses_either_graph_witness(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    selected: str,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
    deployed = await case.publish(ir)
    version_id = (
        case.promotion.scope.version_id
        if selected == "pinned"
        else deployed.graph_version_id.removeprefix("gv_")
    )
    async with database.transaction() as writer:
        await writer.execute(
            delete(graph_version_read_admission).where(
                graph_version_read_admission.c.graph_version_id == version_id
            )
        )
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire(database, case)

    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("periodic", [False, True])
async def test_admission_snapshot_wait_gate_precedes_graph_reads(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    offset: int,
    periodic: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        wait_id = seed.new_id()
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.node_id)
            .values(status="waiting")
        )
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=case.node_id,
                kind="admission",
                status="open",
                wake_at=case.promotion.clock.now() + timedelta(seconds=offset),
                **seed.stamps(),
            )
        )
    reader = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(reader)

    try:
        with ClaimQueries(reader) as queries:
            if periodic and offset > 0:
                with pytest.raises(AdmissionSnapshotRejected):
                    await acquire(reader, case, periodic_recheck=periodic)
            else:
                acquired = await acquire(reader, case, periodic_recheck=periodic)
                assert acquired.wait_id == wait_id
    finally:
        await reader.close()

    if periodic and offset > 0:
        assert all(
            "graph_version_read_admission" not in sql for sql in queries.statements
        )
    assert ledger.outstanding == reader.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "raw"])
@pytest.mark.parametrize("distinct", [False, True])
async def test_admission_snapshot_reserves_two_graphs_before_checkout(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    kind: str,
    distinct: bool,
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    if distinct:
        ir = skeleton_ir()
        cast(JsonObject, ir["runtime"])["concurrency"] = {"max_running_nodes": 2}
        await case.publish(ir)
    native, raw = LIMITS.reservation()
    exact = replace(LIMITS, native_bytes=native, raw_bytes=raw)
    insufficient = replace(
        exact, **{f"{kind}_bytes": native - 1 if kind == "native" else raw - 1}
    )
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionSnapshotRejected):
        await acquire(database, case, limits=insufficient)
    refused_checkouts = ledger.checkouts
    accepted = await acquire(database, case, limits=exact)

    assert refused_checkouts == 0
    assert (accepted.graph is not accepted.cap_graph) is distinct
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0
