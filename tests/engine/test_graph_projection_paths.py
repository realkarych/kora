from dataclasses import replace
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.connection import Database
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.reads import graph_version_by_number
from kora.engine.graph_projection_paths import (
    ProjectionTextBudget,
    build_graph_cel_projection_paths,
    read_graph_cel_projection_paths,
    read_saved_paths,
)
from kora.ir.document import GraphDocument
from kora.ir.graph_projection_paths import (
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionRejected,
    compose_graph_projection_paths,
)
from kora.ir.json import JsonObject
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.graph_inventory_cases import inventory_arguments
from tests.engine.graph_output_cases import LIMIT, child, output_document
from tests.engine.graph_projection_cases import (
    PROJECTION_LIMITS,
    projection_document,
    projection_inputs,
    projection_sites,
)

LIMITS = PROJECTION_LIMITS
EMPTY: JsonObject = {"whole": [], "presence": []}


def projection_entries(value: JsonObject) -> dict[tuple[str, ...], JsonObject]:
    return {
        tuple(cast(list[str], entry["path"])): cast(JsonObject, entry["paths"])
        for entry in cast(list[JsonObject], value["outputs"])
    }


def saved_version() -> tuple[JsonObject, str, JsonObject, str]:
    raw = projection_document()
    ir_text, contract = projection_inputs(raw)
    paths = build_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)
    contract["cel_projection_paths"] = ObjectReader(paths.text).object()
    return raw, ir_text, contract, paths.text


@pytest.mark.asyncio
async def test_direct_projection_paths_match_the_exact_saved_version(
    database: Database,
) -> None:
    raw, _, contract, expected = saved_version()
    prepared = prepare_graph_values(raw, contract, max_encoded_bytes=LIMIT)
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace("projection")
        foreign_namespace = await seeder.namespace("foreign")
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(
            graph,
            2,
            prepared.ir,
            execution_contract=prepared.execution_contract,
            value_encoding_version=prepared.value_encoding_version,
        )
    async with database.read_transaction() as connection:
        row = await graph_version_by_number(connection, namespace, graph, 2)
        absent = await graph_version_by_number(connection, namespace, graph, 1)
        foreign = await graph_version_by_number(connection, foreign_namespace, graph, 2)
    assert row is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    assert isinstance(decoded, DecodedGraphValues)
    checked = read_graph_cel_projection_paths(
        cast(str, row.ir), decoded.execution_contract, limits=LIMITS
    )
    await database.close()
    checked_object = ObjectReader(checked.text).object()
    outputs = projection_entries(checked_object)

    assert row.id == version
    assert absent is foreign is None
    assert checked.text == expected
    assert checked_object["parameters"] == {"whole": [".enabled"], "presence": []}
    assert list(outputs) == [
        ("graph", "nodes", "gate", "outputs", "response"),
        ("graph", "nodes", "presence", "outputs", "response"),
        ("graph", "nodes", "producer", "outputs", "session"),
        ("graph", "nodes", "producer", "outputs", "value"),
        ("graph", "nodes", "wait", "outputs", "event"),
        ("graph", "subgraphs", "unused", "nodes", "gate", "outputs", "response"),
        ("graph", "subgraphs", "unused", "nodes", "producer", "outputs", "session"),
        ("graph", "subgraphs", "unused", "nodes", "producer", "outputs", "value"),
    ]
    assert outputs[("graph", "nodes", "producer", "outputs", "value")] == {
        "whole": [r"['\u00e9']"],
        "presence": [""],
    }
    assert outputs[
        ("graph", "subgraphs", "unused", "nodes", "producer", "outputs", "value")
    ] == {
        "whole": [".other"],
        "presence": [""],
    }
    assert sum(paths == EMPTY for paths in outputs.values()) == 6
    assert ledger.opened == ledger.closed > 0
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


