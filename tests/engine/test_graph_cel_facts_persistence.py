from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import insert

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
from kora.db.tables import graph_version
from kora.engine.graph_cel_facts import (
    GraphCelReadLimits,
    read_graph_cel_facts,
    stage_graph_cel_facts,
)
from kora.engine.graph_output_inventory import read_graph_output_inventory
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_parameters import build_graph_parameter_binding
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.document import GraphDocument
from kora.ir.graph_cel_inventory import checked_graph_sites
from kora.ir.json import JsonObject
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_inventory_cases import (
    complete_inventory,
    inventory_arguments,
)
from tests.engine.graph_output_cases import (
    LIMIT,
    child,
    output_document,
    output_preparation_capacity_document,
)

READ_LIMITS: GraphCelReadLimits = GraphCelReadLimits(
    ir_bytes=LIMIT,
    output_context_bytes=LIMIT,
    parameter_context_bytes=LIMIT,
    comparison_bytes=LIMIT,
    declarations=10,
    triggers=0,
    typed_work=100_000,
    typed_bytes=100_000,
)


def cel_rows(contract: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], child(contract, "cel_expressions")["fields"])


def expression_version() -> tuple[JsonObject, str, JsonObject, str]:
    raw = output_document("human_input")
    child(raw, "graph")["params"] = {
        "type": "object",
        "properties": {"enabled": {"type": "boolean"}},
        "required": ["enabled"],
    }
    child(raw, "runtime")["infrastructure_retry"] = {
        "max_attempts": 1,
        "predicate": 'params.enabled && "😀".matches("^😀$")',
    }
    document = GraphDocument.model_validate(raw)
    ir_text, types = inventory_arguments(raw)
    sites = checked_graph_sites(
        document,
        READ_LIMITS.declarations,
        READ_LIMITS.triggers,
        admission=CheckedInventoryAdmission(
            READ_LIMITS.typed_work, READ_LIMITS.typed_bytes
        ),
    )
    assert isinstance(sites, tuple)
    facts = stage_graph_cel_facts(sites, LIMIT).text
    contract: JsonObject = {
        "semantics": "kora.execution.v1",
        "type_contracts": types,
        "outputs": saved_array(complete_inventory((ir_text, types))),
        "parameters": ObjectReader(
            build_graph_parameter_binding(document, max_encoded_bytes=LIMIT)
        ).object(),
        "cel_expressions": ObjectReader(facts).object(),
    }
    return raw, ir_text, contract, facts


