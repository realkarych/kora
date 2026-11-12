import gc
import json
import pickle
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert, select, update

from kora.db.connection import Database, Dialect
from kora.db.graph_resource_owner_reads import acquire_graph_resource_owners
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.graph_version_reads import (
    AcquiredGraphVersion,
    TransportEncoding,
    acquire_graph_version,
)
from kora.db.reads import graph_version_by_number
from kora.db.tables import attempt, graph_version, node_instance
from kora.db.tables import graph_version_resource_owner as owner_table
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    ExecutionContractReadLimits,
    ParameterSchemaReadLimits,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import build_graph_cel_projection_paths
from kora.engine.graph_resource_references import (
    CheckedNativeGraphResourceReferences,
    GraphResourceOwner,
    GraphResourceScope,
    NativeGraphResourceTransport,
    NativeResourceEncoding,
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
    schema_member,
)
from kora.engine.run_materialization import (
    InitialNode,
    MaterializationFailure,
    MaterializationRejected,
    PreparedRunMaterialization,
    prepare_initial_node_rows,
    prepare_run_materialization,
)
from kora.ir.json import JsonObject
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, START, Seeder
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
from tests.engine.test_parameter_schema_selection import named_roots

LIMITS = ExecutionContractReadLimits(
    VERSION_LIMITS,
    PROJECTION_LIMITS,
    RESOURCE_LIMITS,
    100_000,
    150_000_000,
    1_000_000_000_000_000,
)
MARKER = "protected-complete-contract-marker"
PARAMETER_MARKER = "private_parameter_canary"
SCHEMA_LIMITS = ParameterSchemaReadLimits(
    100_000, 100_000, 100_000, 32, 4096, 100_000, 100_000, 100_000
)
NO_SCHEMA_WORK = replace(SCHEMA_LIMITS, work=0)
NO_SELECTION_WORK = replace(SCHEMA_LIMITS, selection_work=0)


class NonNativeIdentifier(str):
    __slots__ = ()


class RaisingNativeText(str):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        raise ValueError(MARKER)

    def __ne__(self, other: object) -> bool:
        raise ValueError(MARKER)


