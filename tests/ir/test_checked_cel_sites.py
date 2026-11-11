import pytest

from kora.cel import PathSet
from kora.ir import export_ir
from kora.ir.approval_compilation import ApprovalPrerequisite, checked_approval_sites
from kora.ir.branch_expressions import checked_branch_sites
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import FieldPath
from kora.ir.json import JsonObject
from kora.ir.retry_expressions import checked_retry_sites
from kora.ir.trigger_expressions import (
    checked_sensor_trigger_sites,
    validate_sensor_trigger_expressions,
)
from kora.ir.wait_expressions import checked_wait_sites
from tests.ir.approval_default_cases import gate
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA, branch
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.retry_expression_cases import retrying
from tests.ir.trigger_expression_cases import sensor
from tests.ir.wait_expression_cases import wait

MARKER: str = "private-site-marker"
PRIVATE_SCHEMA: JsonObject = {"$ref": "https://example.invalid/private-schema"}
INPUTS: dict[str, object] = {
    "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
}
INTEGER_OK: JsonObject = {"properties": {"ok": {"type": "integer"}}, "required": ["ok"]}
RECURSIVE_CALL: dict[str, object] = node("subgraph", config={"body": "recursive"})


def approval_document() -> GraphDocument:
    root_schema: JsonObject = {
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    body_schema: JsonObject = {
        "properties": {"name": {"type": "integer"}},
        "required": ["name"],
    }
    condition = (
        'response.decision == "approve" && '
        f'inputs.data.name.matches("^{MARKER}$") && params.name.matches("^a$")'
    )
    return document_for(
        contracts=BUILTINS,
        params=root_schema,
        nodes={
            "producer": producer(schema=root_schema),
            "default": gate(),
            "gate": gate(predicate=condition, inputs=INPUTS),
            "call": RECURSIVE_CALL,
        },
        bodies={
            "unused": {
                "nodes": {
                    "producer": producer(schema=body_schema),
                    "gate": gate(predicate="inputs.data.name == 1", inputs=INPUTS),
                }
            },
            "recursive": {
                "nodes": {"gate": gate(predicate="true"), "again": RECURSIVE_CALL}
            },
        },
    )


def unused_failures(*failures: dict[str, object]) -> GraphDocument:
    return document_for(
        nodes={"first": gate(predicate="true")},
        bodies={
            "unused": {
                "nodes": {
                    f"failure_{index}": failure
                    for index, failure in enumerate(failures)
                }
            }
        },
    )


def approval_path(*owner: str) -> FieldPath:
    return (*owner, "config", "approve_when")


def test_approval_sites_anchor_generated_defaults_and_owning_bodies() -> None:
    document = approval_document()
    original = export_ir(document)

    sites = checked_approval_sites(document, max_declarations=15)
    over_capacity = checked_approval_sites(document, max_declarations=14)

    assert isinstance(sites, tuple)
    assert [site.path for site in sites] == [
        approval_path("graph", "nodes", "default"),
        approval_path("graph", "nodes", "gate"),
        approval_path("graph", "subgraphs", "unused", "nodes", "gate"),
        approval_path("graph", "subgraphs", "recursive", "nodes", "gate"),
    ]
    assert [site.authored for site in sites] == [False, True, True, True]
    assert sites[0].checked.rendering == '(response.decision == "approve")'
    assert sites[1].checked.projections == {
        "response": PathSet((".decision",)),
        "inputs": PathSet((".data.name",)),
        "params": PathSet((".name",)),
    }
    assert MARKER not in repr(sites)
    assert over_capacity is None
    assert export_ir(document) == original


@pytest.mark.parametrize(
    ("failures", "prerequisite"),
    [
        (
            (gate(predicate="1"), gate(PRIVATE_SCHEMA)),
            ApprovalPrerequisite.SCHEMA_UNRESOLVED,
        ),
        (
            (
                gate(PRIVATE_SCHEMA),
                gate(predicate="inputs.missing", inputs=unavailable_inputs()),
            ),
            ApprovalPrerequisite.CONTEXT_UNRESOLVED,
        ),
        (
            (
                gate(predicate="inputs.missing", inputs=unavailable_inputs()),
                gate(predicate="true &&"),
            ),
            ApprovalPrerequisite.SYNTAX_UNRESOLVED,
        ),
    ],
    ids=["schema-over-typed", "context-over-schema", "syntax-over-context"],
)
def test_approval_sites_report_the_highest_priority_prerequisite_of_the_pass(
    failures: tuple[dict[str, object], ...], prerequisite: ApprovalPrerequisite
) -> None:
    document = unused_failures(*failures)

    result = checked_approval_sites(document, max_declarations=5)

    assert result is prerequisite


def test_approval_sites_return_every_static_error_without_a_partial_inventory() -> None:
    document = unused_failures(gate(predicate="1"), gate({}))

    result = checked_approval_sites(document, max_declarations=5)

    assert isinstance(result, DiagnosticCollection)
    assert result.errors_omitted == 0
    assert [(error.code, error.path) for error in result.errors] == [
        ("IR011", approval_path("graph", "subgraphs", "unused", "nodes", name))
        for name in ("failure_0", "failure_1")
    ]


def test_branch_sites_include_dormant_reasons_and_every_stored_body() -> None:
    document = document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "branch": node(
                "branch",
                inputs=INPUTS,
                config={
                    "cases": [
                        {"when": "inputs.data.ok && params.ok", "to": ["target"]},
                        {"when": "false", "to": ["target"], "reason": f'"{MARKER}"'},
                    ],
                    "else_reason": '"fallback"',
                },
            ),
            "target": node(depends_on=["branch"]),
            "call": RECURSIVE_CALL,
        },
        bodies={
            "unused": {
                "nodes": {
                    "producer": producer(schema=INTEGER_OK),
                    "branch": branch("inputs.data.ok == 1", '"reason"', inputs=INPUTS),
                    "target": node(depends_on=["branch"]),
                }
            },
            "recursive": {"nodes": {"branch": branch(), "again": RECURSIVE_CALL}},
        },
    )
    original = export_ir(document)

    sites = checked_branch_sites(document, max_declarations=20)
    over_capacity = checked_branch_sites(document, max_declarations=19)

    assert isinstance(sites, tuple)
    assert [site.path for site in sites] == [
        ("graph", "nodes", "branch", "config", "cases", 0, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "when"),
        ("graph", "nodes", "branch", "config", "cases", 1, "reason"),
        ("graph", "nodes", "branch", "config", "else_reason"),
        (
            "graph",
            "subgraphs",
            "unused",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "when",
        ),
        (
            "graph",
            "subgraphs",
            "unused",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "reason",
        ),
        (
            "graph",
            "subgraphs",
            "recursive",
            "nodes",
            "branch",
            "config",
            "cases",
            0,
            "when",
        ),
    ]
    assert sites[2].checked.rendering == f'"{MARKER}"'
    assert MARKER not in repr(sites)
    assert over_capacity is None
    assert export_ir(document) == original