@pytest.mark.asyncio
async def test_saved_cel_facts_match_the_pinned_version_on_both_databases(
    database: Database,
) -> None:
    raw, _, contract, expected = expression_version()
    altered = ObjectReader(render_object(contract, TextBudget(LIMIT))).object()
    cost = child(cel_rows(altered)[0], "cost")
    cost["steps"] = cast(int, cost["steps"]) + 1
    prepared = tuple(
        prepare_graph_values(raw, value, max_encoded_bytes=LIMIT)
        for value in (contract, altered)
    )
    ledger = ConnectionLedger(database)
    assert ledger.outstanding == database.units_in_flight() == 0

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        foreign_namespace = await seeder.namespace("foreign")
        graph = await seeder.graph(namespace)
        for number, pair in enumerate(prepared, start=1):
            await connection.execute(
                insert(graph_version).values(
                    id=seeder.new_id(),
                    graph_id=graph,
                    number=number,
                    ir=pair.ir,
                    execution_contract=pair.execution_contract,
                    value_encoding_version=pair.value_encoding_version,
                    ir_digest=DIGEST,
                    source_digest=DIGEST,
                    execution_contract_digest=DIGEST,
                    deployment_digest=DIGEST,
                    resolved_digests={},
                    resources={},
                    deployed_by="owner",
                    deployed_at=seeder.clock.now(),
                    status="active",
                    **seeder.stamps(),
                )
            )
    async with database.read_transaction() as connection:
        row = await graph_version_by_number(connection, namespace, graph, 1)
        tampered = await graph_version_by_number(connection, namespace, graph, 2)
        foreign = await graph_version_by_number(connection, foreign_namespace, graph, 1)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert row is not None
    assert tampered is not None
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    bad = read_graph_values(
        tampered.value_encoding_version,
        tampered.ir,
        tampered.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    assert isinstance(decoded, DecodedGraphValues)
    assert isinstance(bad, DecodedGraphValues)
    actual = read_graph_cel_facts(
        cast(str, row.ir), decoded.execution_contract, limits=READ_LIMITS
    )
    with pytest.raises(GraphValueRejected) as refused:
        read_graph_cel_facts(
            cast(str, tampered.ir), bad.execution_contract, limits=READ_LIMITS
        )
    await database.close()

    assert actual.text == expected
    assert row.execution_contract == prepared[0].execution_contract
    assert foreign is None
    assert refused.value.reason is GraphValueFailure.INVALID
    assert refused.value.__context__ is None
    assert ledger.opened > 0
    assert ledger.opened == ledger.closed
    assert (
        ledger.outstanding
        == ledger.closed_while_checked_out
        == database.units_in_flight()
        == 0
    )


def test_saved_cel_reader_refuses_drift_and_comparison_capacity() -> None:
    raw, ir_text, contract, facts = expression_version()
    document = GraphDocument.model_validate(raw)
    sites = checked_graph_sites(
        document, READ_LIMITS.declarations, READ_LIMITS.triggers
    )
    assert isinstance(sites, tuple)
    comparison_bytes = 2 * len(facts)
    expected_alone = stage_graph_cel_facts(sites, comparison_bytes)
    source = render_object(contract, TextBudget(LIMIT))
    wrong_path = ObjectReader(source).object()
    cel_rows(wrong_path)[0]["path"] = [
        "graph",
        "nodes",
        "foreign",
        "config",
        "approve_when",
    ]
    wrong_reads = ObjectReader(source).object()
    cel_rows(wrong_reads)[1]["reads"] = {}
    reversed_fields = ObjectReader(source).object()
    cel_rows(reversed_fields).reverse()
    wrong_type = ObjectReader(source).object()
    cast(list[JsonObject], wrong_type["type_contracts"])[0]["name"] = "StructuredOutpux"
    changed = ObjectReader(ir_text).object()
    child(changed, "runtime", "infrastructure_retry")["predicate"] = "true"
    changed_ir = render_object(changed, TextBudget(LIMIT))
    cases = (
        (ir_text, wrong_path, READ_LIMITS, GraphValueFailure.INVALID),
        (ir_text, wrong_reads, READ_LIMITS, GraphValueFailure.INVALID),
        (ir_text, reversed_fields, READ_LIMITS, GraphValueFailure.INVALID),
        (changed_ir, contract, READ_LIMITS, GraphValueFailure.INVALID),
        (
            ir_text,
            {**contract, "parameters": {}},
            READ_LIMITS,
            GraphValueFailure.INVALID,
        ),
        (ir_text, {**contract, "outputs": []}, READ_LIMITS, GraphValueFailure.INVALID),
        (ir_text, wrong_type, READ_LIMITS, GraphValueFailure.INVALID),
        (
            ir_text,
            {**contract, "type_contracts": []},
            READ_LIMITS,
            GraphValueFailure.BUDGET,
        ),
        (
            ir_text,
            {**contract, "semantics": "other"},
            READ_LIMITS,
            GraphValueFailure.VERSION,
        ),
        (
            ir_text,
            {key: value for key, value in contract.items() if key != "cel_expressions"},
            READ_LIMITS,
            GraphValueFailure.INVALID,
        ),
        (
            ir_text,
            contract,
            replace(READ_LIMITS, comparison_bytes=comparison_bytes),
            GraphValueFailure.BUDGET,
        ),
        (
            ir_text,
            contract,
            replace(READ_LIMITS, typed_work=1),
            GraphValueFailure.BUDGET,
        ),
        (
            ir_text,
            contract,
            replace(READ_LIMITS, typed_bytes=1),
            GraphValueFailure.BUDGET,
        ),
    )

    failures: list[GraphValueFailure] = []
    for candidate_ir, value, limits, _ in cases:
        with pytest.raises(GraphValueRejected) as rejected:
            read_graph_cel_facts(candidate_ir, value, limits=limits)
        failures.append(rejected.value.reason)

    assert failures == [reason for _, _, _, reason in cases]
    assert expected_alone.text == facts


def test_saved_cel_reader_preserves_output_context_exhaustion() -> None:
    raw = output_preparation_capacity_document()
    ir_text, types = inventory_arguments(raw)
    _, _, contract, _ = expression_version()
    contract["type_contracts"] = types

    output_refusal = read_graph_output_inventory(
        ir_text, types, contract["outputs"], max_encoded_bytes=LIMIT
    )
    with pytest.raises(GraphValueRejected) as refused:
        read_graph_cel_facts(ir_text, contract, limits=READ_LIMITS)

    assert isinstance(output_refusal, StructuredSchemaPreparationRefusal)
    assert output_refusal.reason == "preparation_exhausted"
    assert refused.value.reason is GraphValueFailure.BUDGET


def test_saved_cel_reader_rejection_drops_ambient_exception() -> None:
    _, ir_text, contract, _ = expression_version()
    contract["semantics"] = "other"
    protected = RuntimeError("private ambient CEL data")

    try:
        raise protected
    except RuntimeError:
        with pytest.raises(GraphValueRejected) as refused:
            read_graph_cel_facts(ir_text, contract, limits=READ_LIMITS)

    assert refused.value.reason is GraphValueFailure.VERSION
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
