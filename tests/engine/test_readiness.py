from dataclasses import FrozenInstanceError, replace

import pytest

from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.engine.graph_command_manifests import build_declared_command_manifests
from kora.engine.graph_execution_contract import (
    MEMBERS,
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import build_graph_cel_projection_paths
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    build_graph_resource_references,
)
from kora.engine.readiness import ReadinessRejected, prepare_readiness
from kora.ir.graph_json import ObjectReader
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.graph_output_cases import child, output_document
from tests.engine.graph_projection_cases import projection_inputs
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    MARKER,
    insert_case,
    prepared_case,
)

PRIVATE: str = "private-readiness-material"


async def readiness_source(
    database: Database,
    *,
    reverse: bool = False,
    separate_edges: bool = False,
    input_source: str = "agent",
    disconnected: bool = False,
    rules: dict[str, str] | None = None,
    parameter_input: bool = False,
) -> CheckedExecutionContract:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace("readiness-" + seed.new_id())
        graph = await seed.graph(namespace)
        scope = GraphResourceScope(namespace, graph, seed.new_id())
    raw = output_document("command")
    graph = child(raw, "graph")
    graph["params"] = {
        "type": "object",
        "properties": {"enabled": {"type": "boolean"}},
    }
    nodes = child(graph, "nodes")
    agent = child(output_document("agent"), "graph", "nodes", "producer")
    approval = child(output_document("human_input"), "graph", "nodes", "producer")
    nodes["agent"] = agent
    nodes["approval"] = approval
    child(agent, "outputs").pop("value")
    child(nodes, "producer", "outputs").pop("value")
    child(nodes, "producer")["depends_on"] = ["agent"]
    approval["depends_on"] = ["producer"]
    child(approval, "config")["approve_when"] = "response.decision == 'approve'"
    child(raw, "type_contracts")["AgentSession"] = {"builtin_revision": "1"}
    raw["x-protected"] = MARKER
    raw["x-private"] = PRIVATE
    child(raw, "type_contracts")["VcsRef"] = {"builtin_revision": "1"}
    child(nodes, "agent", "outputs")["change"] = {"type": "VcsRef"}
    child(nodes, "producer")["inputs"] = {
        "candidate": {
            "type": "VcsRef",
            "from": input_source,
            "output": "change",
            "path": "commit_sha",
        }
    }
    approval["inputs"] = {
        "gate": {"type": "CommandResult", "from": "producer", "output": "result"},
        "candidate": {
            "type": "VcsRef",
            "from": "agent",
            "output": "change",
            "optional": True,
        },
    }
    approval["depends_on"] = ["producer", "agent"]
    approval["trigger_rule"] = "none_failed_min_one_success"
    if parameter_input:
        child(nodes, "agent")["inputs"] = {
            "requested": {
                "type": "StructuredOutput",
                "from": "$params",
                "path": "enabled",
            }
        }
        child(approval, "inputs")["requested"] = {
            "type": "StructuredOutput",
            "from": "$params",
            "path": "enabled",
        }
    if separate_edges:
        child(nodes, "producer").pop("depends_on")
        child(approval, "inputs").pop("candidate")
        approval["depends_on"] = ["agent"]
    if disconnected:
        child(nodes, "producer").pop("depends_on")
        child(nodes, "producer").pop("inputs")
    if reverse:
        child(raw, "graph")["nodes"] = dict(reversed(tuple(nodes.items())))
    if rules is not None:
        for path, rule in rules.items():
            child(nodes, path)["trigger_rule"] = rule
    text, saved = projection_inputs(raw)
    projection = build_graph_cel_projection_paths(text, saved, limits=LIMITS.projection)
    manifest = build_declared_command_manifests(text, limits=LIMITS.resources.logical)
    references = build_graph_resource_references(
        text, scope, (), limits=LIMITS.resources
    )
    assert len(manifest.text) <= LIMITS.resources.source_bytes
    assert len(references.text) <= LIMITS.physical_input_bytes
    refs = ObjectReader(references.text).object()
    saved["cel_projection_paths"] = ObjectReader(projection.text).object()
    saved["command_resources"] = ObjectReader(manifest.text).object()
    prepared, pin = prepared_case(raw, {name: saved[name] for name in MEMBERS}, scope)
    async with database.transaction() as writer:
        await insert_case(Seeder(writer), scope, 1, prepared, refs)
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
    assert acquired is not None
    return check_detached_execution_contract(
        acquired, pin, scope, refs, (), limits=LIMITS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("separate_edges", [False, True])
async def test_readiness_prepares_data_and_control_edges_in_chain_order(
    database: Database, reverse: bool, separate_edges: bool
) -> None:
    ledger = ConnectionLedger(database)
    checked = await readiness_source(
        database, reverse=reverse, separate_edges=separate_edges
    )
    checkouts = ledger.checkouts

    prepared = prepare_readiness(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes
    )

    assert tuple(node.path for node in prepared.nodes) == (
        "agent",
        "producer",
        "approval",
    )
    assert tuple(node.kind for node in prepared.nodes) == (
        "agent",
        "command",
        "human_input",
    )
    assert tuple(node.predecessors for node in prepared.nodes) == (
        (),
        ("agent",),
        ("agent", "producer"),
    )
    assert tuple(node.trigger_rule for node in prepared.nodes) == (
        "all_success",
        "all_success",
        "none_failed_min_one_success",
    )
    assert prepared.matches(checked.scope, checked.pin)
    assert ledger.checkouts == checkouts
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_readiness_preserves_an_independent_input_inventory(
    database: Database,
) -> None:
    checked = await readiness_source(database)
    prepared = prepare_readiness(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes
    )
    command, approval = prepared.nodes[1:]

    declarations = command.declared_inputs()
    candidate = declarations["candidate"]
    assert isinstance(candidate, dict)
    candidate["path"] = PRIVATE
    declarations.clear()

    assert tuple(entry.name for entry in approval.inputs) == ("gate", "candidate")
    assert command.inputs[0].path == "commit_sha"
    assert command.inputs[0].producer == "agent"
    assert command.inputs[0].output == "change"
    assert command.inputs[0].type_name == "VcsRef"
    assert not command.inputs[0].optional
    assert approval.inputs[1].optional
    with pytest.raises(FrozenInstanceError):
        command.inputs[0].__setattr__("path", PRIVATE)


@pytest.mark.asyncio
async def test_readiness_keeps_the_original_namespace_graph_version_and_digest(
    database: Database,
) -> None:
    checked = await readiness_source(database)

    prepared = prepare_readiness(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes
    )

    for field in ("namespace_id", "graph_id", "version_id"):
        values: dict[str, str] = {field: "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
        assert not prepared.matches(replace(checked.scope, **values), checked.pin)
    assert not prepared.matches(
        checked.scope, replace(checked.pin, execution_contract_digest="0" * 64)
    )
    assert not prepared.matches(
        checked.scope, replace(checked.pin, graph_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")
    )
    with pytest.raises(FrozenInstanceError):
        prepared.__setattr__("nodes", ())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disconnected", "unsupported_source", "budget"])
async def test_readiness_refuses_unsupported_views_without_acquiring_metadata(
    database: Database, case: str
) -> None:
    ledger = ConnectionLedger(database)
    checked = await readiness_source(
        database,
        disconnected=case == "disconnected",
        input_source="$previous_attempt" if case == "unsupported_source" else "agent",
    )
    maximum = 1 if case == "budget" else LIMITS.resources.logical.ir_bytes
    checkouts = ledger.checkouts

    with pytest.raises(ReadinessRejected) as rejected:
        prepare_readiness(checked, max_ir_bytes=maximum)

    assert PRIVATE not in str(rejected.value)
    assert rejected.value.__context__ is None
    assert rejected.value.__cause__ is None
    assert ledger.checkouts == checkouts
    assert ledger.outstanding == 0
