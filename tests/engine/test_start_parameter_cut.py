import pickle
from dataclasses import dataclass
from traceback import walk_tb

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.reads import graph_version_by_number
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.run_input_values import encode_run_input_value
from kora.engine.start_parameter_cut import (
    ParameterCutFailure,
    ParameterCutRejected,
    PreparedManualStartParameterCut,
    prepare_manual_start_parameter_cut,
)
from kora.engine.start_records import (
    PreparedManualStartRequest,
    StartRecordRejected,
    prepare_manual_start_request,
)
from kora.engine.start_replay import (
    ManualStartCandidate,
    ReplayOutcome,
    StartReplay,
    read_manual_start_candidate,
)
from kora.ir.graph_projection_paths import (
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionRejected,
    cut_parameter_value,
    prepare_parameter_cut_paths,
)
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.start_support import KEY, PRIVATE, replay_scenario
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    PARAMETER_MARKER,
    complete_inputs,
    insert_case,
    prepared_case,
    transport,
)

WORK = 100_000
SCRATCH = 1_000_000
SECOND_KEY = "another-receipt-key"


@dataclass(frozen=True)
class CheckedCandidate:
    request: PreparedManualStartRequest
    candidate: ManualStartCandidate
    contract: CheckedExecutionContract
    principal: Principal


