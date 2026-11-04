import cProfile
import hashlib
import sys
from collections.abc import Generator
from contextlib import contextmanager
from types import CodeType, FrameType
from typing import cast

import pytest

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine import graph_types
from kora.engine.graph_native_outputs import build_graph_native_output_contract
from kora.engine.graph_native_sources import VIEW_BYTES
from kora.engine.graph_output_inventory import (
    build_graph_output_inventory,
    read_graph_output_inventory,
)
from kora.engine.graph_output_values import GraphOutputContract
from kora.engine.graph_outputs import build_graph_output_contract
from kora.engine.graph_schema_preparation import prepare_graph_schema
from kora.ir.json import JsonObject
from kora.ir.output_schemas import validate_output_schemas
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from tests.engine.graph_inventory_cases import (
    canonical_size,
    complete_inventory,
    entries,
    inventory_arguments,
    mixed_document,
)
from tests.engine.graph_output_cases import LIMIT, child, output_document
from tests.engine.graph_schema_cases import declaration


def calls(profile: cProfile.Profile, name: str) -> int:
    return sum(
        entry.callcount
        for entry in profile.getstats()
        if not isinstance(entry.code, str) and entry.code.co_name == name
    )


@contextmanager
def observe_calls(owners: tuple[CodeType, ...]) -> Generator[list[int]]:
    if sys.getprofile() is not None:
        raise RuntimeError("source-pass observer already active")
    counts = [0] * len(owners)

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event != "call":
            return
        code = frame.f_code
        for index, owner in enumerate(owners):
            if code is owner:
                counts[index] += 1
                return

    try:
        sys.setprofile(observe)
        yield counts
    finally:
        sys.setprofile(None)


def test_complete_inventory_preserves_mixed_root_and_unused_body_leaves() -> None:
    arguments = inventory_arguments(mixed_document())

    text = complete_inventory(arguments)
    saved = entries(text)
    restored = read_graph_output_inventory(*arguments, saved, max_encoded_bytes=LIMIT)
    leaves: list[GraphOutputContract | StructuredSchemaPreparationRefusal] = []
    for record in saved:
        builder = (
            build_graph_output_contract
            if child(record, "contract")["declared_type"] == "StructuredOutput"
            else build_graph_native_output_contract
        )
        leaves.append(builder(*arguments, record["path"], max_encoded_bytes=LIMIT))

    paths = [record["path"] for record in saved]
    assert paths == [
        ["graph", "nodes", "command", "outputs", "result"],
        ["graph", "nodes", "command", "outputs", "value"],
        ["graph", "nodes", "producer", "outputs", "publication"],
        ["graph", "nodes", "producer", "outputs", "session"],
        ["graph", "nodes", "producer", "outputs", "value"],
        ["graph", "subgraphs", "unused", "nodes", "approve", "outputs", "response"],
        ["graph", "subgraphs", "unused", "nodes", "wait", "outputs", "event"],
    ]
    assert restored == text
    for record, leaf in zip(saved, leaves, strict=True):
        assert list(record) == ["path", "contract", "contract_digest"]
        assert isinstance(leaf, GraphOutputContract)
        assert record["contract_digest"] == leaf.digest
        assert render_object(record["contract"], TextBudget(LIMIT)) == leaf.text
    default = child(saved[5], "contract", "inline_schema")
    assert default["origin"] == "approval_form_default"


def test_inventory_uses_saved_structured_ancestry() -> None:
    raw = output_document()
    child(raw, "type_contracts")["example/Specific"] = declaration({"type": "string"})
    child(raw, "graph", "nodes", "producer", "outputs", "value")["type"] = (
        "example/Specific"
    )
    arguments = inventory_arguments(raw)

    text = complete_inventory(arguments)

    contract = child(entries(text)[1], "contract")
    assert contract["declared_type"] == "example/Specific"
    assert contract["constraints"] == [
        {"kind": "type_schema", "name": "StructuredOutput"},
        {"kind": "type_schema", "name": "example/Specific"},
    ]