@pytest.mark.parametrize("mutation", ["missing-retry", "changed-retry"])
def test_projection_paths_refuse_incomplete_pinned_context(mutation: str) -> None:
    _, ir_text, contract, _ = saved_version()
    if mutation == "missing-retry":
        del contract["retry_policies"]
    else:
        child(contract, "retry_policies")["interpretation"] = "different"

    with pytest.raises(GraphValueRejected) as refusal:
        read_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)

    assert refusal.value.reason is (
        GraphValueFailure.INVALID
        if mutation == "missing-retry"
        else GraphValueFailure.VERSION
    )


@pytest.mark.parametrize("declaration", ["export", "collect"])
def test_projection_paths_refuse_unread_unsupported_declarations(
    declaration: str,
) -> None:
    raw = output_document()
    child(raw, "graph")["subgraphs"] = {
        "unused": {"nodes": child(output_document(), "graph", "nodes")}
    }
    _, contract = projection_inputs(raw)
    assert child(contract, "cel_expressions")["fields"] == []
    if declaration == "export":
        child(raw, "graph", "subgraphs", "unused")["outputs"] = {
            "unread": {
                "type": "StructuredOutput",
                "from": "producer",
                "output": "value",
            }
        }
    else:
        child(raw, "graph", "nodes", "producer", "outputs", "value")["collect"] = (
            "value"
        )
    ir_text, _ = inventory_arguments(raw)

    with pytest.raises(GraphValueRejected) as refusal:
        build_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)

    assert refusal.value.reason is GraphValueFailure.VERSION


@pytest.mark.parametrize("binding", ["selected", "reserved"])
def test_projection_paths_refuse_unsupported_read_routes(binding: str) -> None:
    raw, _, contract, _ = saved_version()
    source = child(raw, "graph", "nodes", "gate", "inputs", "data")
    if binding == "selected":
        source["path"] = "value"
    else:
        source["from"] = "$params"
    ir_text, _ = inventory_arguments(raw)

    with pytest.raises(GraphValueRejected) as refusal:
        read_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)

    assert refusal.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize("mutation", ["association", "missing", "noncanonical"])
def test_projection_paths_refuse_drift(mutation: str) -> None:
    _, ir_text, contract, _ = saved_version()
    entries = cast(list[JsonObject], child(contract, "cel_projection_paths")["outputs"])
    if mutation == "association":
        entries[3]["paths"], entries[-1]["paths"] = (
            entries[-1]["paths"],
            entries[3]["paths"],
        )
    elif mutation == "missing":
        entries.pop()
    else:
        cast(JsonObject, entries[-1]["paths"])["presence"] = ["", ""]

    with pytest.raises(GraphValueRejected) as refusal:
        read_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)

    assert refusal.value.reason is GraphValueFailure.INVALID


def test_projection_paths_never_serialize_or_retain_private_failures() -> None:
    _, ir_text, contract, _ = saved_version()
    marker = "private-cel-projection"
    paths = read_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)
    entries = cast(list[JsonObject], child(contract, "cel_projection_paths")["outputs"])
    entries[-1]["paths"] = {"whole": [marker], "presence": []}

    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(paths)
    try:
        raise LookupError(marker)
    except LookupError:
        with pytest.raises(GraphValueRejected) as refusal:
            read_graph_cel_projection_paths(ir_text, contract, limits=LIMITS)
    error = refusal.value
    traceback = error.__traceback__
    frames: list[str] = []
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        if traceback.tb_frame.f_code.co_name == "read_graph_cel_projection_paths":
            assert {"ir_text", "execution_contract"}.isdisjoint(
                traceback.tb_frame.f_locals
            )
        traceback = traceback.tb_next

    assert marker not in str(error)
    assert error.__context__ is error.__cause__ is None
    assert "checked_projection_paths" not in frames
    assert "projection_paths_result" not in frames


