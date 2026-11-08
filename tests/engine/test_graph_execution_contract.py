import gc
import json
import pickle
from dataclasses import replace
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert

from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.graph_version_reads import AcquiredGraphVersion, acquire_graph_version
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_execution_contract import (
    ExecutionContractReadLimits,
    ParameterSchemaReadLimits,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import build_graph_cel_projection_paths
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceScope,
    build_graph_resource_references,
)
from kora.engine.graph_version_identity import (
    CheckedGraphVersionIdentity,
    GraphVersionPin,
    PreparedGraphVersionIdentity,
    prepare_graph_version_identity,
)
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceKeyword,
    ParameterSchemaPosition,
    resolve_parameter_reference,
)
from kora.ir.json import JsonObject
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_output_cases import child, output_document
from tests.engine.graph_projection_cases import PROJECTION_LIMITS, projection_inputs
from tests.engine.test_graph_command_manifests import authored_manifest, resource
from tests.engine.test_graph_resource_references import (
    LIMITS as RESOURCE_LIMITS,
)
from tests.engine.test_graph_resource_references import (
    ROOT,
    SCOPE,
    VERSION_LIMITS,
    assert_refused,
    content,
    declared,
    physical_file,
)
from tests.engine.test_graph_types import custom

LIMITS = ExecutionContractReadLimits(
    VERSION_LIMITS,
    PROJECTION_LIMITS,
    RESOURCE_LIMITS,
    100_000,
    150_000_000,
    1_000_000_000_000_000,
)
MARKER = "protected-complete-contract-marker"
SCHEMA_LIMITS = ParameterSchemaReadLimits(100_000, 100_000, 100_000, 32, 4096)
NO_SCHEMA_WORK = replace(SCHEMA_LIMITS, work=0)


def complete_inputs(
    scope: GraphResourceScope,
    *,
    resources: bool = True,
    long_integer: bool = False,
    second_command: bool = False,
    parameter_reference: bool = False,
) -> tuple[JsonObject, JsonObject, JsonObject, tuple[GraphResourceOwner, ...]]:
    raw = output_document("command")
    graph = child(raw, "graph")
    graph["params"] = {"type": "object", "properties": {"enabled": {"type": "boolean"}}}
    nodes = child(graph, "nodes")
    command = child(nodes, "producer")
    agent = child(output_document("agent"), "graph", "nodes", "producer")
    approval = child(output_document("human_input"), "graph", "nodes", "producer")
    nodes["agent"] = agent
    nodes["approval"] = approval
    child(agent, "outputs").pop("value")
    child(command, "outputs").pop("value")
    command["depends_on"] = ["agent"]
    approval["depends_on"] = ["producer"]
    child(approval, "config")["approve_when"] = "response.decision == 'approve'"
    child(raw, "type_contracts")["AgentSession"] = {"builtin_revision": "1"}
    if parameter_reference:
        graph["params"] = {
            "$ref": "urn:kora:artifact:example:Parameter#/$defs/alias",
            "$defs": {"leaf": False},
        }
        child(raw, "type_contracts")["example/Parameter"] = custom(
            {"$defs": {"alias": {"$ref": "#/$defs/leaf"}, "leaf": True}}
        )
    raw["x-protected"] = MARKER
    if long_integer:
        raw["x-decimal"] = 10**800 + 71
    owners: tuple[GraphResourceOwner, ...] = ()
    if resources:
        child(command, "config").update(authored_manifest([resource("gate.sh")]))
        owners = (
            GraphResourceOwner(scope, ROOT, "gate.sh", content(incarnation=MARKER)),
        )
    if second_command:
        second = child(output_document("command"), "graph", "nodes", "producer")
        child(second, "outputs").pop("value")
        child(second, "config").update(authored_manifest([resource("gate.sh")]))
        nodes["second"] = second
        owners = (
            *owners,
            GraphResourceOwner(
                scope,
                ("graph", "nodes", "second"),
                "gate.sh",
                content(incarnation="another-object"),
            ),
        )
    ir_text, staged = projection_inputs(raw)
    projection = build_graph_cel_projection_paths(
        ir_text, staged, limits=LIMITS.projection
    )
    refs = build_graph_resource_references(
        ir_text, scope, owners, limits=LIMITS.resources
    )
    envelope: JsonObject = {
        "semantics": staged["semantics"],
        "type_contracts": staged["type_contracts"],
        "parameters": staged["parameters"],
        "outputs": staged["outputs"],
        "retry_policies": staged["retry_policies"],
        "cel_expressions": staged["cel_expressions"],
        "cel_projection_paths": ObjectReader(projection.text).object(),
        "command_resources": declared(ir_text),
    }
    assert len(refs.text) <= LIMITS.physical_input_bytes
    return raw, envelope, ObjectReader(refs.text).object(), owners


