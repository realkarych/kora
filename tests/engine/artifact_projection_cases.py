from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

from kora.conformance.capture import Capture, captured
from kora.db.graph_value_json import ObjectReader
from kora.engine.artifact_projection import (
    PreparedArtifactProjection,
    ProjectionReadMetadata,
)
from kora.engine.artifact_projection_codec import REPRESENTATION, ProjectionLimits
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import build_graph_cel_projection_paths
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    build_graph_resource_references,
)
from kora.engine.locks import COMPONENT
from kora.ir.json import JsonObject, JsonValue
from kora.observability import Level, LoggingSettings
from tests.engine.graph_output_cases import child, output_document
from tests.engine.graph_projection_cases import approval, projection_inputs
from tests.engine.graph_schema_cases import declaration
from tests.engine.test_graph_execution_contract import LIMITS as GRAPH_LIMITS
from tests.engine.test_graph_execution_contract import prepared_case, transport
from tests.engine.test_graph_resource_references import SCOPE, declared
from tests.support.observability import FakeClock

PATH = ("graph", "nodes", "producer", "outputs", "value")
LIMITS = ProjectionLimits(1_000_000, 500_000_000, 100_000_000, 1_000_000_000_000_000)


@contextmanager
def projection_capture() -> Generator[Capture]:
    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "projection-tests",
        FakeClock().now,
    ) as capture:
        yield capture


def projection_graph(
    schema: JsonObject | None = None,
    predicate: str = "has(inputs.data) && inputs.data == inputs.data",
    *,
    second: bool = False,
    named: bool = False,
    scope: GraphResourceScope = SCOPE,
) -> CheckedExecutionContract:
    raw = output_document()
    if schema is not None:
        child(raw, "graph", "nodes", "producer", "outputs", "value")["schema"] = schema
    nodes = child(raw, "graph", "nodes")
    nodes["gate"] = approval(predicate)
    if named:
        child(raw, "type_contracts")["example/Value"] = declaration(
            {} if schema is None else schema
        )
        child(nodes, "producer", "outputs", "value")["type"] = "example/Value"
        child(nodes, "gate", "inputs", "data")["type"] = "example/Value"
    if second:
        nodes["another"] = child(output_document(), "graph", "nodes", "producer")
        if schema is not None:
            child(nodes, "another", "outputs", "value")["schema"] = schema
        other_gate = approval(predicate)
        child(other_gate, "inputs", "data")["from"] = "another"
        other_gate["depends_on"] = ["another"]
        nodes["other_gate"] = other_gate
    ir, staged = projection_inputs(raw)
    paths = build_graph_cel_projection_paths(ir, staged, limits=GRAPH_LIMITS.projection)
    resources = build_graph_resource_references(
        ir, scope, (), limits=GRAPH_LIMITS.resources
    )
    contract: JsonObject = {
        name: staged[name]
        for name in (
            "semantics",
            "type_contracts",
            "parameters",
            "outputs",
            "retry_policies",
            "cel_expressions",
        )
    }
    contract["cel_projection_paths"] = ObjectReader(paths.text).object()
    contract["command_resources"] = declared(ir)
    prepared, pin = prepared_case(raw, contract, scope)
    return check_detached_execution_contract(
        transport(prepared, scope),
        pin,
        scope,
        ObjectReader(resources.text).object(),
        (),
        limits=GRAPH_LIMITS,
    )


def metadata(value: PreparedArtifactProjection) -> ProjectionReadMetadata:
    return ProjectionReadMetadata(
        value.binding,
        value.identity,
        value.content_digest,
        len(value.encoded()),
        REPRESENTATION,
        True,
    )


def native_values(value: object) -> tuple[JsonValue, JsonValue]:
    assert isinstance(value, dict)
    return cast(JsonValue, value["original"]), cast(JsonValue, value["converted"])
