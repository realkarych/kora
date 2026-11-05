import gc
import json
from weakref import ref

import pytest

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.cost import Variable
from kora.cel.tree import Node
from kora.cel.types import Record
from kora.ir import export_ir, validate_expression_syntax, validate_retry_expressions
from kora.ir.approval_inputs import DirectInputContexts
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
from kora.ir.retry_expressions import RetryExpressionChecks, checked_retry_sites
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.branch_expression_cases import SCHEMA
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.retry_expression_cases import retry_document, retrying
from tests.ir.trigger_expression_cases import expensive


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("has(outputs.value) && outputs.value.ok && inputs.data.ok && params.ok", True),
        ("has(outputs.result) && outputs.result.exit_code != 0", True),
        ("outputs.result.exit_code != 0", False),
        ("outputs.value.ok", False),
        ("true", True),
        ("false", True),
        ("1", False),
        ('"literal"', False),
        ("null", False),
        ("response.ok", False),
        ("payload.ok", False),
        ("observation.ok", False),
        ("inputs.data.missing", False),
        ('params.mode == "missing"', False),
        ("outputs == null", False),
        ("inputs == null", False),
        ("[1].exists(outputs, true)", False),
        ("[1].exists(inputs, true)", False),
        ("[1].exists(params, true)", False),
    ],
)
def test_retry_fields_use_optional_outputs_and_declared_contexts(
    nested: bool,
    text: str,
    valid: bool,
) -> None:
    document = retry_document(text, nested=nested)
    original = export_ir(document)

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert result.retry_expressions_valid is valid
    assert result.expressions_seen == result.expressions_checked == 1
    assert result.policies_checked == 1
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)
    assert bool(result.diagnostics.errors) is not valid
    assert all(
        error.code == "IR011"
        and error.path == (*prefix, "nodes", "check", "retry", "predicate")
        and error.location is not None
        and (error.location.file, error.location.line) == ("retry.py", 7)
        and error.node == "check"
        for error in result.diagnostics.errors
    )
    assert export_ir(document) == original


