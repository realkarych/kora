from dataclasses import dataclass, replace

from sqlalchemy import insert, select

from kora.db.claim_sources import ClaimSourceBinding
from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.run_input_reads import RunInputBinding, RunInputField
from kora.db.tables import inbound_event, node_instance, run, run_inputs
from kora.engine.attempt_preparation_budget import AttemptPreparationLimits
from kora.engine.claim_material import prepare_claim_material
from kora.engine.graph_cel_facts import GraphCelReadLimits, stage_graph_cel_facts
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    build_declared_command_manifests,
)
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    ExecutionContractReadLimits,
    check_detached_execution_contract,
    execution_contract_read_reservation,
)
from kora.engine.graph_output_inventory import build_graph_output_inventory
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_parameters import build_graph_parameter_binding
from kora.engine.graph_projection_paths import (
    GraphProjectionReadLimits,
    build_graph_cel_projection_paths,
)
from kora.engine.graph_resource_references import (
    GraphResourceReferenceLimits,
    GraphResourceScope,
    build_graph_resource_references,
)
from kora.engine.graph_retry_policies import build_graph_retry_policies
from kora.engine.graph_types import build_graph_type_contracts
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    GraphVersionPin,
    PreparedGraphVersionIdentity,
    graph_version_hash_bytes,
    prepare_graph_version_identity,
)
from kora.engine.locks import TransitionLocks
from kora.engine.promotion_admission import record_manual_promotion_admission
from kora.engine.readiness import prepare_readiness
from kora.engine.run_input_reads import read_run_input
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import promotion_scopes
from kora.engine.start_records import (
    AcceptedStart,
    StartReceipt,
    prepare_manual_start,
    start_receipt_document,
)
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.document import GraphDocument
from kora.ir.graph_cel_inventory import checked_graph_sites
from kora.ir.graph_json import ObjectReader, TextBudget, render_object
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_policy import prepare_node_policy
from kora.protocols.worker_claim_wire import MAX_IMMUTABLE_BYTES, MAX_NODE_POLICY_BYTES
from tests.db.seed import Seeder
from tests.engine.attempt_admission_support import (
    SELECTION,
    AttemptAdmissionCase,
    attempt_admission_case,
)
from tests.engine.graph_output_cases import child
from tests.engine.promotion_support import INPUT_SENTINEL, PromotionCase
from tests.engine.test_graph_execution_contract import complete_inputs, insert_case
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS
from tests.support.output_bindings import publication_binding

IR_BYTES = 2 * MAX_IMMUTABLE_BYTES
STAGE_BYTES = 16 * IR_BYTES
CEL = GraphCelReadLimits(
    IR_BYTES, STAGE_BYTES, IR_BYTES, IR_BYTES, 100, 0, STAGE_BYTES, STAGE_BYTES
)
PROJECTION = GraphProjectionReadLimits(
    CEL,
    IR_BYTES,
    9 * IR_BYTES + 8 * STAGE_BYTES,
    IR_BYTES,
    STAGE_BYTES,
    CEL.typed_bytes + 10 * IR_BYTES,
)
MANIFEST = CommandManifestLimits(IR_BYTES, 10 * IR_BYTES, 20 * IR_BYTES)
RESOURCES = GraphResourceReferenceLimits(
    MANIFEST, 20 * IR_BYTES, 40 * IR_BYTES, 80 * IR_BYTES
)
IDENTITY = GraphVersionIdentityLimits(
    2 * IR_BYTES, graph_version_hash_bytes(2 * IR_BYTES, 0)
)
READ = ExecutionContractReadLimits(
    IDENTITY, PROJECTION, RESOURCES, IR_BYTES, 1024 * IR_BYTES, 1024 * STAGE_BYTES**2
)


@dataclass(frozen=True)
class LargeAdmissionCase:
    case: AttemptAdmissionCase
    limits: AttemptPreparationLimits
    expected_bytes: int


def graph_sources(
    raw: JsonObject, scope: GraphResourceScope
) -> tuple[PreparedGraphVersionIdentity, GraphVersionPin, JsonObject]:
    text = render_object(raw, TextBudget(IR_BYTES))
    types = saved_array(build_graph_type_contracts(text, max_encoded_bytes=STAGE_BYTES))
    outputs = build_graph_output_inventory(
        text,
        types,
        binding=publication_binding(),
        max_encoded_bytes=STAGE_BYTES,
    )
    assert isinstance(outputs, str)
    document = GraphDocument.model_validate(raw)
    sites = checked_graph_sites(
        document,
        CEL.declarations,
        CEL.triggers,
        admission=CheckedInventoryAdmission(CEL.typed_work, CEL.typed_bytes),
    )
    assert isinstance(sites, tuple)
    staged: JsonObject = {
        "semantics": "kora.execution.v1",
        "type_contracts": types,
        "parameters": ObjectReader(
            build_graph_parameter_binding(document, max_encoded_bytes=STAGE_BYTES)
        ).object(),
        "outputs": saved_array(outputs),
        "retry_policies": ObjectReader(
            build_graph_retry_policies(text, max_encoded_bytes=STAGE_BYTES)
        ).object(),
        "cel_expressions": ObjectReader(
            stage_graph_cel_facts(sites, STAGE_BYTES).text
        ).object(),
    }
    del document, sites, outputs
    staged["cel_projection_paths"] = ObjectReader(
        build_graph_cel_projection_paths(text, staged, limits=PROJECTION).text
    ).object()
    staged["command_resources"] = ObjectReader(
        build_declared_command_manifests(text, limits=MANIFEST).text
    ).object()
    refs = ObjectReader(
        build_graph_resource_references(text, scope, (), limits=RESOURCES).text
    ).object()
    prepared = prepare_graph_version_identity(raw, staged, limits=IDENTITY)
    return (
        prepared,
        GraphVersionPin(
            scope.graph_id, scope.version_id, prepared.digests.execution_contract_digest
        ),
        refs,
    )