def test_projection_paths_charge_supplied_and_expected_text_together() -> None:
    _, ir_text, contract, expected = saved_version()
    saved = child(contract, "cel_projection_paths")
    entries = cast(list[JsonObject], saved["outputs"])
    output_paths = projection_entries(saved)
    path_encoding = sum(
        len(render_object({"path": list(path)}, TextBudget(LIMIT)))
        - len('{"path":}')
        + len(path)
        for path in output_paths
    )
    list_staging = sum(
        len(cast(list[str], paths["whole"])) + len(cast(list[str], paths["presence"]))
        for paths in [child(saved, "parameters"), *output_paths.values()]
    )
    exact = 2 * len(expected) + path_encoding + list_staging
    exact_limits = replace(LIMITS, section_bytes=exact)
    short_limits = replace(LIMITS, section_bytes=exact - 1)

    accepted = read_graph_cel_projection_paths(ir_text, contract, limits=exact_limits)
    with pytest.raises(GraphValueRejected) as exhausted:
        read_graph_cel_projection_paths(ir_text, contract, limits=short_limits)
    cast(JsonObject, entries[-1]["paths"])["whole"] = [".wrong"]
    with pytest.raises(GraphValueRejected) as mismatch:
        read_graph_cel_projection_paths(ir_text, contract, limits=exact_limits)

    assert accepted.text == expected
    assert exhausted.value.reason is GraphValueFailure.BUDGET
    assert mismatch.value.reason is GraphValueFailure.INVALID