@pytest.mark.parametrize(
    ("type_name", "output", "field"),
    [
        ("CommandResult", "result", "exit_code == 0"),
        ("VcsRef", "value", "commit == 'abc'"),
        ("AgentSession", "session", "turns > 0"),
        ("StructuredOutput", "value", "anything"),
    ],
)
def test_retry_outputs_use_real_supported_native_views(
    type_name: str,
    output: str,
    field: str,
) -> None:
    text = f"has(outputs.{output}) && outputs.{output}.{field}"
    document = document_for(
        contracts=BUILTINS, nodes={"check": retrying(text, producer(type_name))}
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_valid
    assert result.expressions_checked == 1


@pytest.mark.parametrize("kind", ["human_input", "wait_event"])
def test_retry_scheduler_outputs_keep_their_real_inline_schema(kind: str) -> None:
    output = "response" if kind == "human_input" else "event"
    source = node(
        kind, outputs={output: {"type": "StructuredOutput", "schema": SCHEMA}}
    )
    if kind == "human_input":
        source["config"] = {
            "channel": "cli",
            "form": SCHEMA,
            "timeout": "1h",
            "on_expiry": "fail",
        }
    text = f"has(outputs.{output}) && outputs.{output}.ok"
    document = document_for(contracts=BUILTINS, nodes={"check": retrying(text, source)})

    result = validate_retry_expressions(document)

    assert result.retry_expressions_valid


@pytest.mark.parametrize(
    "field", ["transcript", "metadata", "denials", "price_revision_id"]
)
def test_retry_agent_session_outputs_keep_published_exclusions(field: str) -> None:
    text = f"has(outputs.session) && has(outputs.session.{field})"
    document = document_for(
        contracts=BUILTINS, nodes={"check": retrying(text, producer("AgentSession"))}
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert not result.retry_expressions_valid
    assert result.diagnostics.errors[0].code == "IR011"


@pytest.mark.parametrize("kind", ["timer", "branch", "subgraph", "fan_out"])
def test_retry_other_node_kinds_use_real_input_and_parameter_contexts(
    kind: str,
) -> None:
    document = document_for(
        contracts=BUILTINS,
        params=SCHEMA,
        nodes={
            "producer": producer(schema=SCHEMA),
            "check": retrying(
                "inputs.data.ok && params.ok",
                node(kind),
                inputs={
                    "data": {
                        "type": "StructuredOutput",
                        "from": "producer",
                        "output": "value",
                    }
                },
            ),
        },
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_valid


@pytest.mark.parametrize("kind", ["timer", "branch"])
def test_retry_outputless_nodes_have_a_closed_empty_output_record(kind: str) -> None:
    document = document_for(
        nodes={"check": retrying("has(outputs.missing)", node(kind))}
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert not result.retry_expressions_valid
    assert result.context_unresolved == 0
    assert result.diagnostics.errors[0].code == "IR011"


@pytest.mark.parametrize("schema", [None, {}])
def test_retry_parameter_omission_remains_distinct(schema: JsonObject | None) -> None:
    document = document_for(params=schema, nodes={"check": retrying("params.anything")})

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert result.retry_expressions_valid is (schema == {})


def test_retry_omission_and_dormant_predicates_preserve_authored_data() -> None:
    document = document_for(
        nodes={"omitted": retrying(None), "disabled": retrying("1")}
    )
    original = export_ir(document)

    result = validate_retry_expressions(document)

    assert result.policies_checked == 2
    assert result.expressions_seen == result.expressions_checked == 1
    assert result.retry_expressions_complete
    assert not result.retry_expressions_valid
    assert result.diagnostics.errors[0].path == (
        "graph",
        "nodes",
        "disabled",
        "retry",
        "predicate",
    )
    assert export_ir(document) == original


@pytest.mark.parametrize(
    ("text", "valid", "unresolved"),
    [
        ("params.ok", True, 0),
        ("false", True, 0),
        ("1", False, 0),
        ("has(outputs.value)", False, 1),
        ("inputs.data.ok", False, 1),
    ],
)
def test_retry_body_predicates_keep_supported_and_unavailable_contexts_explicit(
    text: str, valid: bool, unresolved: int
) -> None:
    document = document_for(
        params=SCHEMA,
        bodies={
            "unused": {
                "nodes": {"timer": node()},
                "retry": {"max_attempts": 1, "predicate": text},
                "source": {"file": "body.py", "line": 9},
            }
        },
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_valid is valid
    assert result.context_unresolved == unresolved
    assert result.retry_expressions_complete is (unresolved == 0)
    assert result.expressions_seen == 1
    assert all(
        error.path == ("graph", "subgraphs", "unused", "retry", "predicate")
        and error.node is None
        and error.location is not None
        and error.location.file == "body.py"
        for error in result.diagnostics.errors
    )


def test_retry_preserves_incomplete_prerequisites_and_independent_errors() -> None:
    document = document_for(
        params={"$ref": "https://example.invalid/private-schema"},
        nodes={
            "literal": retrying("true", inputs=unavailable_inputs()),
            "schema": retrying("params.ok"),
            "context": retrying("inputs.unknown", inputs=unavailable_inputs()),
            "syntax": retrying("true &&"),
            "invalid": retrying("unknown"),
            "output": retrying("has(outputs.value)", producer()),
        },
    )

    result = validate_retry_expressions(document)
    syntax = validate_expression_syntax(document)

    assert not result.retry_expressions_complete
    assert not result.retry_expressions_valid
    assert result.expressions_seen == 6
    assert result.expressions_checked == 2
    assert result.syntax_unresolved == result.schema_unresolved == 1
    assert result.context_unresolved == 2
    assert len(result.diagnostics.errors) == len(syntax.diagnostics.errors) == 1


@pytest.mark.parametrize("kind", ["subgraph", "fan_out", "timer", "branch"])
def test_retry_unsupported_output_declarations_remain_incomplete(kind: str) -> None:
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "check": retrying(
                "has(outputs.value)",
                node(kind, outputs={"value": {"type": "StructuredOutput"}}),
            )
        },
    )

    result = validate_retry_expressions(document)

    assert not result.retry_expressions_complete
    assert result.context_unresolved == 1
    assert result.expressions_checked == 0
    assert result.diagnostics.valid


def test_retry_preserves_codepoint_offsets_cost_and_bounded_diagnostics() -> None:
    text = '"😀" == "😀" && missing'
    document = document_for(
        params={},
        nodes={
            "offset": retrying(text),
            "cost": retrying(expensive().replace("observation", "params")),
        },
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert {error.code for error in result.diagnostics.errors} == {"IR011", "IR027"}
    assert result.diagnostics.errors[1].offset == text.index("missing")


def test_retry_diagnostics_are_shared_bounded_and_content_free() -> None:
    marker = "private_retry_expression_content"
    document = document_for(nodes={f"check{i}": retrying(marker) for i in range(1002)})

    result = validate_retry_expressions(document)
    encoded = json.dumps(
        [error.model_dump(mode="json") for error in result.diagnostics.errors]
    )

    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2
    assert not result.retry_expressions_complete
    assert not result.retry_expressions_valid
    assert marker not in encoded


def test_retry_context_reuses_real_output_mapping_and_nested_bounds() -> None:
    one = document_for(
        contracts=BUILTINS, nodes={"check": retrying("has(outputs.result)")}
    )
    many = document_for(
        contracts=BUILTINS,
        nodes={f"check{i}": retrying("has(outputs.result)") for i in range(100)},
    )
    bounded: JsonObject = {
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 3},
            }
        },
        "required": ["items"],
    }
    bounded_document = document_for(
        contracts=BUILTINS,
        nodes={"check": retrying("has(outputs.value)", producer(schema=bounded))},
    )
    checks = RetryExpressionChecks(bounded_document)

    single = validate_retry_expressions(one)
    repeated = validate_retry_expressions(many)
    output = checks.inputs.output_context(bounded_document.graph.nodes["check"])

    assert single.retry_expressions_valid
    assert repeated.retry_expressions_valid
    assert single.schema_steps == repeated.schema_steps
    assert repeated.checker_steps == single.checker_steps * 100
    assert isinstance(output, Variable)
    assert isinstance(output.type, Record)
    assert not any(field.required for field in output.type.fields.values())
    value_bound = output.bound.fields["value"]
    items = value_bound.fields["items"]
    assert items.items == 2
    assert items.element is not None
    assert items.element.string_bytes == 12


@pytest.mark.parametrize("source", ["has(outputs.result)", "unknown", "true &&"])
def test_retry_result_releases_context_document_and_checked_state(source: str) -> None:
    document = retry_document(source)
    document_ref = ref(document)
    owned = (
        InputParameterContexts
        | DirectInputContexts
        | SchemaTypes
        | CheckedExpression
        | Environment
        | Node
    )
    gc.collect()
    baseline = sum(isinstance(value, owned) for value in gc.get_objects())

    result = validate_retry_expressions(document)
    del document
    gc.collect()
    remaining = sum(isinstance(value, owned) for value in gc.get_objects())

    assert document_ref() is None
    assert remaining == baseline
    assert result.expressions_seen == 1


def test_retry_scoped_pass_does_not_select_infrastructure_environment() -> None:
    document = document_for()
    raw = export_ir(document)
    raw["runtime"] = {
        "runtime": "local",
        "vcs": "git",
        "infrastructure_retry": {"max_attempts": 1, "predicate": "unknown"},
    }
    ordinary = type(document).model_validate(raw)

    result = validate_retry_expressions(ordinary)
    sites = checked_retry_sites(ordinary, max_declarations=2)

    assert result.retry_expressions_valid
    assert result.expressions_seen == 0
    assert sites == ()


@pytest.mark.parametrize(
    ("text", "valid"),
    [("inputs.data.ok", False), ("has(inputs.data) && inputs.data.ok", True)],
)
def test_retry_preserves_optional_input_guard_rules(text: str, valid: bool) -> None:
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "producer": producer(schema=SCHEMA),
            "check": retrying(
                text,
                inputs={
                    "data": {
                        "type": "StructuredOutput",
                        "from": "producer",
                        "output": "value",
                        "optional": True,
                    }
                },
            ),
        },
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_complete
    assert result.retry_expressions_valid is valid


