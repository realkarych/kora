from typing import cast

import pytest
from sqlalchemy import select

from kora.db.claim_sources import acquire_claim_sources
from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.run_input_reads import RunInputField
from kora.db.tables import attempt, attempt_claim_read_admission, graph_version, wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.attempt_inputs import read_attempt_inputs
from kora.engine.attempt_preparation_budget import admit_attempt_preparation
from kora.engine.claim_material import prepare_claim_material
from kora.engine.claim_sources import decode_claim_sources
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.run_input_reads import read_run_input
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import (
    MAX_IMMUTABLE_BYTES,
    MAX_NODE_POLICY_BYTES,
    ClaimWireRejected,
)
from tests.db.closing import ConnectionLedger
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.expiry_support import expiry_context
from tests.engine.large_admission_support import READ, large_admission_case
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS


async def exercise_full_claim(database: Database, padding: str, extra: int) -> None:
    workload = await large_admission_case(database, padding, extra)
    case = workload.case
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)
    reservation = admit_attempt_preparation(case.checked, workload.limits)
    assert reservation.retained_units <= workload.limits.retained_units
    assert reservation.work_units <= workload.limits.work_units

    if extra:
        with pytest.raises(ClaimWireRejected, match="Claim representation is invalid"):
            case.prepare(limits=workload.limits)
        assert ledger.checkouts == 0
        after = await case.promotion.snapshot(database)
        async with database.read_transaction() as reader:
            saved_attempt = await reader.scalar(select(attempt.c.id))
            saved_source = await reader.scalar(
                select(attempt_claim_read_admission.c.attempt_id)
            )
            saved_wait = await reader.scalar(select(wait.c.id))

        assert after == before
        assert saved_attempt is saved_source is saved_wait is None
        assert ledger.outstanding == database.units_in_flight() == 0
        return

    prepared = case.prepare(limits=workload.limits)
    assert ledger.checkouts == 0
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    states: list[AttemptAdmissionState] = []
    for _ in range(2):
        async with database.transaction() as writer:
            states.append(
                await admit_attempt(
                    await expiry_context(writer, case.promotion),
                    prepared,
                    LIMITS,
                    caps,
                    global_limit=1,
                    runtime_limit=1,
                    interval=INTERVAL,
                )
            )
    async with database.read_transaction() as reader:
        sources = await acquire_claim_sources(reader, case.binding, LIMITS)
        scope = case.promotion.scope
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=READ.identity.max_encoded_bytes,
        )
        refs = await reader.scalar(
            select(graph_version.c.resources).where(
                graph_version.c.id == scope.version_id
            )
        )
        with await reader.execute(select(attempt)) as rows:
            saved = dict(rows.mappings().one())
    assert sources is not None
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, case.promotion.pin, scope, cast(JsonObject, refs), (), limits=READ
    )
    del acquired, refs
    params = await read_run_input(
        database, case.binding.run, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert params is not None
    decoded = decode_claim_sources(sources)
    inputs = read_attempt_inputs(decoded.values["input_bindings"], {})
    checkouts = ledger.checkouts
    material = prepare_claim_material(
        checked,
        params,
        case.binding.node_path,
        inputs.claim_map(),
        case.price,
        SELECTION,
    )

    assert states == [AttemptAdmissionState.QUEUED, AttemptAdmissionState.REFUSED]
    selected = (
        material.immutable.payload if padding == "immutable" else material.node_policy
    )
    assert len(selected) == workload.expected_bytes
    assert len(material.immutable.payload) <= MAX_IMMUTABLE_BYTES
    assert len(material.node_policy) <= MAX_NODE_POLICY_BYTES
    assert material.identity.binding == case.binding.run
    assert (
        material.identity.contract_digest
        == case.promotion.pin.execution_contract_digest
    )
    assert inputs.claim_map() == {}
    assert saved["id"] == case.binding.attempt_id
    assert saved["price_table_id"] == case.price.id
    assert saved["status"] == "created"
    assert saved["started_at"] is saved["deadline"] is saved["deadline_binding"] is None
    assert sources.binding == case.binding
    assert ledger.checkouts == checkouts
    assert ledger.outstanding == database.units_in_flight() == 0
    _, nodes, _ = await case.promotion.snapshot(database)
    node = next(node for node in nodes if node["id"] == case.binding.node_id)
    assert node["current_attempt_id"] == case.binding.attempt_id
    assert node["attempt_count"] == 1
    assert node["status"] == "queued"