def test_projection_paths_admit_repeated_sources_with_unchanged_result() -> None:
    raw = output_document("human_input")
    base_ir, base = projection_inputs(raw)
    child(raw, "graph")["description"] = "x" * 10_000
    wider_ir, wider = projection_inputs(raw)
    narrow = replace(LIMITS, cel=replace(LIMITS.cel, output_context_bytes=20_000))
    exact_source = (
        2 * narrow.cel.ir_bytes
        + 8 * narrow.cel.output_context_bytes
        + 3 * narrow.cel.parameter_context_bytes
        + narrow.cel.comparison_bytes
        + 2 * narrow.retry_bytes
        + len(base_ir)
    )
    exact = replace(narrow, source_bytes=exact_source)
    short = replace(exact, source_bytes=exact_source - 1)

    first = build_graph_cel_projection_paths(base_ir, base, limits=exact)
    second = build_graph_cel_projection_paths(wider_ir, wider, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as exhausted:
        build_graph_cel_projection_paths(wider_ir, wider, limits=narrow)
    with pytest.raises(GraphValueRejected) as aggregate_exhausted:
        build_graph_cel_projection_paths(base_ir, base, limits=short)

    assert first.text == second.text
    assert exhausted.value.reason is GraphValueFailure.BUDGET
    assert aggregate_exhausted.value.reason is GraphValueFailure.BUDGET


def test_projection_path_lexer_preserves_work_exhaustion() -> None:
    path = ".some_path"
    before_lexer = len(path) + 1
    admission = ProjectionAdmission(before_lexer, LIMIT)

    with pytest.raises(ProjectionRejected) as exhausted:
        admission.parsed_path(path)

    assert admission.steps == before_lexer
    assert exhausted.value.reason is ProjectionFailure.CAPACITY


def test_projection_rendering_obeys_work_and_scratch_admission() -> None:
    raw = output_document("human_input")
    ir_text, contract = projection_inputs(raw)
    scratch_envelope = (
        LIMITS.cel.ir_bytes
        + LIMITS.cel.typed_bytes
        + len(ir_text)
        + 4 * LIMITS.section_bytes
    )
    short = replace(LIMITS, scratch_bytes=scratch_envelope - 1)
    admission = ProjectionAdmission(256, LIMIT)
    budget = ProjectionTextBudget(LIMIT, admission)
    saved: JsonObject = {
        "interpretation": "kora.cel-projection-paths.v1",
        "parameters": {"whole": [], "presence": []},
        "outputs": [{"path": ["x" * 1024], "paths": {"whole": [], "presence": []}}],
    }

    with pytest.raises(GraphValueRejected) as scratch_exhausted:
        build_graph_cel_projection_paths(ir_text, contract, limits=short)
    with pytest.raises(ProjectionRejected) as work_exhausted:
        read_saved_paths(saved, budget)

    assert scratch_exhausted.value.reason is GraphValueFailure.BUDGET
    assert work_exhausted.value.reason is ProjectionFailure.CAPACITY
    assert admission.steps == 256


def test_projection_reader_rejects_nonstring_scalars_before_rendering() -> None:
    admission = ProjectionAdmission(128, LIMIT)
    budget = ProjectionTextBudget(LIMIT, admission)
    saved: JsonObject = {
        "interpretation": "kora.cel-projection-paths.v1",
        "parameters": {"whole": [10**10_000], "presence": []},
        "outputs": [],
    }

    with pytest.raises(GraphValueRejected) as refusal:
        read_saved_paths(saved, budget)

    assert refusal.value.reason is GraphValueFailure.INVALID
    assert budget.remaining == 128


def test_projection_trie_charges_whole_subsumed_source_reads() -> None:
    raw = projection_document()
    _, contract = projection_inputs(raw)
    declarations = [
        cast(list[str], entry["path"])
        for entry in cast(list[JsonObject], contract["outputs"])
    ]
    nodes = child(raw, "graph", "nodes")
    child(nodes, "gate", "config")["approve_when"] = (
        "has(inputs.data) && inputs.data == null"
    )
    base = GraphDocument.model_validate(raw)
    baseline_sites = projection_sites(base)
    child(nodes, "presence", "config")["approve_when"] = (
        "has(inputs.data) && inputs.data.long_prefix == true"
    )
    wider = GraphDocument.model_validate(raw)
    wider_sites = projection_sites(wider)
    before = ProjectionAdmission(LIMIT, LIMIT)
    after = ProjectionAdmission(LIMIT, LIMIT)

    first = compose_graph_projection_paths(base, baseline_sites, declarations, before)
    second = compose_graph_projection_paths(wider, wider_sites, declarations, after)
    scratch = LIMIT - after.scratch_remaining
    exact = ProjectionAdmission(after.steps, scratch)
    repeated = compose_graph_projection_paths(wider, wider_sites, declarations, exact)
    with pytest.raises(ProjectionRejected) as work_exhausted:
        compose_graph_projection_paths(
            wider,
            wider_sites,
            declarations,
            ProjectionAdmission(after.steps - 1, scratch),
        )
    with pytest.raises(ProjectionRejected) as scratch_exhausted:
        compose_graph_projection_paths(
            wider,
            wider_sites,
            declarations,
            ProjectionAdmission(after.steps, scratch - 1),
        )

    assert first.outputs == second.outputs == repeated.outputs
    assert before.steps < after.steps
    assert before.scratch_remaining > after.scratch_remaining
    assert exact.scratch_remaining == 0
    assert work_exhausted.value.reason is ProjectionFailure.CAPACITY
    assert scratch_exhausted.value.reason is ProjectionFailure.CAPACITY


def test_projection_order_admits_shared_prefix_work_and_scratch() -> None:
    shared_keys = ["a" * 512 + suffix for suffix in ("z", "b")]
    separate_keys = ["c" + shared_keys[0][1:], shared_keys[1]]
    separate = ProjectionAdmission(LIMIT, LIMIT)
    shared = ProjectionAdmission(LIMIT, LIMIT)

    separate.order(separate_keys)
    ordered = shared.order(shared_keys)
    scratch = LIMIT - shared.scratch_remaining
    at_limit = ProjectionAdmission(shared.steps, scratch).order(shared_keys)
    with pytest.raises(ProjectionRejected) as work_exhausted:
        ProjectionAdmission(shared.steps - 1, scratch).order(shared_keys)
    with pytest.raises(ProjectionRejected) as scratch_exhausted:
        ProjectionAdmission(shared.steps, scratch - 1).order(shared_keys)

    assert at_limit == ordered == tuple(reversed(shared_keys))
    assert shared.steps > separate.steps
    assert work_exhausted.value.reason is ProjectionFailure.CAPACITY
    assert scratch_exhausted.value.reason is ProjectionFailure.CAPACITY
