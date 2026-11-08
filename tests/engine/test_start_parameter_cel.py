from traceback import walk_tb

import pytest

from kora.cel_eval.bounds import MAX_BYTES
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import COMPONENT
from kora.engine.start_parameter_cel import (
    ParameterCelFailure,
    ParameterCelRejected,
    check_manual_start_parameter_cel,
)
from kora.engine.start_parameter_cut import (
    PreparedManualStartParameterCut,
    prepare_manual_start_parameter_cut,
)
from kora.ir.json import JsonObject, JsonValue
from kora.observability import Level, LoggingSettings, bound_scope
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock
from tests.engine.start_support import KEY, PRIVATE
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    complete_inputs,
    prepared_case,
    transport,
)
from tests.engine.test_graph_resource_references import SCOPE
from tests.engine.test_start_parameter_cut import (
    SCRATCH,
    WORK,
    CheckedCandidate,
    candidate_for,
)


def cut_for(case: CheckedCandidate) -> PreparedManualStartParameterCut:
    return prepare_manual_start_parameter_cut(
        case.request,
        KEY,
        case.candidate,
        case.contract,
        max_work=WORK,
        max_scratch_bytes=SCRATCH,
    )


def check(
    case: CheckedCandidate, cut: PreparedManualStartParameterCut
) -> PreparedManualStartParameterCut:
    return check_manual_start_parameter_cel(
        cut,
        case.contract,
        max_ir_bytes=len(case.contract.canonical_ir),
        max_schema_work=WORK,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_type", "predicate", "value", "encoded"),
    [
        ("integer", "params.enabled == 1", 1.0, b'{"enabled":1.0}'),
        ("integer", "params.enabled == 0", -0.0, b'{"enabled":-0.0}'),
        ("number", "params.enabled == 1.0", 1, b'{"enabled":1}'),
    ],
    ids=["integral-float", "signed-zero", "number-from-int"],
)
async def test_manual_parameter_cel_preserves_admitted_unconverted_values(
    database: Database,
    schema_type: str,
    predicate: str,
    value: JsonValue,
    encoded: bytes,
) -> None:
    ledger = ConnectionLedger(database)
    case = await candidate_for(
        database,
        {"enabled": value, "unused": PRIVATE * 1_000},
        parameter_reads=True,
        enabled_schema={"type": schema_type},
        enabled_predicate=predicate,
    )
    cut = cut_for(case)
    before = ledger.checkouts

    admitted = check(case, cut)

    assert admitted is cut
    assert admitted.projection_bytes() == encoded
    assert ledger.checkouts == before
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "predicate", "value"),
    [
        ({"type": "integer"}, "params.enabled == 1", 1.5),
        ({"type": "integer"}, "params.enabled == 1", PRIVATE),
        ({"type": "string", "maxLength": 2}, "params.enabled == 'A'", "abcdefghi"),
        (
            {"type": "array", "items": {"type": "number"}},
            "params.enabled.size() > 0",
            [9_007_199_254_740_993] * 230,
        ),
    ],
    ids=["nonintegral", "wrong-kind", "tightened-string", "expanded-conversion"],
)
async def test_manual_parameter_cel_refuses_values_after_an_admitted_cut(
    database: Database, schema: JsonObject, predicate: str, value: JsonValue
) -> None:
    case = await candidate_for(
        database,
        {"enabled": value},
        parameter_reads=True,
        enabled_schema=schema,
        enabled_predicate=predicate,
    )
    cut = cut_for(case)
    original = cut.projection_bytes()

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "manual-parameter-cel-test",
        FakeClock().now,
    ) as capture:
        with (
            bound_scope(request_id="manual-check"),
            pytest.raises(ParameterCelRejected, match="^values$") as refusal,
        ):
            check(case, cut)
        records = capture.finish()
        raw_output = capture.reader.data
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert len(original) <= MAX_BYTES
    assert cut.projection_bytes() == original
    assert refusal.value.reason is ParameterCelFailure.VALUES
    assert refusal.value.__context__ is None
    assert {"cut", "checked"}.isdisjoint(frames["check_manual_start_parameter_cel"])
    assert [
        (record["request_id"], record["outcome"])
        for record in records
        if record["event"] == "engine.start_parameter_cel.checked"
    ] == [("manual-check", "values")]
    for protected in (PRIVATE, KEY, case.contract.scope.version_id):
        assert protected.encode() not in raw_output
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_manual_parameter_cel_empty_reads_require_no_schema_mapping(
    database: Database,
) -> None:
    case = await candidate_for(
        database, {"enabled": PRIVATE * 1_000}, parameter_reads=False
    )
    cut = cut_for(case)

    admitted = check_manual_start_parameter_cel(
        cut, case.contract, max_ir_bytes=1, max_schema_work=1
    )

    assert admitted is cut
    assert admitted.projection_bytes() == b"{}"
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_manual_parameter_cel_binds_scope_and_digest_independently(
    database: Database,
) -> None:
    case = await candidate_for(database, {"enabled": True}, parameter_reads=True)
    cut = cut_for(case)
    same_scope = detached_contract(case.contract.scope, parameter_reads=False)
    same_digest = detached_contract(SCOPE, parameter_reads=True)

    for other in (same_scope, same_digest):
        with pytest.raises(ParameterCelRejected, match="^binding$"):
            check_manual_start_parameter_cel(
                cut, other, max_ir_bytes=1, max_schema_work=1
            )

    assert same_scope.scope == case.contract.scope
    assert same_scope.pin.execution_contract_digest != cut.execution_contract_digest
    assert same_digest.scope != case.contract.scope
    assert same_digest.pin.execution_contract_digest == cut.execution_contract_digest
    assert database.units_in_flight() == 0


def detached_contract(
    scope: GraphResourceScope, *, parameter_reads: bool
) -> CheckedExecutionContract:
    raw, saved, refs, owners = complete_inputs(
        scope, resources=False, parameter_reads=parameter_reads
    )
    prepared, pin = prepared_case(raw, saved, scope)
    return check_detached_execution_contract(
        transport(prepared, scope), pin, scope, refs, owners, limits=LIMITS
    )


@pytest.mark.asyncio
async def test_manual_parameter_cel_admits_work_before_conversion(
    database: Database,
) -> None:
    case = await candidate_for(database, {"enabled": True}, parameter_reads=True)
    cut = cut_for(case)
    size = len(case.contract.canonical_ir)

    with pytest.raises(ParameterCelRejected, match="^capacity$"):
        check_manual_start_parameter_cel(
            cut, case.contract, max_ir_bytes=size - 1, max_schema_work=WORK
        )
    with pytest.raises(ParameterCelRejected, match="^capacity$"):
        check_manual_start_parameter_cel(
            cut, case.contract, max_ir_bytes=size, max_schema_work=1
        )
    accepted = check(case, cut)

    assert accepted is cut
    assert database.units_in_flight() == 0