def complete_inputs(
    scope: GraphResourceScope,
    *,
    resources: bool = True,
    long_integer: bool = False,
    second_command: bool = False,
    parameter_reads: bool = False,
    parameter_reference: bool = False,
    graph_settings: JsonObject | None = None,
    runtime_settings: JsonObject | None = None,
    enabled_schema: JsonObject | None = None,
    enabled_predicate: str = "params.enabled",
) -> tuple[JsonObject, JsonObject, JsonObject, tuple[GraphResourceOwner, ...]]:
    raw = output_document("command")
    graph = child(raw, "graph")
    graph["params"] = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"} if enabled_schema is None else enabled_schema
        },
    }
    if graph_settings is not None:
        graph.update(graph_settings)
    if runtime_settings is not None:
        child(raw, "runtime").update(runtime_settings)
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
    if parameter_reads:
        schema = child(graph, "params")
        child(schema, "properties").update(
            {PARAMETER_MARKER: {"type": "string"}, "dormant": {"type": "boolean"}}
        )
        schema["required"] = ["enabled", "dormant"]
        child(approval, "config")["approve_when"] = (
            f"({enabled_predicate}) && has(params.{PARAMETER_MARKER}) && "
            "response.decision == 'approve'"
        )
        child(raw, "runtime")["infrastructure_retry"] = {
            "max_attempts": 1,
            "predicate": "params.dormant",
        }
    child(raw, "type_contracts")["AgentSession"] = {"builtin_revision": "1"}
    if parameter_reference:
        child(graph, "params")["$defs"] = {
            "selected": {"$ref": "urn:kora:artifact:example:Parameter#/$defs/alias"},
            "leaf": False,
        }
        child(raw, "type_contracts")["example/Parameter"] = custom(
            {"$defs": {"alias": {"$ref": "#/$defs/leaf"}, "leaf": True}}
        )
        child(raw, "type_contracts")["example/Unselected"] = custom(
            {"$ref": "#/$defs/actual", "$defs": {"actual": True, "second": True}}
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


class PinComparisonText(str):
    def __ne__(self, other: object) -> bool:
        return False


@pytest.mark.parametrize(
    "field", ["graph_id", "version_id", "execution_contract_digest"]
)
def test_complete_reader_refuses_non_native_pin_strings(field: str) -> None:
    raw, envelope, refs, owners = complete_inputs(SCOPE, parameter_reads=True)
    prepared, pin = prepared_case(raw, envelope, SCOPE)
    acquired = transport(prepared, SCOPE)
    wrong = replace(pin, **{field: PinComparisonText(DIGEST)})

    with pytest.raises(GraphValueRejected) as refusal:
        check_detached_execution_contract(
            acquired, wrong, SCOPE, refs, owners, limits=LIMITS
        )

    assert_refused(refusal.value, GraphValueFailure.INVALID)


def test_complete_reader_parameter_paths_preserve_the_checked_identity() -> None:
    scope = replace(SCOPE)
    raw, envelope, refs, owners = complete_inputs(scope, parameter_reads=True)
    prepared, pin = prepared_case(raw, envelope, scope)
    acquired = transport(prepared, scope)
    expected_scope = replace(scope)
    expected_pin = replace(pin)

    checked = check_detached_execution_contract(
        acquired, pin, scope, refs, owners, limits=LIMITS
    )
    paths = checked.parameter_paths
    returned_scope, returned_pin = checked.scope, checked.pin
    with pytest.raises(AttributeError, match="immutable"):
        paths.whole = (MARKER,)
    with pytest.raises(AttributeError, match="immutable"):
        delattr(paths, "presence")
    with pytest.raises(AttributeError, match="immutable"):
        paths.__init__((MARKER,), ())
    with pytest.raises(AttributeError, match="immutable"):
        checked.__init__(scope, pin, MARKER, MARKER, checked.resources, paths)
    with pytest.raises(TypeError, match="requires the complete checking reader"):
        CheckedExecutionContract(scope, pin, MARKER, MARKER, checked.resources, paths)
    with pytest.raises(TypeError, match="require explicit access") as pickling:
        pickle.dumps(paths)
    with pytest.raises(ValueError, match="not iterable") as encoding:
        jsonable_encoder(paths)
    scope.__init__(MARKER, MARKER, MARKER)
    pin.__init__(MARKER, MARKER, MARKER)
    returned_scope.__init__(MARKER, MARKER, MARKER)
    returned_pin.__init__(MARKER, MARKER, MARKER)

    assert checked.scope == expected_scope
    assert checked.pin == expected_pin
    assert paths.whole == (".enabled",)
    assert paths.presence == (f".{PARAMETER_MARKER}",)
    assert PARAMETER_MARKER not in repr(paths)
    assert PARAMETER_MARKER not in repr(pickling.value)
    assert PARAMETER_MARKER not in repr(encoding.value)


async def insert_case(
    seeder: Seeder,
    scope: GraphResourceScope,
    number: int,
    prepared: PreparedGraphVersionIdentity,
    refs: JsonObject,
) -> None:
    pair, hashes = prepared.values, prepared.digests
    await seeder.graph_version(
        scope.graph_id,
        number,
        pair.ir,
        version_id=scope.version_id,
        execution_contract=pair.execution_contract,
        value_encoding_version=pair.value_encoding_version,
        ir_digest=hashes.ir_digest,
        execution_contract_digest=hashes.execution_contract_digest,
        deployment_digest=hashes.deployment_digest,
        resources=refs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("older_policy", ["allow", "skip", "queue"])
async def test_run_materialization_keeps_the_exact_saved_root_inventory(
    database: Database, older_policy: str
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        old_scope = GraphResourceScope(namespace, graph, seeder.new_id())
        new_scope = GraphResourceScope(namespace, graph, seeder.new_id())
    producer = child(output_document("command"), "graph", "nodes", "producer")
    newer_producer = child(output_document("command"), "graph", "nodes", "producer")
    old_raw, old_envelope, old_refs, old_owners = complete_inputs(
        old_scope,
        resources=False,
        graph_settings={
            "overlap": older_policy,
            "nodes": {
                "wrapper": {
                    "kind": "subgraph",
                    "config": {"body": "body"},
                    "trigger_rule": "all_done",
                },
                "producer": producer,
            },
            "subgraphs": {
                "body": {
                    "nodes": {
                        "nested_only": {"kind": "timer", "config": {"duration": "1s"}}
                    }
                }
            },
        },
        runtime_settings={"limits": {"timeout": "001h"}},
    )
    new_raw, new_envelope, new_refs, new_owners = complete_inputs(
        new_scope,
        resources=False,
        graph_settings={
            "nodes": {
                "later": {"kind": "timer", "config": {"duration": "2s"}},
                "producer": newer_producer,
            }
        },
    )
    old_prepared, old_pin = prepared_case(old_raw, old_envelope, old_scope)
    new_prepared, new_pin = prepared_case(new_raw, new_envelope, new_scope)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        await insert_case(seeder, old_scope, 1, old_prepared, old_refs)
        await insert_case(seeder, new_scope, 2, new_prepared, new_refs)
    async with database.read_transaction() as connection:
        old = await acquire_graph_version(
            connection,
            namespace,
            graph,
            old_scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        new = await acquire_graph_version(
            connection,
            namespace,
            graph,
            new_scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        native_old = await graph_version_by_number(connection, namespace, graph, 1)
        native_new = await graph_version_by_number(connection, namespace, graph, 2)
    assert old is not None
    assert new is not None
    assert native_old is not None
    assert native_new is not None
    old_checked = check_detached_execution_contract(
        old,
        old_pin,
        old_scope,
        native_old.resources,
        old_owners,
        limits=LIMITS,
    )
    new_checked = check_detached_execution_contract(
        new,
        new_pin,
        new_scope,
        native_new.resources,
        new_owners,
        limits=LIMITS,
    )
    before = ledger.checkouts
    exact_bytes = len(old_checked.canonical_ir)

    previous = prepare_run_materialization(
        old_checked,
        max_ir_bytes=exact_bytes,
        max_top_level_nodes=4,
    )
    later = prepare_run_materialization(
        new_checked,
        max_ir_bytes=len(new_checked.canonical_ir),
        max_top_level_nodes=4,
    )
    with pytest.raises(MaterializationRejected) as bytes_refusal:
        prepare_run_materialization(
            old_checked,
            max_ir_bytes=exact_bytes - 1,
            max_top_level_nodes=4,
        )
    with pytest.raises(MaterializationRejected) as nodes_refusal:
        prepare_run_materialization(
            old_checked,
            max_ir_bytes=exact_bytes,
            max_top_level_nodes=3,
        )
    with pytest.raises(AttributeError, match="immutable"):
        previous.nodes = later.nodes
    with pytest.raises(AttributeError, match="immutable"):
        delattr(previous, "timeout")
    with pytest.raises(AttributeError, match="immutable"):
        previous.manual_overlap = "allow"
    with pytest.raises(TypeError, match="requires checked preparation"):
        PreparedRunMaterialization(
            object(), (MARKER, MARKER, MARKER, MARKER), later.nodes, None, "allow"
        )
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(previous)
    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(previous)

    assert previous.nodes == (
        InitialNode("wrapper", "subgraph", "all_done"),
        InitialNode("producer", "command", "all_success"),
        InitialNode("agent", "agent", "all_success"),
        InitialNode("approval", "human_input", "all_success"),
    )
    assert later.nodes == (
        InitialNode("later", "timer", "all_success"),
        *previous.nodes[1:],
    )
    assert previous.timeout == "001h"
    assert later.timeout is None
    assert previous.manual_overlap == older_policy
    assert later.manual_overlap == "allow"
    assert previous.matches(old_scope, old_pin)
    assert later.matches(new_scope, new_pin)
    assert not previous.matches(replace(old_scope, namespace_id=MARKER), old_pin)
    assert not previous.matches(
        replace(old_scope, graph_id=MARKER), replace(old_pin, graph_id=MARKER)
    )
    assert not previous.matches(
        new_scope, replace(old_pin, version_id=new_scope.version_id)
    )
    assert not previous.matches(
        old_scope, replace(old_pin, execution_contract_digest=MARKER)
    )
    assert not previous.matches(
        old_scope, replace(old_pin, graph_id=NonNativeIdentifier(graph))
    )
    assert "wrapper" not in repr(previous)
    assert (
        bytes_refusal.value.reason
        is nodes_refusal.value.reason
        is MaterializationFailure.CAPACITY
    )
    assert ledger.checkouts == before
    assert ledger.outstanding == database.units_in_flight() == 0


class OutOfRangeOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(days=1)


@pytest.mark.asyncio
async def test_initial_node_rows_bind_the_exact_saved_graph_and_native_state(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seeder.new_id())
    producer = child(output_document("command"), "graph", "nodes", "producer")
    raw, envelope, refs, owners = complete_inputs(
        scope,
        resources=False,
        graph_settings={
            "nodes": {
                "wrapper": {
                    "kind": "subgraph",
                    "config": {"body": "body"},
                    "trigger_rule": "all_done",
                },
                "producer": producer,
            },
            "subgraphs": {
                "body": {
                    "nodes": {
                        "nested_only": {"kind": "timer", "config": {"duration": "1s"}}
                    }
                }
            },
        },
    )
    prepared, pin = prepared_case(raw, envelope, scope)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        await insert_case(seeder, scope, 1, prepared, refs)
        run_id = await seeder.run(namespace_id, graph_id, scope.version_id, "pending")
        ids = tuple(seeder.new_id() for _ in range(4))
    async with database.read_transaction() as connection:
        acquired = await acquire_graph_version(
            connection,
            namespace_id,
            graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        native = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert acquired is not None
    assert native is not None
    checked = check_detached_execution_contract(
        acquired, pin, scope, native.resources, owners, limits=LIMITS
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=4
    )
    unissued = object.__new__(PreparedRunMaterialization)
    checkouts = ledger.checkouts
    local = timezone(timedelta(hours=5, minutes=30))
    aware_stamp = START.astimezone(local)
    overflow = datetime.min.replace(tzinfo=local)
    invalid_cases = (
        (scope, pin, run_id, ids[:-1], aware_stamp),
        (scope, pin, run_id, (*ids, MARKER), aware_stamp),
        (scope, pin, run_id, (ids[0], *ids[:-1]), aware_stamp),
        (scope, pin, run_id, (MARKER, *ids[1:]), aware_stamp),
        (scope, pin, run_id, (run_id, *ids[1:]), aware_stamp),
        (scope, pin, run_id, (scope.version_id, *ids[1:]), aware_stamp),
        (scope, pin, MARKER, ids, aware_stamp),
        (
            scope,
            replace(pin, execution_contract_digest=MARKER),
            run_id,
            ids,
            aware_stamp,
        ),
        (replace(scope, namespace_id=MARKER), pin, run_id, ids, aware_stamp),
        (scope, pin, run_id, ids, aware_stamp.replace(tzinfo=None)),
        (scope, pin, run_id, ids, overflow),
        (scope, pin, run_id, ids, START.replace(tzinfo=OutOfRangeOffset())),
        (scope, pin, run_id, ids, datetime(999, 1, 1, tzinfo=UTC)),
    )

    rows = prepare_initial_node_rows(
        materialization, scope, pin, run_id, ids, aware_stamp
    )
    for (
        selected_scope,
        selected_pin,
        selected_run,
        selected_ids,
        selected_time,
    ) in invalid_cases:
        with pytest.raises(MaterializationRejected) as rejected:
            prepare_initial_node_rows(
                materialization,
                selected_scope,
                selected_pin,
                selected_run,
                selected_ids,
                selected_time,
            )
        assert rejected.value.reason is MaterializationFailure.BINDING
        assert rejected.value.__context__ is rejected.value.__cause__ is None
        assert [
            set(frame.f_locals)
            for frame, _ in walk_tb(rejected.value.__traceback__)
            if frame.f_code.co_name == "prepare_initial_node_rows"
        ] == [set()]
    with pytest.raises(MaterializationRejected) as unissued_refusal:
        prepare_initial_node_rows(unissued, scope, pin, run_id, ids, aware_stamp)
    assert ledger.checkouts == checkouts
    assert [row["id"] for row in rows] == list(ids)
    assert len({id(row["consumed_versions"]) for row in rows}) == len(rows)
    async with database.transaction() as connection:
        observed_before = (await connection.execute(select(node_instance.c.id))).all()
        await connection.execute(insert(node_instance), rows)
        result = await connection.execute(
            select(node_instance).order_by(node_instance.c.id)
        )
        stored = tuple(dict(row) for row in result.mappings())
        physical_attempts = (await connection.execute(select(attempt.c.id))).all()
        null_selections = (
            (
                await connection.execute(
                    select(node_instance.c.id)
                    .where(node_instance.c.branch_selection.is_(None))
                    .order_by(node_instance.c.id)
                )
            )
            .scalars()
            .all()
        )
        await connection.rollback()
    async with database.read_transaction() as connection:
        remaining = (await connection.execute(select(node_instance.c.id))).all()

    assert observed_before == physical_attempts == remaining == []
    assert unissued_refusal.value.reason is MaterializationFailure.BINDING
    assert [row["id"] for row in stored] == list(ids)
    assert list(null_selections) == list(ids)
    assert [(row["node_path"], row["kind"], row["trigger_rule"]) for row in stored] == [
        ("wrapper", "subgraph", "all_done"),
        ("producer", "command", "all_success"),
        ("agent", "agent", "all_success"),
        ("approval", "human_input", "all_success"),
    ]
    for row in stored:
        assert row["run_id"] == run_id
        assert row["status"] == "pending"
        assert row["subgraph_attempt"] == 1
        assert row["top_level"] is True
        assert row["consumed_versions"] == {}
        assert row["attempt_count"] == 0
        assert row["created_at"] == row["updated_at"] == START
        assert all(
            row[field] is None
            for field in (
                "current_attempt_id",
                "branch_selection",
                "started_at",
                "deadline_expired_at",
                "provider_wait_deadline",
            )
        )
    assert ledger.outstanding == database.units_in_flight() == 0


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
        scope, bad_scope, empty_scope, drift_scope, tampered_scope = (
            GraphResourceScope(namespace, graph, seeder.new_id()) for _ in range(5)
        )
    raw, envelope, refs, owners = complete_inputs(
        scope, parameter_reads=True, parameter_reference=True
    )
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
    drift = dict(envelope)
    drift["cel_projection_paths"] = {
        **child(envelope, "cel_projection_paths"),
        "parameters": {"whole": [".dormant"], "presence": [f".{PARAMETER_MARKER}"]},
    }
    drifted, drift_pin = prepared_case(raw, drift, drift_scope)
    drift_owners = tuple(
        GraphResourceOwner(drift_scope, owner.path, owner.name, owner.reference)
        for owner in owners
    )
    tampered_raw, tampered_envelope, tampered_refs, tampered_owners = complete_inputs(
        tampered_scope, resources=False, parameter_reference=True
    )
    unselected = next(
        row
        for row in cast("list[JsonObject]", tampered_envelope["type_contracts"])
        if row["name"] == "example/Unselected"
    )
    binding = cast("list[JsonObject]", unselected["references"])[0]
    binding["target_path"] = ["$defs", "second"]
    tampered, tampered_pin = prepared_case(
        tampered_raw, tampered_envelope, tampered_scope
    )
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        await insert_case(seeder, scope, 1, good, refs)
        await insert_case(seeder, bad_scope, 2, wrong, refs)
        await insert_case(seeder, empty_scope, 3, empty, empty_refs)
        await insert_case(seeder, drift_scope, 4, drifted, refs)
        await insert_case(seeder, tampered_scope, 5, tampered, tampered_refs)
        await connection.execute(
            insert(owner_table),
            [
                {
                    "graph_version_id": scope.version_id,
                    "ordinal": ordinal,
                    "subgraph_name": None,
                    "node_name": item.path[-1],
                    "file_name": item.name,
                    "store_id": item.reference.address.storage_identity.store_id,
                    "payload_digest": item.reference.address.payload_digest,
                    "object_incarnation": item.reference.object_incarnation,
                    "total_size": item.reference.total_size,
                    "payload_state": "expiring",
                }
                for ordinal, item in enumerate(owners)
            ],
        )
    before_read = ledger.checkouts

    async with database.read_snapshot() as connection:
        pairs = [
            await acquire_graph_version(
                connection,
                namespace,
                graph,
                selected.version_id,
                max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
            )
            for selected in (scope, bad_scope, empty_scope, drift_scope, tampered_scope)
        ]
        native, bad_native, empty_native, drift_native, tampered_native = [
            await graph_version_by_number(connection, namespace, graph, number)
            for number in (1, 2, 3, 4, 5)
        ]
        physical_field = graph_resource_binary_projection(
            database.dialect, graph_version.c.resources
        )
        physical_wire: bytes = (
            await connection.execute(
                select(physical_field).where(graph_version.c.id == scope.version_id)
            )
        ).scalar_one()
        encoding_query = (
            "SHOW server_encoding"
            if database.dialect is Dialect.POSTGRESQL
            else "PRAGMA main.encoding"
        )
        native_encoding: object = (
            await connection.exec_driver_sql(encoding_query)
        ).scalar_one()
        async with database.transaction() as writer:
            await writer.execute(
                update(owner_table)
                .where(owner_table.c.graph_version_id == scope.version_id)
                .values(payload_state="expired")
            )
        owner_batch = await acquire_graph_resource_owners(
            connection, scope.version_id, max_text_bytes=LIMITS.physical_input_bytes
        )
        empty_batch = await acquire_graph_resource_owners(
            connection,
            empty_scope.version_id,
            max_text_bytes=LIMITS.physical_input_bytes,
        )
    async with database.read_snapshot() as connection:
        fresh_batch = await acquire_graph_resource_owners(
            connection, scope.version_id, max_text_bytes=LIMITS.physical_input_bytes
        )
    acquired, bad_acquired, empty_acquired, drift_acquired, tampered_acquired = pairs
    assert acquired is not None
    assert bad_acquired is not None
    assert empty_acquired is not None
    assert drift_acquired is not None
    assert native is not None
    assert bad_native is not None
    assert empty_native is not None
    assert drift_native is not None
    assert tampered_acquired is not None
    assert tampered_native is not None
    assert ledger.checkouts > before_read
    assert ledger.outstanding == database.units_in_flight() == 0
    assert native_encoding == (
        "UTF8" if database.dialect is Dialect.POSTGRESQL else "UTF-8"
    )
    assert (
        len(physical_wire)
        + sum(
            len(cell) for row in owner_batch.rows for cell in row if type(cell) is bytes
        )
        <= LIMITS.physical_input_bytes
    )
    after_read = ledger.checkouts
    checked = check_detached_execution_contract(
        acquired,
        pin,
        scope,
        NativeGraphResourceTransport(physical_wire, "utf-8"),
        owner_batch,
        limits=LIMITS,
        parameter_schema_limits=SCHEMA_LIMITS,
    )
    assert type(checked.resources) is CheckedNativeGraphResourceReferences
    assert checked.resources.owner_states == ("expiring",)
    assert tuple(row[-1] for row in fresh_batch.rows) == (b"expired",)
    arena = checked.parameter_schema
    assert arena is not None
    budget = ParameterReferenceBudget(100_000, 100_000, 100_000)
    definitions = schema_member(arena.nodes, arena.nodes[arena.root], "$defs", budget)
    assert definitions is not None
    selected = schema_member(arena.nodes, arena.nodes[definitions], "selected", budget)
    assert selected is not None
    alias = resolve_parameter_reference(
        arena,
        ParameterSchemaPosition(arena.root, selected),
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
    wrong_wire = json.dumps(empty_native.resources).encode("utf-16le")
    with pytest.raises(GraphValueRejected) as invalid_encoding:
        check_detached_execution_contract(
            empty_acquired,
            empty_pin,
            empty_scope,
            NativeGraphResourceTransport(wrong_wire, "utf-16le"),
            empty_batch,
            limits=LIMITS,
        )
    empty_wire = json.dumps(empty_native.resources).encode("utf-8")
    forged_codec = RaisingNativeText("utf-8")
    unsupported_codec = MARKER + "-codec"
    malformed_pair, unsupported_pair = (
        AcquiredGraphVersion(
            empty_acquired.namespace_id,
            empty_acquired.graph_id,
            empty_acquired.id,
            cast(TransportEncoding, label),
            empty_acquired.payloads,
            empty_acquired.digests,
        )
        for label in (forged_codec, unsupported_codec)
    )
    native_empty = NativeGraphResourceTransport(empty_wire, "utf-8")
    malformed_inputs = (
        (
            empty_acquired,
            empty_scope,
            NativeGraphResourceTransport(
                empty_wire, cast(NativeResourceEncoding, forged_codec)
            ),
        ),
        (malformed_pair, empty_scope, native_empty),
        (
            unsupported_pair,
            empty_scope,
            NativeGraphResourceTransport(
                empty_wire, cast(NativeResourceEncoding, unsupported_codec)
            ),
        ),
        (
            empty_acquired,
            replace(empty_scope, graph_id=RaisingNativeText(empty_scope.graph_id)),
            native_empty,
        ),
    )
    for pair, candidate_scope, candidate_wire in malformed_inputs:
        with pytest.raises(GraphValueRejected) as malformed_native:
            check_detached_execution_contract(
                pair,
                empty_pin,
                candidate_scope,
                candidate_wire,
                empty_batch,
                limits=LIMITS,
            )
        assert_refused(malformed_native.value, GraphValueFailure.INVALID)
    with pytest.raises(GraphValueRejected) as invalid_output:
        check_detached_execution_contract(
            bad_acquired,
            wrong_pin,
            bad_scope,
            bad_native.resources,
            bad_owners,
            limits=LIMITS,
        )
    with pytest.raises(GraphValueRejected) as invalid_parameters:
        check_detached_execution_contract(
            drift_acquired,
            drift_pin,
            drift_scope,
            drift_native.resources,
            drift_owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SELECTION_WORK,
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
    with pytest.raises(GraphValueRejected) as exhausted_selection:
        check_detached_execution_contract(
            acquired,
            pin,
            scope,
            native.resources,
            owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SELECTION_WORK,
        )
    with pytest.raises(GraphValueRejected) as invalid_unselected:
        check_detached_execution_contract(
            tampered_acquired,
            tampered_pin,
            tampered_scope,
            tampered_native.resources,
            tampered_owners,
            limits=LIMITS,
            parameter_schema_limits=NO_SELECTION_WORK,
        )
    await database.close()

    assert acquired.namespace_id == scope.namespace_id
    assert_refused(invalid_encoding.value, GraphValueFailure.INVALID)
    assert checked.pin == pin
    assert checked.scope == scope
    assert checked_empty.scope == empty_scope
    assert checked_empty.parameter_schema is None
    assert checked.canonical_contract == good.values.execution_contract
    assert checked.parameter_paths.whole == (".enabled",)
    assert checked.parameter_paths.presence == (f".{PARAMETER_MARKER}",)
    assert checked_empty.parameter_paths.whole == ()
    assert checked_empty.parameter_paths.presence == ()
    assert ObjectReader(checked.resources.text).object() == refs
    assert named_roots(arena) == ["example/Parameter"]
    assert leaf.document_root == alias.document_root != arena.root
    assert arena.nodes[leaf.node].value is True
    assert_refused(invalid_output.value, GraphValueFailure.INVALID)
    assert_refused(invalid_parameters.value, GraphValueFailure.INVALID)
    assert PARAMETER_MARKER not in repr(invalid_parameters.value)
    assert_refused(invalid_namespace.value, GraphValueFailure.INVALID)
    assert_refused(exhausted_selection.value, GraphValueFailure.BUDGET)
    assert_refused(invalid_unselected.value, GraphValueFailure.INVALID)
    assert ledger.checkouts == after_read
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
