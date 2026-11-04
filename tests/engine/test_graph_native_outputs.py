import cProfile
import hashlib
from typing import cast

import pytest

from kora.cel import (
    CheckedExpression,
    Environment,
    ResultKind,
    SchemaTypes,
    Variable,
    check_typed,
)
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_native_outputs import (
    build_graph_native_output_contract,
    native_output_type,
    read_graph_native_output_contract,
)
from kora.engine.graph_native_sources import VIEW_BYTES
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_types import build_graph_type_contracts
from kora.ir.agent_session_view import published_object
from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonValue
from tests.engine.graph_native_cases import (
    native_arguments,
    native_document,
    native_record,
)
from tests.engine.graph_output_cases import LIMIT, child, input_bytes


def no_documents(uri: str) -> JsonValue | None:
    return None


@pytest.mark.parametrize("name", ["AgentSession", "CommandResult", "VcsRef"])
@pytest.mark.parametrize("nested", [False, True])
def test_native_contract_selects_published_direct_role(name: str, nested: bool) -> None:
    arguments = native_arguments(name, nested)

    result = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    saved = ObjectReader(result.text).object()
    restored = read_graph_native_output_contract(
        *arguments, saved, result.digest, max_encoded_bytes=LIMIT
    )

    assert restored == result
    assert list(saved) == [
        "execution_semantics",
        "declared_type",
        "type_contracts",
        "inline_schema",
        "checker_binding",
        "checker",
        "validation",
    ]
    assert saved["declared_type"] == name
    assert saved["inline_schema"] is None
    assert saved["validation"] == {"kind": "published_builtin", "name": name}
    record = native_record(saved)
    assert record["name"] == name
    assert record["builtin_revision"] == "1"
    assert record["parent"] is None
    assert record["behavior"] == child(record, "descriptor")["behavior"]
    if name == "AgentSession":
        assert saved["checker_binding"] == "kora.agent-session-view.v1"
        checker = child(saved, "checker")
        assert list(checker) == ["kind", "name", "policy", "schema"]
        assert checker["kind"] == "published_view"
        assert child(checker, "schema")["$id"] == "urn:kora:artifact:AgentSession:cel:1"
        assert child(checker, "schema") != child(record, "schema")
    else:
        assert saved["checker_binding"] is None
        assert saved["checker"] == {"kind": "declared_type", "name": name}


@pytest.mark.parametrize("name", ["AgentSession", "VcsRef"])
def test_native_contract_ignores_separate_structured_config(name: str) -> None:
    first = native_document(name)
    second = native_document(name)
    child(first, "graph", "nodes", "producer", "config")["output_schema"] = {
        "type": "string"
    }
    child(second, "graph", "nodes", "producer", "config")["output_schema"] = {
        "type": "integer"
    }

    left = build_graph_native_output_contract(
        *native_arguments(name, raw=first), max_encoded_bytes=LIMIT
    )
    right = build_graph_native_output_contract(
        *native_arguments(name, raw=second), max_encoded_bytes=LIMIT
    )

    assert left == right


@pytest.mark.parametrize("name", ["AgentSession", "CommandResult", "VcsRef"])
@pytest.mark.parametrize("mode", ["inline", "custom", "list", "wrong_role", "wrapper"])
def test_native_contract_refuses_wider_composition(name: str, mode: str) -> None:
    arguments = native_arguments(name)
    raw = ObjectReader(arguments[0]).object()
    node = child(raw, "graph", "nodes", "producer")
    if mode in {"custom", "list"} and name != "VcsRef":
        arguments[2][-1] = "value"
    declaration = child(node, "outputs", arguments[2][-1])
    if mode == "inline":
        declaration["schema"] = {}
    elif mode == "custom":
        declaration["type"] = "custom/Native"
        child(raw, "type_contracts")["custom/Native"] = {
            "parent": name,
            "schema": {"$schema": SCHEMA_DIALECT},
            "behavior": {
                "validation": "json_schema",
                "digest_fields": "all",
                "summary": "all",
            },
        }
    elif mode == "list":
        declaration["type"] = "List[" + name + "]"
    elif mode == "wrong_role":
        node["kind"] = "timer"
        node["config"] = {"duration": "0s"}
    else:
        arguments[2][:] = ["graph", "subgraphs", "body", "outputs", "result"]
    altered_ir = render_object(raw, TextBudget(LIMIT))
    records = saved_array(
        build_graph_type_contracts(altered_ir, max_encoded_bytes=LIMIT)
    )

    with pytest.raises(GraphValueRejected):
        build_graph_native_output_contract(
            altered_ir, records, arguments[2], max_encoded_bytes=LIMIT
        )
    if mode != "wrapper":
        with pytest.raises(GraphValueRejected) as unsupported:
            native_output_type(raw, arguments[2])
        assert unsupported.value.reason is GraphValueFailure.VERSION