def test_wait_sites_check_event_and_poll_fields_and_keep_omitted_matches_absent() -> (
    None
):
    document = document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "event": wait(
                "inputs.data.ok && params.ok && payload.ready", inputs=INPUTS
            ),
            "poll": wait("observation.ready", poll=True),
            "omitted": wait(None, inputs=unavailable_inputs()),
            "call": RECURSIVE_CALL,
        },
        bodies={
            "unused": {
                "nodes": {
                    "producer": producer(schema=INTEGER_OK),
                    "event": wait(
                        "inputs.data.ok == 1 && payload.ready", inputs=INPUTS
                    ),
                    "poll": wait(f'observation.name == "{MARKER}"', poll=True),
                }
            },
            "recursive": {"nodes": {"wait": wait(), "again": RECURSIVE_CALL}},
        },
    )
    original = export_ir(document)

    sites = checked_wait_sites(document, max_declarations=17)
    over_capacity = checked_wait_sites(document, max_declarations=16)

    assert isinstance(sites, tuple)
    assert [site.path for site in sites] == [
        ("graph", "nodes", "event", "config", "event", "match"),
        ("graph", "nodes", "poll", "config", "poll", "until"),
        ("graph", "subgraphs", "unused", "nodes", "event", "config", "event", "match"),
        ("graph", "subgraphs", "unused", "nodes", "poll", "config", "poll", "until"),
        (
            "graph",
            "subgraphs",
            "recursive",
            "nodes",
            "wait",
            "config",
            "event",
            "match",
        ),
    ]
    assert MARKER not in repr(sites)
    assert over_capacity is None
    assert export_ir(document) == original


def test_retry_sites_check_node_and_body_predicates_and_keep_omissions_absent() -> None:
    document = document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "check": retrying(
                "has(outputs.value) && outputs.value.ok && inputs.data.ok && params.ok",
                producer(schema=SCHEMA),
                inputs=INPUTS,
            ),
            "omitted": retrying(None),
            "call": RECURSIVE_CALL,
        },
        bodies={
            "unused": {
                "retry": {"max_attempts": 1, "predicate": "params.ok"},
                "nodes": {
                    "check": retrying(
                        "has(outputs.value) && outputs.value.ok == 1",
                        producer(schema=INTEGER_OK),
                    ),
                    "timer": node(),
                },
            },
            "recursive": {
                "retry": {"max_attempts": 1, "predicate": f'"{MARKER}" != ""'},
                "nodes": {"check": retrying("true"), "again": RECURSIVE_CALL},
            },
        },
    )
    original = export_ir(document)

    sites = checked_retry_sites(document, max_declarations=15)
    over_capacity = checked_retry_sites(document, max_declarations=14)

    assert isinstance(sites, tuple)
    assert [site.path for site in sites] == [
        ("graph", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "retry", "predicate"),
        ("graph", "subgraphs", "unused", "nodes", "check", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "retry", "predicate"),
        ("graph", "subgraphs", "recursive", "nodes", "check", "retry", "predicate"),
    ]
    assert MARKER not in repr(sites)
    assert over_capacity is None
    assert export_ir(document) == original


def test_sensor_trigger_sites_admit_every_trigger_before_checking() -> None:
    document = document_for(
        triggers=[
            {"kind": "manual"},
            sensor("observation.ready"),
            sensor("false"),
            sensor(f'observation.name == "{MARKER}"'),
        ]
    )
    malformed = document_for(triggers=[{"kind": "manual"}, sensor("true &&")])
    typed = document_for(triggers=[sensor("observation.ready"), sensor("1")])

    sites = checked_sensor_trigger_sites(document, max_triggers=4)
    over_capacity = checked_sensor_trigger_sites(document, max_triggers=3)
    malformed_over_capacity = checked_sensor_trigger_sites(malformed, max_triggers=1)
    unresolved = checked_sensor_trigger_sites(malformed, max_triggers=2)
    refused = checked_sensor_trigger_sites(typed, max_triggers=2)

    assert isinstance(sites, tuple)
    assert [site.path for site in sites] == [
        ("graph", "triggers", index, "poll", "until") for index in (1, 2, 3)
    ]
    assert MARKER not in repr(sites)
    assert over_capacity is malformed_over_capacity is None
    assert unresolved == validate_sensor_trigger_expressions(malformed)
    assert refused == validate_sensor_trigger_expressions(typed)