async def candidate_for(
    database: Database, params: JsonObject, *, parameter_reads: bool
) -> CheckedCandidate:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seeder.new_id())
        owner = seeder.new_id()
        principal = Principal(
            token_id=await seeder.token("a" * 64, owner=owner),
            kind="user",
            owner=owner,
            namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=False),),
            approve=False,
            platform=False,
            runtime=None,
        )
    raw, envelope, refs, owners = complete_inputs(
        scope, resources=False, parameter_reads=parameter_reads
    )
    prepared, pin = prepared_case(raw, envelope, scope)
    async with database.transaction() as connection:
        await insert_case(Seeder(connection), scope, 1, prepared, refs)
    request = prepare_manual_start_request(namespace_id, graph_id, params)
    async with database.read_snapshot() as connection:
        candidate = await read_manual_start_candidate(
            connection, principal, request, KEY
        )
        acquired = await acquire_graph_version(
            connection,
            namespace_id,
            graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
        row = await graph_version_by_number(connection, namespace_id, graph_id, 1)
    assert acquired is not None
    assert row is not None
    assert database.units_in_flight() == 0
    checked = check_detached_execution_contract(
        acquired, pin, scope, row.resources, owners, limits=LIMITS
    )
    return CheckedCandidate(request, candidate, checked, principal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parameter_reads", "include_presence", "enabled", "expected"),
    [
        (True, True, True, b'{"enabled":true,"private_parameter_canary":""}'),
        (True, False, True, b'{"enabled":true}'),
        (True, False, [-0.0, 0, 1.0, "\0"], b'{"enabled":[-0.0,0,1.0,"\\u0000"]}'),
        (False, True, True, b"{}"),
    ],
)
async def test_manual_cut_uses_detached_original_parameters_and_checked_paths(
    database: Database,
    parameter_reads: bool,
    include_presence: bool,
    enabled: JsonValue,
    expected: bytes,
) -> None:
    ledger = ConnectionLedger(database)
    raw: JsonObject = {"enabled": enabled, "dormant": True, "unused": PRIVATE * 1_000}
    if include_presence:
        raw[PARAMETER_MARKER] = PRIVATE * 1_000
    case = await candidate_for(database, raw, parameter_reads=parameter_reads)
    scope, pin = case.contract.scope, case.contract.pin
    checkouts = ledger.checkouts

    result = prepare_manual_start_parameter_cut(
        case.request,
        KEY,
        case.candidate,
        case.contract,
        max_work=WORK,
        max_scratch_bytes=SCRATCH,
    )
    with pytest.raises(AttributeError, match="immutable"):
        result.version_id = PRIVATE
    with pytest.raises(AttributeError, match="immutable"):
        delattr(result, "_projection")
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(result)
    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(result)

    assert result.projection_bytes() == expected
    assert result.request_digest == case.request.identity.request_digest
    assert (result.namespace_id, result.graph_id, result.version_id) == (
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
    )
    assert result.execution_contract_digest == pin.execution_contract_digest
    assert len(case.request.parameter_bytes()) > 4096
    assert PRIVATE not in repr(result)
    assert ledger.checkouts == checkouts
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_manual_cut_refuses_request_key_selector_and_scope_substitution(
    database: Database,
) -> None:
    case = await candidate_for(database, {"enabled": True}, parameter_reads=True)
    scope = case.contract.scope
    changed = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {"enabled": False}
    )
    exact = prepare_manual_start_request(
        scope.namespace_id,
        scope.graph_id,
        {"enabled": True},
        graph_version_id=scope.version_id,
    )
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        missing_id = seeder.new_id()
        another_graph = await seeder.graph(scope.namespace_id, "another-graph")
        await seeder.graph_version(another_graph)
        newer_id = await seeder.graph_version(scope.graph_id, number=2)
    missing = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {}, graph_version_id=missing_id
    )
    wrong_graph = prepare_manual_start_request(scope.namespace_id, another_graph, {})
    async with database.read_snapshot() as connection:
        other_key = await read_manual_start_candidate(
            connection, case.principal, case.request, SECOND_KEY
        )
        no_version = await read_manual_start_candidate(
            connection, case.principal, missing, KEY
        )
        newer = await read_manual_start_candidate(
            connection, case.principal, case.request, KEY
        )
        other_graph = await read_manual_start_candidate(
            connection, case.principal, wrong_graph, KEY
        )
        exact_candidate = await read_manual_start_candidate(
            connection, case.principal, exact, KEY
        )
    substitutions = (
        (changed, KEY, case.candidate),
        (exact, KEY, case.candidate),
        (case.request, KEY, exact_candidate),
        (case.request, KEY, other_key),
        (case.request, PRIVATE, case.candidate),
        (case.request, "\0", case.candidate),
        (missing, KEY, no_version),
        (case.request, KEY, newer),
        (wrong_graph, KEY, other_graph),
    )

    rejected: list[ParameterCutRejected] = []
    for request, key, candidate in substitutions:
        with pytest.raises(ParameterCutRejected) as refusal:
            prepare_manual_start_parameter_cut(
                request, key, candidate, case.contract, max_work=1, max_scratch_bytes=1
            )
        rejected.append(refusal.value)
    equivalent = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {"enabled": True}
    )
    result = prepare_manual_start_parameter_cut(
        equivalent,
        KEY,
        case.candidate,
        case.contract,
        max_work=WORK,
        max_scratch_bytes=SCRATCH,
    )
    exact_result = prepare_manual_start_parameter_cut(
        exact,
        KEY,
        exact_candidate,
        case.contract,
        max_work=WORK,
        max_scratch_bytes=SCRATCH,
    )

    assert {refusal.reason for refusal in rejected} == {ParameterCutFailure.BINDING}
    assert no_version.version_id is None
    assert newer.version_id == newer_id
    assert other_graph.version_id is not None
    assert (
        result.projection_bytes()
        == exact_result.projection_bytes()
        == b'{"enabled":true}'
    )
    assert result.request_digest != exact_result.request_digest