@pytest.mark.parametrize("collected", [False, True])
def test_retry_output_source_refusal_precedes_mapping(collected: bool) -> None:
    declaration = (
        {"type": "StructuredOutput", "collect": "items"}
        if collected
        else {"type": "example/Missing"}
    )
    source = producer()
    source["outputs"] = {"result": {"type": "CommandResult"}, "value": declaration}
    document = document_for(
        contracts=BUILTINS, nodes={"check": retrying("has(outputs.result)", source)}
    )

    result = validate_retry_expressions(document)

    assert result.context_unresolved == 1
    assert not result.retry_expressions_complete
    assert result.schema_steps == 0
    assert result.expressions_checked == 0


def test_retry_output_schema_mapping_failure_stays_incomplete() -> None:
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "check": retrying(
                "has(outputs.value)",
                producer(schema={"$ref": "https://example.invalid/private-schema"}),
            )
        },
    )

    result = validate_retry_expressions(document)

    assert result.schema_unresolved == 1
    assert not result.retry_expressions_complete
    assert result.diagnostics.valid


def test_retry_output_mapping_preserves_original_reference_root() -> None:
    schema: JsonObject = {
        "$defs": {"ready": {"type": "boolean"}},
        "properties": {"ok": {"$ref": "#/$defs/ready"}},
        "required": ["ok"],
    }
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "check": retrying(
                "has(outputs.value) && outputs.value.ok", producer(schema=schema)
            )
        },
    )

    result = validate_retry_expressions(document)

    assert result.retry_expressions_valid
