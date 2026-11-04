import pytest

from kora.ir import export_ir, validate_approval_response_predicates
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import gate, saved_form
from tests.ir.approval_input_cases import BUILTINS, direct_document, producer
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.output_schema_cases import human


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    ("type_name", "predicate", "valid"),
    [
        ("VcsRef", "inputs.data.commit == 'abc'", True),
        ("VcsRef", "inputs.data.commit == 1", False),
        ("CommandResult", "inputs.data.exit_code == 0", True),
        ("CommandResult", "inputs.data.exit_code == 0.0", False),
        ("AgentSession", "inputs.data.turns > 0", True),
        ("AgentSession", "inputs.data.turns == 'one'", False),
        ("StructuredOutput", "inputs.data.anything", True),
        ("StructuredOutput", "inputs.data == null", True),
        (
            "example/Custom",
            "inputs.data.ok && params.ok && response.decision == 'approve'",
            True,
        ),
        ("example/Custom", "inputs.data.ok == 'true'", False),
    ],
)
def test_approval_direct_inputs_use_producer_views(
    type_name: str, predicate: str, valid: bool, nested: bool
) -> None:
    contracts = {
        **BUILTINS,
        "example/Custom": saved_form(
            {"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        ),
    }
    document = direct_document(
        predicate, producer(type_name), type_name, nested=nested, contracts=contracts
    )
    before = export_ir(document)

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is valid
    assert result.predicates_checked == 1
    assert len(result.diagnostics.errors) == int(not valid)
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "kind",
    [
        "agent",
        "agent_config",
        "agent_declaration",
        "command",
        "human_input",
        "wait_event",
    ],
)
@pytest.mark.parametrize("valid", [False, True])
def test_approval_direct_inputs_use_role_owned_structured_schema(
    kind: str, valid: bool
) -> None:
    schema: JsonObject = {
        "properties": {"choice": {"enum": ["yes"]}},
        "required": ["choice"],
    }
    source = producer(schema=schema)
    output = "value"
    if kind.startswith("agent"):
        config: dict[str, object] = {
            "harness": "claude",
            "prompt": "",
            "workspace": {"repo": ""},
        }
        if kind != "agent_declaration":
            config["output_schema"] = schema
        value: dict[str, object] = {"type": "StructuredOutput"}
        if kind != "agent_config":
            value["schema"] = schema
        source = node(
            "agent",
            config=config,
            outputs={
                "session": {"type": "AgentSession"},
                "value": value,
            },
        )
    elif kind == "human_input":
        source = human(schema, schema)
        output = "response"
    elif kind == "wait_event":
        source = node(
            "wait_event",
            outputs={"event": {"type": "StructuredOutput", "schema": schema}},
        )
        output = "event"
    predicate = "inputs.data.choice == '" + ("yes" if valid else "no") + "'"
    document = direct_document(predicate, source, output=output)

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is valid
    assert result.predicates_checked == 1
    assert len(result.diagnostics.errors) == int(not valid)


@pytest.mark.parametrize("local", [False, True])
def test_approval_direct_inputs_are_node_and_body_scoped(local: bool) -> None:
    boolean: JsonObject = {
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    integer: JsonObject = {
        "properties": {"ok": {"type": "integer"}},
        "required": ["ok"],
    }
    binding = {
        "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
    }
    root = {
        "producer": producer(schema=boolean),
        "gate": gate(None, "inputs.data.ok", inputs=binding),
    }
    body = {"producer": producer(schema=integer)} if local else {}
    body["gate"] = gate(None, "inputs.data.ok == 1", inputs=binding)
    document = document_for(
        contracts=BUILTINS, nodes=root, bodies={"unused": {"nodes": body}}
    )

    result = validate_approval_response_predicates(document)

    assert result.diagnostics.valid
    assert result.predicates_seen == 2
    assert result.predicates_checked == 1 + int(local)
    assert result.context_unresolved == int(not local)
    assert result.approval_predicates_complete is local


@pytest.mark.parametrize(
    ("predicate", "optional", "valid"),
    [
        ("inputs.data.exit_code == 0", False, True),
        ("inputs.data.exit_code == 0", True, False),
        ("has(inputs.data) && inputs.data.exit_code == 0", True, True),
        ("inputs == {}", False, False),
        ("inputs['data'].exit_code == 0", False, False),
        ("[true].all(inputs, inputs)", False, False),
        ("inputs.unknown == 0", False, False),
        ("false && inputs.unknown", False, False),
    ],
)
def test_approval_direct_inputs_preserve_optional_and_root_rules(
    predicate: str, optional: bool, valid: bool
) -> None:
    document = direct_document(
        predicate, producer("CommandResult"), "CommandResult", optional=optional
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is valid
    assert result.predicates_checked == 1


@pytest.mark.parametrize(
    "predicate",
    [
        "inputs.missing",
        "inputs == {}",
        "has(inputs.missing)",
        "[true].all(inputs, inputs)",
    ],
)
def test_approval_empty_inputs_are_closed(predicate: str) -> None:
    document = document_for(nodes={"gate": gate(None, predicate)})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.context_unresolved == 0
    assert result.diagnostics.errors[0].code == "IR011"


@pytest.mark.parametrize(
    "case",
    [
        "missing_source",
        "missing_output",
        "path",
        "empty_path",
        "params",
        "inputs",
        "previous",
        "list",
        "wrapper",
        "collect",
        "mismatch",
        "unknown",
        "revision",
        "inherited",
        "custom_inline",
        "vcs_inline",
        "command_inline",
        "session_inline",
        "agent_conflict",
        "human_conflict",
        "agent_roles",
        "session_wrong_role",
        "vcs_wrong_role",
        "custom_wrong_role",
        "file_missing",
        "file_multiple",
    ],
)
def test_approval_direct_inputs_keep_unavailable_prerequisites_incomplete(
    case: str,
) -> None:
    binding: dict[str, object] = {
        "type": "StructuredOutput",
        "from": "producer",
        "output": "value",
    }
    source = producer()
    contracts = dict(BUILTINS)
    if case == "missing_source":
        binding["from"] = "missing"
    elif case == "missing_output":
        binding["output"] = "missing"
    elif case in {"path", "empty_path"}:
        binding["path"] = "ok" if case == "path" else ""
    elif case in {"params", "inputs", "previous"}:
        binding["from"] = {
            "params": "$params",
            "inputs": "$inputs",
            "previous": "$previous_attempt",
        }[case]
    elif case == "list":
        binding["type"] = "List[StructuredOutput]"
        source = producer("List[StructuredOutput]")
    elif case == "wrapper":
        source = node(
            "subgraph",
            config={"body": "missing"},
            outputs={"value": {"type": "StructuredOutput"}},
        )
    elif case == "collect":
        source = node(
            "command",
            config={
                "cmd": ["true"],
                "workspace": {"repo": ""},
                "output_file": "value.json",
            },
            outputs={
                "result": {"type": "CommandResult"},
                "value": {"type": "StructuredOutput", "collect": "value"},
            },
        )
    elif case == "mismatch":
        binding["type"] = "VcsRef"
    elif case == "unknown":
        contracts.pop("StructuredOutput")
    elif case == "revision":
        contracts["StructuredOutput"] = {"builtin_revision": "unavailable"}
    elif case in {"inherited", "custom_inline"}:
        binding["type"] = "example/Custom"
        contracts["example/Custom"] = {
            **saved_form({}),
            **({"parent": "StructuredOutput"} if case == "inherited" else {}),
        }
        source = producer("example/Custom", {} if case == "custom_inline" else None)
    elif case in {"vcs_inline", "command_inline", "session_inline"}:
        type_name = {
            "vcs_inline": "VcsRef",
            "command_inline": "CommandResult",
            "session_inline": "AgentSession",
        }[case]
        binding["type"] = type_name
        source = producer(type_name, {})
        if type_name == "CommandResult":
            binding["output"] = "result"
        if type_name == "AgentSession":
            binding["output"] = "session"
    elif case in {
        "session_wrong_role",
        "vcs_wrong_role",
        "file_missing",
        "file_multiple",
    }:
        type_name = {
            "session_wrong_role": "AgentSession",
            "vcs_wrong_role": "VcsRef",
        }.get(case, "StructuredOutput")
        binding["type"] = type_name
        command_config: dict[str, object] = {"cmd": ["true"], "workspace": {"repo": ""}}
        if case != "file_missing":
            command_config["output_file"] = "value.json"
        command_outputs: dict[str, object] = {
            "result": {"type": "CommandResult"},
            "value": {"type": type_name},
        }
        if case == "file_multiple":
            command_outputs["second"] = {"type": "StructuredOutput"}
        source = node("command", config=command_config, outputs=command_outputs)
    elif case == "custom_wrong_role":
        binding["type"] = "example/Custom"
        contracts["example/Custom"] = saved_form({"type": "object"})
        source = node(
            "agent",
            outputs={
                "session": {"type": "AgentSession"},
                "value": {"type": "example/Custom"},
            },
        )
    elif case == "human_conflict":
        source = human({"type": "boolean"}, {"type": "integer"})
        binding["output"] = "response"
    else:
        config: dict[str, object] = {
            "harness": "claude",
            "prompt": "",
            "workspace": {"repo": ""},
        }
        outputs: dict[str, object] = {
            "session": {"type": "AgentSession"},
            "value": {"type": "StructuredOutput", "schema": {"type": "integer"}},
        }
        if case == "agent_conflict":
            config["output_schema"] = {"type": "boolean"}
        if case == "agent_roles":
            config["output_schema"] = {"type": "integer"}
            outputs["second"] = {"type": "StructuredOutput"}
        source = node("agent", config=config, outputs=outputs)
    document = document_for(
        contracts=contracts,
        nodes={
            "producer": source,
            "gate": gate(None, "inputs.data.ok && params.ok", inputs={"data": binding}),
            "independent": gate(None, "true"),
        },
    )

    result = validate_approval_response_predicates(document)

    assert not result.approval_predicates_complete
    assert result.context_unresolved == 1
    assert result.schema_unresolved == 0
    assert result.predicates_seen == 2
    assert result.predicates_checked == 1
    assert result.diagnostics.valid


@pytest.mark.parametrize(
    "predicate",
    [
        "inputs.data.denials",
        "inputs.data.transcript",
        "has(inputs.data.transcript)",
        "inputs.data['transcript']",
        "inputs.data.metadata",
        "inputs.data.price_revision_id",
        "inputs.data.usage.all(item, has(item.reported_usd))",
        "inputs.data.usage.all(item, has(item.observation_id))",
    ],
)
def test_approval_agent_session_inputs_use_frozen_metadata_view(predicate: str) -> None:
    document = direct_document(
        predicate, node("agent"), "AgentSession", output="session"
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.diagnostics.errors[0].code == "IR011"


@pytest.mark.parametrize(
    "schema", [{"type": "null"}, {"const": None}, {"enum": [None]}]
)
def test_approval_direct_inputs_keep_structured_null_distinct(
    schema: JsonObject,
) -> None:
    document = direct_document("inputs.data", producer(schema=schema))

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.diagnostics.errors[0].code == "IR011"


@pytest.mark.parametrize("bounded", [False, True])
def test_approval_direct_inputs_preserve_schema_roots_and_bounds(bounded: bool) -> None:
    schema: JsonObject = {
        "$defs": {"value": {"type": "string", "maxLength": 2}},
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/value"},
                **({"maxItems": 1} if bounded else {}),
            }
        },
        "required": ["items"],
    }
    document = direct_document(
        "inputs.data.items.map(x, inputs.data.items.all(y, true)).size() > 0",
        producer(schema=schema),
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is bounded
    assert result.predicates_checked == 1
    if not bounded:
        assert result.diagnostics.errors[0].code == "IR027"


def test_approval_direct_input_local_reference_roots_remain_separate() -> None:
    schemas: list[JsonObject] = [
        {
            "$defs": {"value": {"type": kind}},
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "required": ["value"],
        }
        for kind in ("boolean", "integer")
    ]
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "left": producer(schema=schemas[0]),
            "right": producer(schema=schemas[1]),
            "gate": gate(
                None,
                "inputs.left.value && inputs.right.value == 1",
                inputs={
                    name: {"type": "StructuredOutput", "from": name, "output": "value"}
                    for name in ("left", "right")
                },
            ),
        },
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.schema_unresolved == 0


def test_approval_direct_inputs_keep_recursive_saved_mapping() -> None:
    schema: JsonObject = {
        "properties": {"next": {"$ref": "#"}, "ok": {"type": "boolean"}},
        "required": ["next", "ok"],
    }
    document = direct_document(
        "inputs.data.next.next.ok",
        producer("example/Recursive"),
        "example/Recursive",
        contracts={**BUILTINS, "example/Recursive": saved_form(schema)},
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.schema_unresolved == 0


@pytest.mark.parametrize(
    "predicate",
    [
        "inputs.data == {}",
        "inputs.data[response.decision]",
        "inputs.data.usage.all(row, row.model == 'known')",
    ],
)
def test_approval_agent_session_permitted_subtrees_keep_static_view(
    predicate: str,
) -> None:
    document = direct_document(
        predicate, node("agent"), "AgentSession", output="session"
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
