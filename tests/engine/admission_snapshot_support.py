import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from kora.api.exchange import ApiExchange
from kora.api.graph_deployment_limits import READ
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import inbound_event, node_instance, run
from kora.engine.admission_initialization import AdmissionInitialization
from kora.engine.admission_snapshot import (
    AcquiredAdmissionSnapshot,
    AdmissionSnapshotLimits,
    acquire_admission_snapshot,
)
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_snapshot import (
    AcquiredGraphSnapshot,
    GraphSnapshotLimits,
    acquire_graph_snapshot,
)
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import PromotionOutcome
from kora.ir.json import JsonObject
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, HARNESS, NAMESPACE, skeleton_ir
from tests.api.support import application, client_for, deployment_services
from tests.db.seed import FakeClock, fixed_randomness
from tests.engine.promotion_support import PromotionCase
from tests.engine.test_admission_initialization import rates
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS
from tests.plugins.authentication.support import AuthorityClock, seed_case

KEY = "http-admission-snapshot"
VERSIONS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/versions"
RUNS = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/runs"
LIMITS = AdmissionSnapshotLimits(
    READ, INPUT_LIMITS, 32_768, 100_000_000, 4096, 400_000_000, 400_000_000
)


def checked(
    acquired: AcquiredGraphSnapshot, pin: GraphVersionPin, scope: GraphResourceScope
) -> CheckedExecutionContract:
    return check_detached_execution_contract(
        acquired.graph,
        pin,
        scope,
        acquired.physical,
        acquired.owners,
        limits=LIMITS.graph,
    )


@dataclass(frozen=True)
class AdmissionSnapshotCase:
    app: ApiExchange
    headers: dict[str, str]
    promotion: PromotionCase
    node_id: str
    price_id: str
    policy: NamespacePolicyConfiguration

    async def publish(self, ir: JsonObject) -> DeployResponse:
        async with client_for(self.app) as client:
            response = await client.post(
                VERSIONS,
                json={"graph": GRAPH, "ir": ir, "resources": []},
                headers=self.headers,
            )
        assert response.status_code == 201
        return DeployResponse.model_validate(response.json())


async def snapshot_case(
    database: Database,
    preparer: DeploymentPreparer,
    capture: Capture,
    *,
    namespace_price: bool = True,
    ir: JsonObject | None = None,
    node_path: str = "agent",
) -> AdmissionSnapshotCase:
    identity = await seed_case(database, NAMESPACE)
    namespace_id = identity.principal.namespaces[0].namespace_id
    headers = {"Authorization": str(identity.request.authorization)}
    clock = FakeClock()
    identifiers = IdentifierGenerator(clock.now, fixed_randomness(71))
    policy = NamespacePolicyConfiguration.prepare(
        namespace_id, 1, {"max_pending_runs": 1000}
    )
    initialization = AdmissionInitialization(
        database,
        AuthorityClock(clock.now()),
        identifiers.new,
        policies=(policy,),
        prices=(
            PriceConfiguration(
                namespace_id if namespace_price else None,
                clock.now(),
                {"model": rates()},
            ),
        ),
    )
    await initialization.initialize(capture.diagnostics)
    app = application(
        database, capture, deployment_services(database, preparer, HARNESS)
    )
    async with client_for(app) as client:
        publication = await client.post(
            VERSIONS,
            json={
                "graph": GRAPH,
                "ir": skeleton_ir() if ir is None else ir,
                "resources": [],
            },
            headers=headers,
        )
        assert publication.status_code == 201
        deployed = DeployResponse.model_validate(publication.json())
        response = await client.post(
            RUNS, json={"params": {}}, headers=headers | {"Idempotency-Key": KEY}
        )
        assert response.status_code == 201
        accepted = AcceptedRun.model_validate(response.json())
    run_id = accepted.run_id.removeprefix("run_")
    version_id = accepted.graph_version_id.removeprefix("gv_")
    async with database.read_snapshot() as reader:
        graph_id = await reader.scalar(select(run.c.graph_id).where(run.c.id == run_id))
        receipt_id = await reader.scalar(
            select(inbound_event.c.id).where(
                inbound_event.c.namespace_id == namespace_id,
                inbound_event.c.idempotency_key == KEY,
            )
        )
        assert type(graph_id) is str
        assert type(receipt_id) is str
        scope = GraphResourceScope(namespace_id, graph_id, version_id)
        acquired = await acquire_graph_snapshot(
            reader,
            scope,
            GraphSnapshotLimits(
                READ, LIMITS.graph_native_bytes, LIMITS.owner_text_bytes
            ),
        )
    pin = GraphVersionPin(graph_id, version_id, deployed.execution_contract_digest)
    contract = checked(acquired, pin, scope)
    clock.advance(1)
    promotion = PromotionCase(
        scope,
        pin,
        prepare_run_materialization(
            contract, max_ir_bytes=len(contract.canonical_ir), max_top_level_nodes=3
        ),
        run_id,
        receipt_id,
        clock,
        identifiers,
    )
    assert await promotion.promote(database) is PromotionOutcome.APPLIED
    async with database.read_transaction() as reader:
        node_id = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == run_id, node_instance.c.node_path == node_path
            )
        )
    assert type(node_id) is str
    return AdmissionSnapshotCase(
        app, headers, promotion, node_id, initialization.prices[0].id, policy
    )


async def acquire(
    database: Database,
    case: AdmissionSnapshotCase,
    *,
    limits: AdmissionSnapshotLimits = LIMITS,
    periodic_recheck: bool = True,
) -> AcquiredAdmissionSnapshot:
    return await acquire_admission_snapshot(
        database,
        case.node_id,
        limits,
        observed_at=case.promotion.clock.now(),
        periodic_recheck=periodic_recheck,
        deadline=asyncio.get_running_loop().time() + 10.0,
    )
