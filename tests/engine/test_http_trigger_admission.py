from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.claim_sources import ClaimSourceBinding, acquire_claim_sources
from kora.db.connection import Database
from kora.db.cost_reads import read_latest_price
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.run_input_reads import RunInputBinding, RunInputField
from kora.db.tables import attempt, graph_version, inbound_event, node_instance, run
from kora.engine.admission_initialization import AdmissionInitialization
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.claim_sources import decode_claim_sources
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.readiness import prepare_readiness
from kora.engine.run_input_reads import read_run_input
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import PromotionOutcome
from kora.ir.json import JsonObject
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import (
    GRAPH,
    HARNESS,
    NAMESPACE,
    SELECTION,
    preparer,
    skeleton_ir,
)
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, fixed_randomness
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.admission_wait_support import LIMITS as SOURCE_LIMITS
from tests.engine.attempt_admission_support import AttemptAdmissionCase
from tests.engine.expiry_support import expiry_context
from tests.engine.promotion_support import PromotionCase
from tests.engine.start_cases import start_rows
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture", "preparer"]

KEY = "http-trigger-admission"
VERSIONS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/versions"
RUNS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/runs"


async def promotion_from_http(
    database: Database,
    namespace_id: str,
    accepted: AcceptedRun,
    deployed: DeployResponse,
    clock: FakeClock,
    identifiers: IdentifierGenerator,
) -> tuple[PromotionCase, CheckedExecutionContract]:
    run_id = accepted.run_id.removeprefix("run_")
    version_id = accepted.graph_version_id.removeprefix("gv_")
    async with database.read_transaction() as reader:
        graph_id = await reader.scalar(
            select(run.c.graph_id).where(
                run.c.id == run_id,
                run.c.namespace_id == namespace_id,
                run.c.graph_version_id == version_id,
            )
        )
        receipt_id = await reader.scalar(
            select(inbound_event.c.id).where(
                inbound_event.c.namespace_id == namespace_id,
                inbound_event.c.idempotency_key == KEY,
            )
        )
        assert isinstance(graph_id, str)
        assert isinstance(receipt_id, str)
        acquired = await acquire_graph_version(
            reader,
            namespace_id,
            graph_id,
            version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        references = await reader.scalar(
            select(graph_version.c.resources).where(graph_version.c.id == version_id)
        )
    assert acquired is not None
    scope = GraphResourceScope(namespace_id, graph_id, version_id)
    pin = GraphVersionPin(graph_id, version_id, deployed.execution_contract_digest)
    checked = check_detached_execution_contract(
        acquired, pin, scope, cast(JsonObject, references), (), limits=LIMITS
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=3
    )
    return (
        PromotionCase(
            scope, pin, materialization, run_id, receipt_id, clock, identifiers
        ),
        checked,
    )


async def admission_from_promoted_http(
    database: Database,
    promotion: PromotionCase,
    checked: CheckedExecutionContract,
    policy: NamespacePolicyConfiguration,
) -> AttemptAdmissionCase:
    async with database.read_transaction() as reader:
        node_id = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == "agent",
            )
        )
        price = await read_latest_price(reader, None, promotion.clock.now())
    assert isinstance(node_id, str)
    assert price is not None
    scope = promotion.scope
    binding = ClaimSourceBinding(
        RunInputBinding(
            scope.namespace_id, scope.graph_id, scope.version_id, promotion.run_id
        ),
        node_id,
        promotion.identifiers.new(),
        price.id,
        "agent",
    )
    params = await read_run_input(
        database, binding.run, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert params is not None
    return AttemptAdmissionCase(
        promotion,
        checked,
        prepare_readiness(checked, max_ir_bytes=len(checked.canonical_ir)),
        params,
        binding,
        price,
        policy,
    )


@pytest.mark.asyncio
async def test_http_trigger_replay_preserves_initial_attempt(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    identity = await seed_case(database, NAMESPACE)
    namespace_id = identity.principal.namespaces[0].namespace_id
    headers = {"Authorization": str(identity.request.authorization)}
    keyed = headers | {"Idempotency-Key": KEY}
    clock = FakeClock()
    identifiers = IdentifierGenerator(clock.now, fixed_randomness(37))
    policy = NamespacePolicyConfiguration.prepare(
        namespace_id, 1, {"max_pending_runs": 1000}
    )
    initialization = AdmissionInitialization(
        database, AuthorityClock(clock.now()), identifiers.new, policies=(policy,)
    )
    await initialization.initialize(api_capture.diagnostics)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    ledger = ConnectionLedger(database)
    async with client_for(app) as client:
        publication = await client.post(
            VERSIONS,
            json={"graph": GRAPH, "ir": skeleton_ir(), "resources": []},
            headers=headers,
        )
        assert publication.status_code == 201
        deployed = DeployResponse.model_validate(publication.json())

        first = await client.post(RUNS, json={"params": {}}, headers=keyed)
        accepted = AcceptedRun.model_validate(first.json())
        pending = await start_rows(database)
        async with database.read_transaction() as reader:
            initial_node = await reader.scalar(select(node_instance.c.id))
            initial_attempt = await reader.scalar(select(attempt.c.id))
        clock.advance(1)
        promotion, checked = await promotion_from_http(
            database, namespace_id, accepted, deployed, clock, identifiers
        )
        promoted = await promotion.promote(database)
        case = await admission_from_promoted_http(database, promotion, checked, policy)
        prepared = case.prepare(SELECTION)
        caps = PreparedGraphCaps(promotion.scope, None, None)
        outcomes: list[AttemptAdmissionState] = []
        async with database.transaction() as writer:
            outcomes.append(
                await admit_attempt(
                    await expiry_context(writer, promotion),
                    prepared,
                    SOURCE_LIMITS,
                    caps,
                    global_limit=1,
                    runtime_limit=1,
                    interval=INTERVAL,
                )
            )
        before_replay = await promotion.snapshot(database)
        clock.advance(7)
        replay = await client.post(RUNS, json={"params": {}}, headers=keyed)
        promoted_again = await promotion.promote(database)
        async with database.transaction() as writer:
            outcomes.append(
                await admit_attempt(
                    await expiry_context(writer, promotion),
                    prepared,
                    SOURCE_LIMITS,
                    caps,
                    global_limit=1,
                    runtime_limit=1,
                    interval=INTERVAL,
                )
            )
    after_replay = await promotion.snapshot(database)
    saved_start = await start_rows(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            saved_attempt = dict(rows.mappings().one())
        acquired = await acquire_claim_sources(reader, case.binding, SOURCE_LIMITS)

    assert first.status_code == 201
    assert (replay.status_code, replay.json()) == (200, first.json())
    assert accepted.graph_version_id == deployed.graph_version_id
    assert len(pending["run"]) == len(pending["receipt"]) == 1
    assert pending["run"][0]["status"] == "pending"
    assert pending["run"][0]["started_at"] is None
    assert initial_node is initial_attempt is None
    assert promoted is PromotionOutcome.APPLIED
    assert promoted_again is PromotionOutcome.UNCHANGED
    assert outcomes == [AttemptAdmissionState.QUEUED, AttemptAdmissionState.REFUSED]
    assert before_replay == after_replay
    assert saved_start["receipt"] == pending["receipt"]
    assert len(saved_start["run"]) == len(saved_start["run_inputs"]) == 1
    saved_run, nodes, events = after_replay
    assert saved_run["status"] == "running"
    assert saved_run["started_at"] == clock.now() - timedelta(seconds=7)
    assert pending["run"][0]["created_at"] == AuthorityClock().now()
    assert saved_run["needs_scheduling"] is True
    assert len(nodes) == 3
    assert {node["node_path"]: node["status"] for node in nodes} == {
        "agent": "queued",
        "gate": "pending",
        "approve": "pending",
    }
    agent = next(node for node in nodes if node["node_path"] == "agent")
    assert agent["current_attempt_id"] == saved_attempt["id"] == case.binding.attempt_id
    assert agent["attempt_count"] == 1
    assert saved_attempt["status"] == "created"
    assert saved_attempt["execution_state"] == "not_started"
    assert saved_attempt["lease_epoch"] == 0
    assert saved_attempt["price_table_id"] == initialization.baseline.id
    assert all(
        saved_attempt[name] is None
        for name in (
            "started_at",
            "deadline",
            "worker_id",
            "execution_domain_id",
            "lease_owner",
        )
    )
    assert acquired is not None
    assert decode_claim_sources(acquired).values["input_bindings"] == (
        '{"version":1,"inputs":[]}'
    )
    assert [
        event["kind"] for event in sorted(events, key=lambda event: str(event["id"]))
    ] == [
        "run.created",
        "run.started",
        "node.ready",
        "node.queued",
    ]
    assert ledger.outstanding == database.units_in_flight() == 0