def prepared_case(
    raw: JsonObject, envelope: JsonObject, scope: GraphResourceScope
) -> tuple[PreparedGraphVersionIdentity, GraphVersionPin]:
    prepared = prepare_graph_version_identity(raw, envelope, limits=LIMITS.identity)
    return prepared, GraphVersionPin(
        scope.graph_id, scope.version_id, prepared.digests.execution_contract_digest
    )


def transport(
    prepared: PreparedGraphVersionIdentity, scope: GraphResourceScope
) -> AcquiredGraphVersion:
    pair, hashes = prepared.values, prepared.digests
    return AcquiredGraphVersion(
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        "ascii",
        (
            json.dumps(pair.ir).encode("ascii"),
            json.dumps(pair.execution_contract).encode("ascii"),
        ),
        (
            hashes.ir_digest.encode("ascii"),
            hashes.execution_contract_digest.encode("ascii"),
            hashes.deployment_digest.encode("ascii"),
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "semantics",
        "type_contracts",
        "parameters",
        "outputs",
        "retry_policies",
        "cel_expressions",
        "cel_projection_paths",
        "command_resources",
        "missing",
        "extra",
        "order",
    ],
)
def test_complete_reader_refuses_every_rehashed_inconsistent_member(field: str) -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE)
    bad_values: JsonObject = {
        "semantics": "kora.execution.v2",
        "parameters": {"interpretation": "kora.graph-parameters.v1", "schema": {}},
        "outputs": [],
        "retry_policies": {},
        "cel_expressions": {
            "interpretation": "kora.cel-expression-facts.v1",
            "fields": [],
        },
        "cel_projection_paths": {
            "interpretation": "kora.cel-projection-paths.v1",
            "parameters": {"whole": [], "presence": []},
            "outputs": [],
        },
        "command_resources": {
            "interpretation": "kora.command-resources.v1",
            "commands": [],
        },
    }
    if field == "missing":
        envelope.pop("command_resources")
    elif field == "extra":
        envelope["other"] = MARKER
    elif field == "order":
        envelope["semantics"] = envelope.pop("semantics")
    elif field == "type_contracts":
        first_type = cast(list[JsonObject], envelope["type_contracts"])[0]
        first_type["name"] = "OtherSession"
    else:
        envelope[field] = bad_values[field]
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)

    with pytest.raises(GraphValueRejected) as refusal:
        check_detached_execution_contract(
            acquired,
            pin,
            SCOPE,
            refs,
            owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SCHEMA_WORK,
        )

    reason = (
        GraphValueFailure.VERSION if field == "semantics" else GraphValueFailure.INVALID
    )
    assert_refused(refusal.value, reason)


def test_complete_reader_accepts_legacy_parameter_inner_order() -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE)
    parameters = child(envelope, "parameters")
    parameters["interpretation"] = parameters.pop("interpretation")
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)

    checked = check_detached_execution_contract(
        acquired, pin, SCOPE, refs, owners, limits=LIMITS
    )

    assert checked.canonical_contract == prepared.values.execution_contract
    assert checked.parameter_schema is None


