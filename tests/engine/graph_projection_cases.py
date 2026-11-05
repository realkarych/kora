from typing import cast

from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_cel_facts import GraphCelReadLimits, stage_graph_cel_facts
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_parameters import build_graph_parameter_binding
from kora.engine.graph_projection_paths import GraphProjectionReadLimits
from kora.engine.graph_retry_policies import build_graph_retry_policies
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import CheckedCelSite
from kora.ir.graph_cel_inventory import checked_graph_sites
from kora.ir.json import JsonObject
from tests.engine.graph_inventory_cases import complete_inventory, inventory_arguments
from tests.engine.graph_output_cases import LIMIT, child, output_document
from tests.ir.trigger_expression_cases import sensor

CEL_LIMITS = GraphCelReadLimits(
    ir_bytes=LIMIT,
    output_context_bytes=LIMIT,
    parameter_context_bytes=LIMIT,
    comparison_bytes=LIMIT,
    declarations=100,
    triggers=10,
    typed_work=LIMIT,
    typed_bytes=LIMIT,
)
PROJECTION_LIMITS = GraphProjectionReadLimits(
    cel=CEL_LIMITS,
    retry_bytes=LIMIT,
    source_bytes=20 * LIMIT,
    section_bytes=LIMIT,
    work=LIMIT,
    scratch_bytes=10 * LIMIT,
)
OPTIONAL_INPUT = {
    "type": "StructuredOutput",
    "from": "producer",
    "output": "value",
    "optional": True,
}


def approval(predicate: str) -> JsonObject:
    node = child(output_document("human_input"), "graph", "nodes", "producer")
    node["inputs"] = {"data": dict(OPTIONAL_INPUT)}
    node["depends_on"] = ["producer"]
    child(node, "config")["approve_when"] = predicate
    return node


def projection_document() -> JsonObject:
    raw = output_document()
    graph = child(raw, "graph")
    nodes = child(graph, "nodes")
    graph["params"] = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "dormant": {"type": "boolean"},
        },
        "required": ["enabled", "dormant"],
    }
    nodes["gate"] = approval(
        "has(inputs.data) && inputs.data['\u00e9'] == true && "
        "params.enabled && response.decision == 'approve'"
    )
    nodes["presence"] = approval("has(inputs.data)")
    child(nodes, "gate")["retry"] = {
        "max_attempts": 1,
        "predicate": (
            "params.dormant && has(inputs.data) && inputs.data.private == true"
        ),
    }
    nodes["wait"] = child(output_document("wait_event"), "graph", "nodes", "producer")
    child(nodes, "wait", "config", "event")["match"] = "payload.ready == true"
    graph["triggers"] = [cast(JsonObject, sensor("observation.ready == true"))]
    nested = child(output_document(), "graph", "nodes")
    nested["gate"] = approval("has(inputs.data) && inputs.data.other == true")
    graph["subgraphs"] = {
        "unused": {
            "nodes": nested,
            "retry": {"max_attempts": 1, "predicate": "params.dormant"},
        },
    }
    child(raw, "runtime")["infrastructure_retry"] = {
        "max_attempts": 1,
        "predicate": "params.dormant",
    }
    return raw


def projection_sites(document: GraphDocument) -> tuple[CheckedCelSite, ...]:
    sites = checked_graph_sites(
        document,
        CEL_LIMITS.declarations,
        CEL_LIMITS.triggers,
        admission=CheckedInventoryAdmission(
            CEL_LIMITS.typed_work, CEL_LIMITS.typed_bytes
        ),
    )
    assert isinstance(sites, tuple), sites
    return sites


def projection_inputs(raw: JsonObject) -> tuple[str, JsonObject]:
    ir_text, types = inventory_arguments(raw)
    document = GraphDocument.model_validate(raw)
    facts = stage_graph_cel_facts(projection_sites(document), LIMIT)
    return ir_text, {
        "semantics": "kora.execution.v1",
        "type_contracts": types,
        "outputs": saved_array(complete_inventory((ir_text, types))),
        "parameters": ObjectReader(
            build_graph_parameter_binding(document, max_encoded_bytes=LIMIT)
        ).object(),
        "cel_expressions": ObjectReader(facts.text).object(),
        "retry_policies": ObjectReader(
            build_graph_retry_policies(ir_text, max_encoded_bytes=LIMIT)
        ).object(),
    }