@pytest.mark.parametrize("nested", [False, True])
def test_empty_inventory_still_requires_real_document_and_types(nested: bool) -> None:
    raw = output_document()
    graph = child(raw, "graph")
    graph["nodes"] = {"timer": {"kind": "timer", "config": {"duration": "0s"}}}
    if nested:
        graph["subgraphs"] = {"body": {"nodes": graph["nodes"], "outputs": {}}}
    raw["type_contracts"] = {}
    arguments = inventory_arguments(raw)
    exact = len(arguments[0]) + canonical_size(arguments[1]) + 2
    profile = cProfile.Profile()

    text = profile.runcall(
        build_graph_output_inventory, *arguments, max_encoded_bytes=exact
    )
    restored = read_graph_output_inventory(*arguments, [], max_encoded_bytes=exact + 2)
    with pytest.raises(GraphValueRejected) as failure:
        build_graph_output_inventory(*arguments, max_encoded_bytes=exact - 1)

    assert text == restored == "[]"
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert calls(profile, "read_document") == 2
    assert calls(profile, "read_graph_type_contracts") == 1
    assert calls(profile, "validate_output_schemas") == 1


@pytest.mark.parametrize("mode", ["missing_role", "missing_types", "altered_types"])
def test_empty_saved_inventory_cannot_hide_source_failures(mode: str) -> None:
    arguments = inventory_arguments(output_document())
    raw = ObjectReader(arguments[0]).object()
    types = arguments[1]
    if mode == "missing_role":
        child(raw, "graph", "nodes", "producer")["outputs"] = {}
    elif mode == "missing_types":
        types = []
    else:
        cast(JsonObject, types[0])["schema"] = {}
    text = render_object(raw, TextBudget(LIMIT))

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_inventory(text, types, [], max_encoded_bytes=LIMIT)

    assert failure.value.reason is (
        GraphValueFailure.INVALID
        if mode == "missing_role"
        else GraphValueFailure.BUDGET
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "extra",
        "duplicate",
        "order",
        "member_order",
        "extra_member",
        "path",
        "digest",
        "rehash",
        "swap",
    ],
)
def test_inventory_reader_refuses_changed_association(mode: str) -> None:
    arguments = inventory_arguments(output_document("command"))
    saved = entries(complete_inventory(arguments))
    if mode == "missing":
        saved.pop()
    elif mode == "extra":
        extra = dict(saved[0])
        extra["path"] = ["graph", "nodes", "absent", "outputs", "result"]
        saved.append(extra)
    elif mode == "duplicate":
        saved.append(dict(saved[0]))
    elif mode == "order":
        saved.reverse()
    elif mode == "member_order":
        saved[0] = dict(reversed(saved[0].items()))
    elif mode == "extra_member":
        saved[0]["extra"] = None
    elif mode == "path":
        saved[0]["path"] = ["graph", "nodes", "producer.result", "outputs", "result"]
    elif mode == "digest":
        saved[0]["contract_digest"] = "F" * 64
    elif mode == "rehash":
        child(saved[0], "contract")["declared_type"] = "VcsRef"
        changed = render_object(saved[0]["contract"], TextBudget(LIMIT))
        saved[0]["contract_digest"] = hashlib.sha256(
            changed.encode("ascii")
        ).hexdigest()
    else:
        saved[0]["contract"], saved[1]["contract"] = (
            saved[1]["contract"],
            saved[0]["contract"],
        )
        saved[0]["contract_digest"], saved[1]["contract_digest"] = (
            saved[1]["contract_digest"],
            saved[0]["contract_digest"],
        )

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_inventory(*arguments, saved, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID


def test_equal_native_contracts_keep_separate_declarations() -> None:
    raw = output_document("command")
    node = child(raw, "graph", "nodes", "producer")
    child(node, "outputs").pop("value")
    child(node, "config").pop("output_file")
    child(raw, "graph")["subgraphs"] = {"body": {"nodes": {"command": node}}}
    arguments = inventory_arguments(raw)
    text = complete_inventory(arguments)
    saved = entries(text)
    saved[0]["contract"], saved[1]["contract"] = (
        saved[1]["contract"],
        saved[0]["contract"],
    )

    restored = read_graph_output_inventory(*arguments, saved, max_encoded_bytes=LIMIT)

    assert restored == text
    assert saved[0]["path"] != saved[1]["path"]
    assert saved[0]["contract_digest"] == saved[1]["contract_digest"]


@pytest.mark.parametrize("mode", ["list", "native_inline", "native_custom", "export"])
def test_unsupported_late_output_refuses_complete_inventory(mode: str) -> None:
    raw = output_document()
    outputs = child(raw, "graph", "nodes", "producer", "outputs")
    if mode == "list":
        outputs["z_later"] = {"type": "List[StructuredOutput]"}
    elif mode == "native_inline":
        child(raw, "type_contracts")["VcsRef"] = {"builtin_revision": "1"}
        outputs["z_later"] = {"type": "VcsRef", "schema": {}}
    elif mode == "native_custom":
        child(raw, "type_contracts")["example/Session"] = declaration(
            {}, "AgentSession"
        )
        outputs["z_later"] = {"type": "example/Session"}
    else:
        child(raw, "graph")["subgraphs"] = {
            "body": {
                "nodes": {
                    "wait": child(
                        output_document("wait_event"), "graph", "nodes", "producer"
                    )
                },
                "outputs": {
                    "export": {
                        "type": "StructuredOutput",
                        "from": "wait",
                        "output": "event",
                    }
                },
            }
        }
    arguments = inventory_arguments(raw)
    profile = cProfile.Profile()

    with pytest.raises(GraphValueRejected) as failure:
        profile.runcall(
            build_graph_output_inventory, *arguments, max_encoded_bytes=LIMIT
        )

    assert failure.value.reason is GraphValueFailure.VERSION
    assert calls(profile, "build_graph_output_contract") == 1 + (mode == "export")
    assert calls(profile, "build_graph_native_output_contract") == 1 + (
        mode == "native_inline"
    )


@pytest.mark.parametrize(
    "schema", [{"pattern": "a"}, {"$ref": "#"}, {"description": "\ud800"}]
)
def test_inventory_preserves_real_preparation_refusal(schema: JsonObject) -> None:
    raw = output_document()
    child(raw, "graph", "nodes", "producer", "outputs", "value")["schema"] = schema
    child(raw, "graph", "nodes")["z_later"] = child(
        output_document("wait_event"), "graph", "nodes", "producer"
    )
    arguments = inventory_arguments(raw)
    profile = cProfile.Profile()

    result = profile.runcall(
        build_graph_output_inventory, *arguments, max_encoded_bytes=LIMIT
    )
    restored = read_graph_output_inventory(*arguments, [], max_encoded_bytes=LIMIT)

    assert isinstance(result, StructuredSchemaPreparationRefusal)
    assert restored == result
    assert calls(profile, "build_graph_output_contract") == 1


@pytest.mark.parametrize("kind", ["agent", "command", "human_input", "wait_event"])
def test_inventory_exact_cumulative_budget_and_source_passes(kind: str) -> None:
    raw = output_document(kind)
    raw["x-unrelated"] = "x" * 8192
    child(raw, "type_contracts")["example/Unrelated"] = declaration(
        {"description": "x" * 4096}
    )
    arguments = inventory_arguments(raw)
    text = complete_inventory(arguments)
    saved = entries(text)
    source = len(arguments[0]) + canonical_size(arguments[1])
    paths = sum(canonical_size(entry["path"]) for entry in saved)
    native = int(kind in {"agent", "command"})
    exact = (
        source
        + paths
        + len(text)
        + len(saved) * source
        + paths
        + (VIEW_BYTES if kind == "agent" else 0)
    )
    owners = (
        graph_types.read_document.__code__,
        graph_types.read_graph_type_contracts.__code__,
        validate_output_schemas.__code__,
        prepare_graph_schema.__code__,
    )

    with observe_calls(owners) as counts:
        actual = build_graph_output_inventory(*arguments, max_encoded_bytes=exact)
    restored = read_graph_output_inventory(
        *arguments, saved, max_encoded_bytes=exact + len(text)
    )
    with pytest.raises(GraphValueRejected) as failure:
        build_graph_output_inventory(*arguments, max_encoded_bytes=exact - 1)
    with pytest.raises(GraphValueRejected) as reader_failure:
        read_graph_output_inventory(
            *arguments, saved, max_encoded_bytes=exact + len(text) - 1
        )
    saved.clear()
    arguments[1].clear()

    assert actual == restored == text
    assert (
        failure.value.reason is reader_failure.value.reason is GraphValueFailure.BUDGET
    )
    document_reads, type_reads, output_passes, preparations = counts
    assert document_reads == 2 + 2 * native + 3
    assert type_reads == 1 + native + 2
    assert output_passes == 1 + native + 1
    assert preparations == 1


@pytest.mark.parametrize("stage", ["saved", "first", "second"])
def test_inventory_admits_before_any_unpaid_leaf(stage: str) -> None:
    arguments = inventory_arguments(output_document("command"))
    text = complete_inventory(arguments)
    saved = entries(text)
    source = len(arguments[0]) + canonical_size(arguments[1])
    paths = sum(canonical_size(entry["path"]) for entry in saved)
    framing = len('{"path":,"contract":,"contract_digest":""}') + 64
    first_path = canonical_size(saved[0]["path"])
    limit = source + paths + 2 + framing + first_path + source + first_path - 1
    if stage == "saved":
        limit = source + len(text) - 1
    elif stage == "second":
        limit += 1 + len(render_object(saved[0]["contract"], TextBudget(LIMIT)))
        second_path = canonical_size(saved[1]["path"])
        limit += 1 + framing + second_path + source + second_path - 1
    profile = cProfile.Profile()

    if stage == "saved":
        with pytest.raises(GraphValueRejected) as failure:
            profile.runcall(
                read_graph_output_inventory, *arguments, saved, max_encoded_bytes=limit
            )
    else:
        with pytest.raises(GraphValueRejected) as failure:
            profile.runcall(
                build_graph_output_inventory, *arguments, max_encoded_bytes=limit
            )

    assert failure.value.reason is GraphValueFailure.BUDGET
    assert calls(profile, "build_graph_native_output_contract") == (stage == "second")
    assert calls(profile, "build_graph_output_contract") == 0
    if stage == "saved":
        assert calls(profile, "read_document") == 0


@pytest.mark.parametrize(
    "mode", ["noncanonical", "foreign", "collision", "cycle", "tuple"]
)
def test_inventory_capture_refuses_ambiguous_or_foreign_input(mode: str) -> None:
    arguments = inventory_arguments(output_document("command"))
    text = complete_inventory(arguments)
    saved: object = entries(text)
    ir = arguments[0]
    if mode == "noncanonical":
        ir = ir.replace('"ir_version":', '"ir_version": ')
    elif mode == "foreign":
        saved = [object()]
    elif mode == "collision":
        saved = [{"\U0001f600": 1, "\ud83d\ude00": 2}]
    elif mode == "cycle":
        cyclic: list[object] = []
        cyclic.append(cyclic)
        saved = cyclic
    else:
        saved = tuple(entries(text))

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_inventory(ir, arguments[1], saved, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID


def test_observer_counts_exact_code_including_recursive_entries() -> None:
    def visit(depth: int) -> int:
        return 0 if depth == 0 else 1 + visit(depth - 1)

    class Other:
        @staticmethod
        def visit(depth: int) -> int:
            return depth

    with observe_calls((visit.__code__,)) as counts:
        result = visit(2) + visit(0) + Other.visit(1)

    assert result == 3
    assert counts == [4]
    assert sys.getprofile() is None


def test_observer_preserves_occupied_slot_and_clears_after_refusal() -> None:
    arguments = inventory_arguments(output_document("command"))
    owners = (graph_types.read_document.__code__,)

    with observe_calls(owners):
        incumbent: object = sys.getprofile()
        with (
            pytest.raises(RuntimeError, match="source-pass observer already active"),
            observe_calls(owners),
        ):
            pass
        after_nested: object = sys.getprofile()
    with pytest.raises(GraphValueRejected) as failure, observe_calls(owners):
        build_graph_output_inventory(*arguments, max_encoded_bytes=1)

    assert incumbent is not None
    assert incumbent is after_nested
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert sys.getprofile() is None