@pytest.mark.asyncio
async def test_cut_refuses_retained_receipt_and_unissued_handles(
    database: Database,
) -> None:
    replay = await replay_scenario(database, skipped=True)
    scope = GraphResourceScope(
        replay.identity.namespace_id, replay.identity.graph_id, replay.version_id
    )
    request = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {"private": PRIVATE}
    )
    raw, envelope, refs, owners = complete_inputs(scope, resources=False)
    prepared, pin = prepared_case(raw, envelope, scope)
    checked = check_detached_execution_contract(
        transport(prepared, scope), pin, scope, refs, owners, limits=LIMITS
    )
    async with database.read_snapshot() as connection:
        retained = await read_manual_start_candidate(
            connection, replay.principal, request, KEY
        )
        with pytest.raises(StartRecordRejected):
            await read_manual_start_candidate(
                connection,
                replay.principal,
                object.__new__(UnissuedManualStartRequest),
                KEY,
            )

    with pytest.raises(ParameterCutRejected) as refusal:
        prepare_manual_start_parameter_cut(
            request, KEY, retained, checked, max_work=1, max_scratch_bytes=1
        )
    with pytest.raises(TypeError, match="requires the complete checking reader"):
        CheckedExecutionContract(
            scope,
            pin,
            checked.canonical_ir,
            checked.canonical_contract,
            checked.resources,
            checked.parameter_paths,
        )
    with pytest.raises(TypeError, match="requires a checked replay read"):
        ManualStartCandidate(
            object(), StartReplay(ReplayOutcome.ABSENT), scope.version_id
        )
    with pytest.raises(TypeError, match="requires checked preparation"):
        PreparedManualStartParameterCut(
            object(),
            scope,
            pin.execution_contract_digest,
            request.identity.request_digest,
            b"{}",
        )
    assert refusal.value.reason is ParameterCutFailure.BINDING
    assert retained.replay.outcome is ReplayOutcome.MATCHED
    assert not retained.is_new_for(request.identity.request_digest, KEY)
    assert retained.version_id is None


class UnissuedManualStartRequest(PreparedManualStartRequest):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_work", "scratch", "expected"),
    [
        (1, SCRATCH, ParameterCutFailure.CAPACITY),
        (WORK, 1, ParameterCutFailure.CAPACITY),
        (WORK, SCRATCH, ParameterCutFailure.VALUES),
    ],
)
async def test_manual_cut_admits_paths_first_and_hides_failure_frames(
    database: Database, max_work: int, scratch: int, expected: ParameterCutFailure
) -> None:
    case = await candidate_for(
        database, {"enabled": PRIVATE * 1_000}, parameter_reads=True
    )

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterCutRejected) as refusal:
            prepare_manual_start_parameter_cut(
                case.request,
                KEY,
                case.candidate,
                case.contract,
                max_work=max_work,
                max_scratch_bytes=scratch,
            )

    assert refusal.value.reason is expected
    assert str(refusal.value) == expected.value
    assert refusal.value.__context__ is None
    assert refusal.value.__cause__ is None
    frames = {
        frame.f_code.co_name: set(frame.f_locals)
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert frames["prepare_manual_start_parameter_cut"] == {"result"}
    assert "_prepare_cut" not in frames
    assert "cut_parameter_value" not in frames


@pytest.mark.parametrize(
    ("accepted", "one_over"),
    [
        ("x" * (4096 - len(b'{"picked":""}')), "x" * (4097 - len(b'{"picked":""}'))),
        ([0] * 254, [0] * 255),
    ],
)
def test_parameter_cutter_accepts_exact_cel_data_limits_and_refuses_one_over(
    accepted: str | list[JsonValue], one_over: str | list[JsonValue]
) -> None:
    first = ProjectionAdmission(WORK, SCRATCH)
    plan = prepare_parameter_cut_paths((".picked",), (), first)
    second = ProjectionAdmission(WORK, SCRATCH)
    other_plan = prepare_parameter_cut_paths((".picked",), (), second)

    cut = cut_parameter_value({"picked": accepted}, plan, first)
    encoded = encode_run_input_value(cut, max_bytes=4096)
    with pytest.raises(ProjectionRejected) as refusal:
        cut_parameter_value({"picked": one_over}, other_plan, second)

    assert (len(encoded) if isinstance(accepted, str) else len(accepted) + 2) == (
        4096 if isinstance(accepted, str) else 256
    )
    assert refusal.value.reason is ProjectionFailure.DATA