def test_complete_reader_rejects_swapped_valid_refs_in_representational_graph() -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE, second_command=True)
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)
    checked = check_detached_execution_contract(
        acquired, pin, SCOPE, refs, owners, limits=LIMITS
    )
    first, second = physical_file(refs), physical_file(refs, command=1)
    first["storage_ref"], second["storage_ref"] = (
        second["storage_ref"],
        first["storage_ref"],
    )

    with pytest.raises(GraphValueRejected) as refusal:
        check_detached_execution_contract(
            acquired,
            pin,
            SCOPE,
            refs,
            owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SCHEMA_WORK,
        )

    accepted = ObjectReader(checked.resources.text).object()
    assert child(physical_file(accepted), "storage_ref")["object_incarnation"] == MARKER
    assert (
        child(physical_file(accepted, command=1), "storage_ref")["object_incarnation"]
        == "another-object"
    )
    assert_refused(refusal.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize(
    ("dimension", "units"),
    [("max_retained_bytes", 46_493_600), ("max_work", 40_298_819_301_597)],
)
def test_complete_reader_admits_exact_aggregate_and_refuses_one_short(
    dimension: Literal["max_retained_bytes", "max_work"],
    units: int,
) -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE, long_integer=True)
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)
    exact = replace(LIMITS, **{dimension: units})
    short = replace(exact, **{dimension: units - 1})
    broken = AcquiredGraphVersion(
        acquired.namespace_id,
        acquired.graph_id,
        acquired.id,
        acquired.encoding,
        (b'"', acquired.payloads[1]),
        acquired.digests,
    )
    outputs = cast(list[JsonObject], envelope["outputs"])

    checked = check_detached_execution_contract(
        acquired, pin, SCOPE, refs, owners, limits=exact
    )
    with pytest.raises(GraphValueRejected) as refusal:
        check_detached_execution_contract(
            acquired, pin, SCOPE, refs, owners, limits=short
        )
    with pytest.raises(GraphValueRejected) as before_decode:
        check_detached_execution_contract(
            broken, pin, SCOPE, refs, owners, limits=short
        )
    with pytest.raises(GraphValueRejected) as malformed:
        check_detached_execution_contract(
            broken, pin, SCOPE, refs, owners, limits=exact
        )

    assert checked.canonical_ir == prepared.values.ir
    assert len(outputs) > 1
    assert any(
        child(entry, "contract")["declared_type"] == "StructuredOutput"
        for entry in outputs
    )
    assert_refused(refusal.value, GraphValueFailure.BUDGET)
    assert_refused(before_decode.value, GraphValueFailure.BUDGET)
    assert_refused(malformed.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize("exhausted", [False, True], ids=["wrong-pin", "source-budget"])
def test_complete_reader_opaque_result_and_refusal_even_under_ambient_error(
    exhausted: bool,
) -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE)
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)
    requested_pin = pin if exhausted else replace(pin, execution_contract_digest=DIGEST)
    slot = "canonical_ir"
    acquired_slot = "_namespace_id"

    checked = check_detached_execution_contract(
        acquired,
        pin,
        SCOPE,
        refs,
        owners,
        limits=LIMITS,
        parameter_schema_limits=SCHEMA_LIMITS,
    )
    with pytest.raises(AttributeError, match="immutable"):
        setattr(checked, slot, MARKER)
    with pytest.raises(AttributeError, match="immutable"):
        delattr(checked, slot)
    with pytest.raises(AttributeError, match="immutable"):
        setattr(acquired, acquired_slot, MARKER)
    with pytest.raises(AttributeError, match="immutable"):
        delattr(acquired, acquired_slot)
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(checked)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(checked)
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        with pytest.raises(GraphValueRejected) as refusal:
            check_detached_execution_contract(
                acquired,
                requested_pin,
                SCOPE,
                refs,
                owners,
                limits=LIMITS,
                parameter_schema_limits=NO_SCHEMA_WORK,
            )

    assert MARKER not in repr(checked)
    assert checked.parameter_schema is not None
    assert MARKER not in repr(encoder.value)
    reason = GraphValueFailure.BUDGET if exhausted else GraphValueFailure.INVALID
    assert_refused(refusal.value, reason)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert set(frames["check_detached_execution_contract"]) == {"limits", "result"}
    assert "_checked_result" not in frames
    assert "_acquired_values" not in frames
    assert "_prepare_parameter_schema" not in frames


def test_complete_reader_releases_checked_identity_on_source_budget_refusal() -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE)
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)
    collecting = gc.isenabled()

    gc.disable()
    try:
        with pytest.raises(GraphValueRejected) as refusal:
            check_detached_execution_contract(
                acquired,
                pin,
                SCOPE,
                refs,
                owners,
                limits=LIMITS,
                parameter_schema_limits=NO_SCHEMA_WORK,
            )
        retained = any(
            type(candidate) is CheckedGraphVersionIdentity and candidate.pin is pin
            for candidate in gc.get_objects()
        )
    finally:
        if collecting:
            gc.enable()

    assert_refused(refusal.value, GraphValueFailure.BUDGET)
    assert not retained