@pytest.mark.parametrize(
    "field",
    [
        "execution_semantics",
        "declared_type",
        "type_contracts",
        "inline_schema",
        "checker_binding",
        "checker",
        "validation",
        "policy",
        "schema",
        "root",
        "native_schema",
        "descriptor",
        "behavior",
        "order",
        "missing",
        "extra",
    ],
)
def test_native_reader_refuses_rehashed_substitution(field: str) -> None:
    arguments = native_arguments("AgentSession")
    original = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    saved = ObjectReader(original.text).object()
    if field == "missing":
        del saved["checker_binding"]
    elif field == "order":
        saved = dict(reversed(saved.items()))
    elif field in {"policy", "schema"}:
        child(saved, "checker")[field] = {}
    elif field == "root":
        child(saved, "checker", "schema")["$id"] = "urn:kora:artifact:AgentSession"
    elif field in {"native_schema", "descriptor", "behavior"}:
        native_record(saved)["schema" if field == "native_schema" else field] = {}
    else:
        saved[field] = "substituted"
    text = render_object(saved, TextBudget(LIMIT))
    digest = hashlib.sha256(text.encode("ascii")).hexdigest()

    with pytest.raises(GraphValueRejected, match="invalid graph value representation"):
        read_graph_native_output_contract(
            *arguments, saved, digest, max_encoded_bytes=LIMIT
        )


@pytest.mark.parametrize("name", ["AgentSession", "CommandResult", "VcsRef"])
def test_native_contract_admission_and_detachment(name: str) -> None:
    arguments = native_arguments(name)
    expected = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    saved = ObjectReader(expected.text).object()
    source_bytes = VIEW_BYTES if name == "AgentSession" else 0
    exact = input_bytes(arguments) + len(expected.text) + source_bytes

    built = build_graph_native_output_contract(*arguments, max_encoded_bytes=exact)
    restored = read_graph_native_output_contract(
        *arguments, saved, expected.digest, max_encoded_bytes=exact
    )
    with pytest.raises(GraphValueRejected) as failure:
        build_graph_native_output_contract(*arguments, max_encoded_bytes=exact - 1)
    with pytest.raises(GraphValueRejected) as reader_failure:
        read_graph_native_output_contract(
            *arguments, saved, expected.digest, max_encoded_bytes=exact - 1
        )
    saved.clear()
    arguments[1].clear()

    assert built == restored == expected
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert reader_failure.value.reason is GraphValueFailure.BUDGET


@pytest.mark.parametrize("stage", ["record", "source"])
def test_native_reader_admits_before_source_acquisition(stage: str) -> None:
    arguments = native_arguments("AgentSession")
    expected = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    saved = ObjectReader(expected.text).object()
    limit = input_bytes(arguments) + len(expected.text) - 1
    if stage == "source":
        limit += VIEW_BYTES
    profile = cProfile.Profile()

    with pytest.raises(GraphValueRejected) as failure:
        profile.runcall(
            read_graph_native_output_contract,
            *arguments,
            saved,
            expected.digest,
            max_encoded_bytes=limit,
        )

    stats = profile.getstats()
    names = {entry.code.co_name for entry in stats if not isinstance(entry.code, str)}
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert all(entry.code is not published_object.__code__ for entry in stats)
    if stage == "record":
        assert "derive_native_output" not in names
        assert "read_document" not in names


@pytest.mark.parametrize(
    ("expression", "valid"),
    [
        ("session.harness == 'claude'", True),
        ("session.usage.all(item, item.model == 'known')", True),
        ("session.provenance.worker_id == 'worker'", True),
        ("session.transcript.capture == 'captured'", False),
        ("size(session.denials.items) == 0", False),
        ("session.metadata.harness == 'claude'", False),
    ],
)
def test_saved_native_view_controls_real_cel(expression: str, valid: bool) -> None:
    contract = build_graph_native_output_contract(
        *native_arguments("AgentSession"), max_encoded_bytes=LIMIT
    )
    saved = ObjectReader(contract.text).object()
    schema = child(saved, "checker", "schema")
    mapped = SchemaTypes(no_documents).document(schema)
    environment = Environment({"session": Variable(mapped)}, ResultKind.BOOL)

    checked = check_typed(expression, environment)

    assert isinstance(checked, CheckedExpression) is valid
    assert cast(str, schema["$id"]) == "urn:kora:artifact:AgentSession:cel:1"


@pytest.mark.parametrize("digest", ["", "F" * 64, "z" * 64, "0" * 64])
def test_native_reader_refuses_digest_before_derivation(digest: str) -> None:
    arguments = native_arguments("AgentSession")
    built = build_graph_native_output_contract(*arguments, max_encoded_bytes=LIMIT)
    saved = ObjectReader(built.text).object()
    profile = cProfile.Profile()

    with pytest.raises(GraphValueRejected) as failure:
        profile.runcall(
            read_graph_native_output_contract,
            *arguments,
            saved,
            digest,
            max_encoded_bytes=LIMIT,
        )

    assert failure.value.reason is GraphValueFailure.INVALID
    names = {
        entry.code.co_name
        for entry in profile.getstats()
        if not isinstance(entry.code, str)
    }
    assert "derive_native_output" not in names