async def save_graph_and_run(
    database: Database, prototype: AttemptAdmissionCase, padding: str, size: int
) -> tuple[PromotionCase, CheckedExecutionContract]:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        graph_id = await seed.graph(prototype.policy.namespace_id, "large")
        scope = GraphResourceScope(
            prototype.policy.namespace_id, graph_id, seed.new_id()
        )
        run_id, receipt_id = seed.new_id(), seed.new_id()
    raw = complete_inputs(
        scope,
        resources=False,
        graph_settings={"overlap": "allow"},
        runtime_settings={"limits": {"timeout": "01h"}},
    )[0]
    config = child(raw, "graph", "nodes", "agent", "config")
    if padding == "immutable":
        config["prompt"] = "a" * size
    else:
        config["tool_policy"] = {"x-padding": "a" * size}
    prepared, pin, refs = graph_sources(raw, scope)
    del raw, config
    clock, identifiers = prototype.promotion.clock, prototype.promotion.identifiers
    identity = prepare_manual_start(scope.namespace_id, scope.graph_id, {})
    receipt = StartReceipt(
        version=1,
        kind="manual",
        namespace_id=scope.namespace_id,
        graph_id=scope.graph_id,
        request_digest=identity.request_digest,
        response=AcceptedStart(
            outcome="accepted",
            run_id=run_id,
            graph_version_id=scope.version_id,
            status="pending",
        ),
    )
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await insert_case(seed, scope, 1, prepared, refs)
        values = seed.run_values(
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            "pending",
            idempotency_key="maximum-claim",
        )
        values.update(
            id=run_id,
            trigger={
                "kind": "manual",
                "overlap": "allow",
                "occurrence_key": receipt_id,
            },
            limits={"timeout": "01h"},
            input_encoding_version=1,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
            started_at=None,
        )
        await writer.execute(insert(run).values(**values))
        await writer.execute(
            insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
        )
        receipt_values: dict[str, object] = dict(
            id=receipt_id,
            namespace_id=scope.namespace_id,
            name=f"kora.start:{scope.graph_id}",
            idempotency_key="maximum-claim",
            payload={},
            auth_kind="bearer",
            nonce=None,
            received_at=clock.now(),
            consumed_by=[],
            start_outcome=start_receipt_document(receipt),
            **seed.stamps(),
        )
        await writer.execute(insert(inbound_event).values(**receipt_values))
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        await locks.acquire_scopes(promotion_scopes(scope))
        await record_manual_promotion_admission(
            writer, locks, scope, values, receipt_values
        )
    del prepared, receipt_values, values, receipt
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=READ.identity.max_encoded_bytes,
        )
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, pin, scope, refs, (), limits=READ
    )
    del acquired
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=3
    )
    return PromotionCase(
        scope, pin, materialization, run_id, receipt_id, clock, identifiers
    ), checked


async def large_admission_case(
    database: Database, padding: str, extra: int
) -> LargeAdmissionCase:
    prototype = await attempt_admission_case(database)
    if padding == "immutable":
        material = prepare_claim_material(
            prototype.checked, prototype.params, "agent", {}, prototype.price, SELECTION
        )
        expected = MAX_IMMUTABLE_BYTES + extra
        size = expected - len(material.immutable.payload)
        del material
    else:
        expected = MAX_NODE_POLICY_BYTES + extra
        size = expected - len(prepare_node_policy({"x-padding": ""}))
    promotion, checked = await save_graph_and_run(database, prototype, padding, size)
    await promotion.promote(database)
    scope = promotion.scope
    async with database.read_transaction() as reader:
        node_id = await reader.scalar(
            select(node_instance.c.id).where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == "agent",
            )
        )
    assert isinstance(node_id, str)
    binding = ClaimSourceBinding(
        RunInputBinding(
            scope.namespace_id, scope.graph_id, scope.version_id, promotion.run_id
        ),
        node_id,
        promotion.identifiers.new(),
        prototype.price.id,
        "agent",
    )
    params = await read_run_input(
        database, binding.run, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert params is not None
    readiness = prepare_readiness(checked, max_ir_bytes=len(checked.canonical_ir))
    case = replace(
        prototype,
        promotion=promotion,
        checked=checked,
        readiness=readiness,
        params=params,
        binding=binding,
    )
    del prototype
    owned, _ = execution_contract_read_reservation(READ)
    limits = AttemptPreparationLimits(
        READ, owned + 16 * IR_BYTES, owned + 1024 * STAGE_BYTES, 1024 * STAGE_BYTES**2
    )
    return LargeAdmissionCase(case, limits, expected)