async def insert_case(
    seeder: Seeder,
    scope: GraphResourceScope,
    number: int,
    prepared: PreparedGraphVersionIdentity,
    refs: JsonObject,
) -> None:
    pair, hashes = prepared.values, prepared.digests
    await seeder.connection.execute(
        insert(graph_version).values(
            id=scope.version_id,
            graph_id=scope.graph_id,
            number=number,
            ir=pair.ir,
            ir_digest=hashes.ir_digest,
            source_digest=DIGEST,
            execution_contract=pair.execution_contract,
            value_encoding_version=pair.value_encoding_version,
            execution_contract_digest=hashes.execution_contract_digest,
            deployment_digest=hashes.deployment_digest,
            resolved_digests={},
            resources=refs,
            deployed_by="owner",
            deployed_at=seeder.clock.now(),
            status="active",
            **seeder.stamps(),
        )
    )


@pytest.mark.asyncio
async def test_complete_reader_uses_exact_db_scope_and_checks_after_release(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        foreign = await seeder.namespace("other")
        scope, bad_scope, empty_scope = (
            GraphResourceScope(namespace, graph, seeder.new_id()) for _ in range(3)
        )
    raw, envelope, refs, owners = complete_inputs(scope, parameter_reference=True)
    good, pin = prepared_case(raw, envelope, scope)
    changed = dict(envelope)
    changed["outputs"] = []
    wrong, wrong_pin = prepared_case(raw, changed, bad_scope)
    bad_owners = tuple(
        GraphResourceOwner(bad_scope, owner.path, owner.name, owner.reference)
        for owner in owners
    )
    empty_raw, empty_envelope, empty_refs, empty_owners = complete_inputs(
        empty_scope, resources=False
    )
    empty, empty_pin = prepared_case(empty_raw, empty_envelope, empty_scope)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        await insert_case(seeder, scope, 1, good, refs)
        await insert_case(seeder, bad_scope, 2, wrong, refs)
        await insert_case(seeder, empty_scope, 3, empty, empty_refs)
    before_read = ledger.checkouts

    async with database.read_transaction() as connection:
        pairs = [
            await acquire_graph_version(
                connection,
                namespace,
                graph,
                selected.version_id,
                max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
            )
            for selected in (scope, bad_scope, empty_scope)
        ]
        native, bad_native, empty_native = [
            await graph_version_by_number(connection, namespace, graph, number)
            for number in (1, 2, 3)
        ]
    acquired, bad_acquired, empty_acquired = pairs
    assert acquired is not None
    assert bad_acquired is not None
    assert empty_acquired is not None
    assert native is not None
    assert bad_native is not None
    assert empty_native is not None
    assert ledger.checkouts > before_read
    assert ledger.outstanding == database.units_in_flight() == 0
    after_read = ledger.checkouts
    checked = check_detached_execution_contract(
        acquired,
        pin,
        scope,
        native.resources,
        owners,
        limits=LIMITS,
        parameter_schema_limits=SCHEMA_LIMITS,
    )
    arena = checked.parameter_schema
    assert arena is not None
    budget = ParameterReferenceBudget(100_000, 100_000, 100_000)
    alias = resolve_parameter_reference(
        arena,
        ParameterSchemaPosition(arena.root, arena.root),
        keyword=ParameterReferenceKeyword.REF,
        budget=budget,
    )
    leaf = resolve_parameter_reference(
        arena, alias, keyword=ParameterReferenceKeyword.REF, budget=budget
    )
    checked_empty = check_detached_execution_contract(
        empty_acquired,
        empty_pin,
        empty_scope,
        empty_native.resources,
        empty_owners,
        limits=LIMITS,
    )
    with pytest.raises(GraphValueRejected) as invalid_output:
        check_detached_execution_contract(
            bad_acquired,
            wrong_pin,
            bad_scope,
            bad_native.resources,
            bad_owners,
            limits=LIMITS,
        )
    with pytest.raises(GraphValueRejected) as invalid_namespace:
        check_detached_execution_contract(
            empty_acquired,
            empty_pin,
            replace(empty_scope, namespace_id=foreign),
            empty_native.resources,
            empty_owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SCHEMA_WORK,
        )
    await database.close()

    assert acquired.namespace_id == scope.namespace_id
    assert checked.pin == pin
    assert checked.scope == scope
    assert checked_empty.scope == empty_scope
    assert checked_empty.parameter_schema is None
    assert checked.canonical_contract == good.values.execution_contract
    assert ObjectReader(checked.resources.text).object() == refs
    assert leaf.document_root == alias.document_root != arena.root
    assert arena.nodes[leaf.node].value is True
    assert_refused(invalid_output.value, GraphValueFailure.INVALID)
    assert_refused(invalid_namespace.value, GraphValueFailure.INVALID)
    assert ledger.checkouts == after_read
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
