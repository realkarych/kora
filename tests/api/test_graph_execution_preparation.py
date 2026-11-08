import asyncio
import hashlib
import json
import pickle
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder

from kora.api.cel_pattern_validation import COMPLETED as PATTERN_COMPLETED
from kora.api.cel_renderings import IncompleteRenderings, RenderingRefusal
from kora.api.exchange import COMPONENT
from kora.api.graph_cel import COMPLETED, GraphCelChecks
from kora.api.graph_execution_preparation import (
    ExecutionPreparationLimits,
    PreparedExecutionContract,
    PreparedLogicalExecutionContract,
    prepare_execution_contract,
    prepare_logical_execution_contract,
)
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.graph_version_reads import AcquiredGraphVersion
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    build_declared_command_manifests,
    check_declared_command_manifests,
)
from kora.engine.graph_execution_contract import (
    MEMBERS,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import build_graph_cel_projection_paths
from kora.engine.graph_resource_references import GraphResourceOwner
from kora.engine.graph_version_identity import GraphVersionPin
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from tests.api.cel_pattern_validation_scenarios import (
    REQUEST_ID,
    assert_idle,
    observe_deliveries,
    wait_for_delivery,
)
from tests.cel_eval.children import ROOT, full_path, run_child
from tests.cel_eval.pattern_validation_scenarios import (
    assert_settled,
    open_evaluator,
    run_loop_turn,
)
from tests.cel_eval.test_cel_evaluation import clock
from tests.engine.graph_output_cases import child
from tests.engine.test_graph_command_manifests import authored_manifest, resource
from tests.engine.test_graph_execution_contract import LIMITS as READ_LIMITS
from tests.engine.test_graph_execution_contract import complete_inputs
from tests.engine.test_graph_resource_references import SCOPE, assert_refused, content

MARKER = "private-preparation-837"
OWNER_MARKER = "second-object"
LIMITS = ExecutionPreparationLimits(
    READ_LIMITS, 100_000, 100_000, 100_000, 200_000_000, 400_000_000_000_000
)
TIGHT_PROJECTION = replace(READ_LIMITS.projection, section_bytes=650)
DIMENSIONS: tuple[tuple[Literal["max_retained_bytes", "max_work"], int], ...] = (
    ("max_retained_bytes", 156_349_497),
    ("max_work", 346_560_682_373_459),
)


def source(
    *, invalid_pattern: bool = False, ordered: bool = False
) -> tuple[str, tuple[GraphResourceOwner, ...]]:
    raw, _, _, owners = complete_inputs(SCOPE, long_integer=True)
    raw["x-signed-zero"] = -0.0
    graph = child(raw, "graph")
    root = owners[0]
    if ordered:
        child(graph, "nodes", "producer", "config").update(
            authored_manifest([resource("é.sh"), resource("z.sh")])
        )
        owners = (
            GraphResourceOwner(
                SCOPE,
                root.path,
                "é.sh",
                content(store="second-store", incarnation=OWNER_MARKER),
            ),
            GraphResourceOwner(SCOPE, root.path, "z.sh", content(incarnation=MARKER)),
        )
    else:
        owners = (
            GraphResourceOwner(
                SCOPE, root.path, root.name, content(incarnation=OWNER_MARKER)
            ),
        )
    child(graph, "params")["required"] = ["enabled"]
    pattern = ("(?ii)" if invalid_pattern else "^") + MARKER
    child(graph, "nodes", "approval", "config")["approve_when"] = (
        f'params.enabled && "a".matches("{pattern}")'
    )
    return render_object(
        raw, TextBudget(READ_LIMITS.identity.max_encoded_bytes)
    ), owners


@contextmanager
def borrowed() -> Generator[tuple[asyncio.Runner, Evaluator]]:
    owner = EvaluationThread(load_binding())
    with asyncio.Runner() as runner:
        evaluator = runner.run(open_evaluator(owner))
        try:
            yield runner, evaluator
        finally:
            runner.run(evaluator.close())
    assert_settled(owner)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def detached(draft: PreparedExecutionContract) -> AcquiredGraphVersion:
    hashes = draft.digests
    return AcquiredGraphVersion(
        draft.scope.namespace_id,
        draft.scope.graph_id,
        draft.scope.version_id,
        "ascii",
        (
            json.dumps(draft.canonical_ir).encode("ascii"),
            json.dumps(draft.canonical_contract).encode("ascii"),
        ),
        (
            hashes.ir_digest.encode("ascii"),
            hashes.execution_contract_digest.encode("ascii"),
            hashes.deployment_digest.encode("ascii"),
        ),
    )


def assert_protected(
    error: BaseException, boundary: str = "prepare_execution_contract"
) -> None:
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
    }
    assert set(frames[boundary]) == {"limits", "error"}
    assert (
        not {"_prepare", "_draft", "_prepare_logical", "_checked_pair"} & frames.keys()
    )
    assert error.__context__ is None
    assert error.__cause__ is None
    assert MARKER not in repr(error)
    assert OWNER_MARKER not in repr(error)


def check_roundtrip() -> None:
    ir_text, owners = source(ordered=True)
    original = owners[0]
    substitute = GraphResourceOwner(
        original.scope,
        original.path,
        original.name,
        content(
            store=original.reference.address.storage_identity.store_id,
            incarnation="other-instance",
        ),
    )

    with borrowed() as (runner, evaluator):
        loop = runner.get_loop()
        draft = observe_deliveries(
            loop,
            loop.create_task(
                prepare_execution_contract(
                    ir_text, SCOPE, owners, evaluator, limits=LIMITS
                )
            ),
            evaluator.owner,
            2,
        )
        assert_idle(evaluator.owner, 1)
    assert isinstance(draft, PreparedExecutionContract)
    acquired = detached(draft)
    pin = GraphVersionPin(
        SCOPE.graph_id, SCOPE.version_id, draft.digests.execution_contract_digest
    )
    physical = ObjectReader(draft.resources.text).object()
    checked = check_detached_execution_contract(
        acquired, pin, SCOPE, physical, owners, limits=READ_LIMITS
    )
    contract = ObjectReader(draft.canonical_contract).object()
    build_only = build_graph_cel_projection_paths(
        ir_text, contract, limits=TIGHT_PROJECTION
    )
    with pytest.raises(GraphValueRejected) as wrong_incarnation:
        check_detached_execution_contract(
            acquired, pin, SCOPE, physical, (substitute, owners[1]), limits=READ_LIMITS
        )
    with pytest.raises(AttributeError, match="immutable"):
        draft.canonical_ir = MARKER
    with pytest.raises(AttributeError, match="immutable"):
        delattr(draft, "canonical_ir")
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(draft)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(draft)

    assert draft.scope == checked.scope == SCOPE
    assert draft.canonical_ir == checked.canonical_ir == ir_text
    assert checked.canonical_contract == draft.canonical_contract
    assert checked.resources.text == draft.resources.text
    assert draft.owners == tuple(reversed(owners))
    assert digest(draft.resources.text) == (
        "2f921df41b098f5b25fdf21d2111dc72600b6099efdb22f3027c9d41d489a055"
    )
    assert draft.digests.ir_digest == (
        "6534672a556ec7f25abb64cd89236ff67ee710636144de4055f2f1e90fe1c44f"
    )
    assert draft.digests.execution_contract_digest == (
        "4a82670802109990959313ee8149e69f5eca4c2e5814fa6132dac91ed6a51801"
    )
    assert draft.digests.deployment_digest == (
        "55b8235631ae365a9d40e63aa2f45bb0a5ea839a261e7e1d4dc4cd0c8ea635b9"
    )
    assert '"x-signed-zero":-0.0' in draft.canonical_ir
    assert draft.digests.ir_digest == digest(ir_text)
    assert draft.digests.execution_contract_digest == digest(draft.canonical_contract)
    assert draft.digests.deployment_digest == digest(
        '{"ir":' + ir_text + ',"execution_contract":' + draft.canonical_contract + "}"
    )
    assert tuple(contract) == MEMBERS
    assert ObjectReader(build_only.text).object() == contract["cel_projection_paths"]
    assert child(contract, "cel_projection_paths")["parameters"] == {
        "whole": [".enabled"],
        "presence": [],
    }
    outputs = cast(list[JsonObject], contract["outputs"])
    assert len(outputs) == 3
    assert any(
        child(output, "contract")["declared_type"] == "StructuredOutput"
        for output in outputs
    )
    assert MARKER not in repr(draft)
    assert OWNER_MARKER not in repr(draft)
    assert MARKER not in repr(encoder.value)
    assert_refused(wrong_incarnation.value, GraphValueFailure.INVALID)


def check_logical_roundtrip() -> None:
    ir_text, owners = source(ordered=True)
    other_owners = tuple(
        GraphResourceOwner(
            owner.scope,
            owner.path,
            owner.name,
            content(store="alternative-store", incarnation="alternative-incarnation"),
        )
        for owner in owners
    )

    with borrowed() as (runner, evaluator):
        logical = runner.run(
            prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
        )
        original = runner.run(
            prepare_execution_contract(ir_text, SCOPE, owners, evaluator, limits=LIMITS)
        )
        alternative = runner.run(
            prepare_execution_contract(
                ir_text, SCOPE, other_owners, evaluator, limits=LIMITS
            )
        )
        assert_idle(evaluator.owner, 1)
    assert type(logical) is PreparedLogicalExecutionContract
    assert isinstance(original, PreparedExecutionContract)
    assert isinstance(alternative, PreparedExecutionContract)
    original_resources = original.resources
    original_digests = original.digests
    copied_digests = deepcopy(logical.digests)
    with pytest.raises(AttributeError, match="immutable"):
        logical.canonical_ir = MARKER
    with pytest.raises(AttributeError, match="immutable"):
        delattr(logical, "canonical_contract")
    with pytest.raises(AttributeError, match="immutable"):
        logical.__init__(MARKER, MARKER, original_digests)
    with pytest.raises(AttributeError, match="immutable"):
        PreparedLogicalExecutionContract.__init__(
            original, MARKER, MARKER, original_digests
        )
    with pytest.raises(AttributeError, match="immutable"):
        original.__init__(
            replace(SCOPE, graph_id=MARKER),
            MARKER,
            MARKER,
            original_digests,
            alternative.resources,
            alternative.owners,
        )
    with pytest.raises(AttributeError, match="immutable"):
        logical.digests.__init__(MARKER, MARKER, MARKER)
    with pytest.raises(AttributeError, match="immutable"):
        logical.digests.__setstate__([MARKER, MARKER, MARKER])
    with pytest.raises(TypeError, match="explicit access") as pickling:
        pickle.dumps(logical)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(logical)

    assert original.scope == SCOPE
    assert original.resources is original_resources
    assert original.owners == tuple(reversed(owners))
    assert original.resources.text != alternative.resources.text
    assert (
        logical.canonical_ir
        == original.canonical_ir
        == alternative.canonical_ir
        == ir_text
    )
    assert (
        logical.canonical_contract
        == original.canonical_contract
        == alternative.canonical_contract
    )
    assert logical.digests == original.digests == alternative.digests
    assert copied_digests == logical.digests
    assert logical.digests.ir_digest == digest(ir_text)
    assert logical.digests.execution_contract_digest == digest(
        logical.canonical_contract
    )
    assert logical.digests.deployment_digest == digest(
        '{"ir":' + ir_text + ',"execution_contract":' + logical.canonical_contract + "}"
    )
    assert not any(
        hasattr(logical, member) for member in ("scope", "resources", "owners")
    )
    for rendered in (repr(logical), repr(encoder.value), repr(pickling.value)):
        assert MARKER not in rendered
        assert logical.digests.deployment_digest not in rendered


def reject_before_native(
    runner: asyncio.Runner,
    evaluator: Evaluator,
    ir_text: str,
    owners: tuple[GraphResourceOwner, ...],
    limits: ExecutionPreparationLimits,
    expected: GraphValueFailure,
) -> None:
    loop = runner.get_loop()
    task = loop.create_task(
        prepare_execution_contract(ir_text, SCOPE, owners, evaluator, limits=limits)
    )

    run_loop_turn(loop)
    assert task.done()
    with pytest.raises(GraphValueRejected) as refusal:
        task.result()

    assert_refused(refusal.value, expected)
    assert_protected(refusal.value)
    assert_idle(evaluator.owner, 1)


def check_capacity() -> None:
    ir_text, owners = source()
    broken = '{"graph":{"x-private":"' + MARKER + '"}}'
    owner = owners[0]
    foreign = GraphResourceOwner(
        replace(SCOPE, namespace_id=MARKER), owner.path, owner.name, owner.reference
    )
    wide = GraphResourceOwner(SCOPE, owner.path, MARKER * 200_000, owner.reference)
    stages = (
        replace(READ_LIMITS, identity=replace(READ_LIMITS.identity, max_hash_bytes=1)),
        replace(
            READ_LIMITS, projection=replace(READ_LIMITS.projection, source_bytes=1)
        ),
        replace(
            READ_LIMITS, projection=replace(READ_LIMITS.projection, scratch_bytes=1)
        ),
        replace(
            READ_LIMITS,
            resources=replace(
                READ_LIMITS.resources,
                logical=replace(READ_LIMITS.resources.logical, ir_bytes=1),
            ),
        ),
    )
    result: PreparedExecutionContract | None = None

    with borrowed() as (runner, evaluator):
        for dimension, units in DIMENSIONS:
            exact = replace(LIMITS, **{dimension: units})
            short = replace(exact, **{dimension: units - 1})
            exact_result = runner.run(
                prepare_execution_contract(
                    ir_text, SCOPE, owners, evaluator, limits=exact
                )
            )
            assert isinstance(exact_result, PreparedExecutionContract)
            result = exact_result
            reject_before_native(
                runner, evaluator, ir_text, owners, short, GraphValueFailure.BUDGET
            )
            reject_before_native(
                runner, evaluator, broken, owners, short, GraphValueFailure.BUDGET
            )
            reject_before_native(
                runner, evaluator, broken, owners, exact, GraphValueFailure.INVALID
            )
        assert result is not None
        physical_short = replace(
            LIMITS,
            reader=replace(
                READ_LIMITS, physical_input_bytes=len(result.resources.text) - 1
            ),
        )
        reject_before_native(
            runner, evaluator, ir_text, owners, physical_short, GraphValueFailure.BUDGET
        )
        reject_before_native(
            runner, evaluator, ir_text, (foreign,), LIMITS, GraphValueFailure.INVALID
        )
        reject_before_native(
            runner, evaluator, ir_text, (wide,), LIMITS, GraphValueFailure.BUDGET
        )
        for stage in stages:
            insufficient = replace(LIMITS, reader=stage)
            reject_before_native(
                runner,
                evaluator,
                ir_text,
                owners,
                insufficient,
                GraphValueFailure.BUDGET,
            )
            reject_before_native(
                runner,
                evaluator,
                broken,
                owners,
                insufficient,
                GraphValueFailure.BUDGET,
            )

    assert result.canonical_ir == ir_text


async def closed_under_context(
    ir_text: str, owners: tuple[GraphResourceOwner, ...] | None, evaluator: Evaluator
) -> EvaluatorUnavailable:
    preparation = (
        prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
        if owners is None
        else prepare_execution_contract(
            ir_text, SCOPE, owners, evaluator, limits=LIMITS
        )
    )
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        with pytest.raises(EvaluatorUnavailable) as unavailable:
            await preparation
    return unavailable.value


def check_refusals() -> None:
    valid_ir, owners = source()
    invalid_ir, _ = source(invalid_pattern=True)
    limited = replace(LIMITS, rendering_characters=1)
    tiny_reader = replace(READ_LIMITS, projection=TIGHT_PROJECTION)
    reader_limited = replace(LIMITS, reader=tiny_reader)

    with borrowed() as (runner, evaluator):
        native = runner.run(
            prepare_execution_contract(
                invalid_ir, SCOPE, owners, evaluator, limits=LIMITS
            )
        )
        incomplete = runner.run(
            prepare_execution_contract(
                valid_ir, SCOPE, owners, evaluator, limits=limited
            )
        )
        with pytest.raises(GraphValueRejected) as assembled:
            runner.run(
                prepare_execution_contract(
                    valid_ir, SCOPE, owners, evaluator, limits=reader_limited
                )
            )
        runner.run(evaluator.close())
        unavailable = runner.run(closed_under_context(valid_ir, owners, evaluator))

    assert isinstance(native, GraphCelChecks)
    assert native.facts is None
    assert [diagnostic.code for diagnostic in native.diagnostics.errors] == ["IR011"]
    assert incomplete == IncompleteRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
    assert unavailable.reason is UnavailableReason.CLOSED
    assert_protected(unavailable)
    assert_refused(assembled.value, GraphValueFailure.BUDGET)
    assert_protected(assembled.value)


def logical_before_comparison(ir_text: str) -> CommandManifestLimits:
    limits = READ_LIMITS.resources.logical
    low, high = 1, limits.work
    while low < high:
        middle = (low + high) // 2
        try:
            build_declared_command_manifests(
                ir_text, limits=replace(limits, work=middle)
            )
        except GraphValueRejected as refusal:
            assert_refused(refusal, GraphValueFailure.BUDGET)
            low = middle + 1
        else:
            high = middle
    return replace(limits, work=high)


def check_logical_refusals() -> None:
    ir_text, _ = source()
    invalid_ir, _ = source(invalid_pattern=True)
    boundary = "prepare_logical_execution_contract"
    tight_manifest = logical_before_comparison(ir_text)
    manifest = build_declared_command_manifests(ir_text, limits=tight_manifest)
    with pytest.raises(GraphValueRejected) as manifest_comparison:
        check_declared_command_manifests(
            ir_text, ObjectReader(manifest.text).object(), limits=tight_manifest
        )
    assert_refused(manifest_comparison.value, GraphValueFailure.BUDGET)
    readers = (
        replace(READ_LIMITS, projection=TIGHT_PROJECTION),
        replace(
            READ_LIMITS,
            resources=replace(READ_LIMITS.resources, logical=tight_manifest),
        ),
    )

    with borrowed() as (runner, evaluator):
        loop = runner.get_loop()
        for dimension, units in DIMENSIONS:
            exact = replace(LIMITS, **{dimension: units})
            short = replace(exact, **{dimension: units - 1})
            result = runner.run(
                prepare_logical_execution_contract(ir_text, evaluator, limits=exact)
            )
            task = loop.create_task(
                prepare_logical_execution_contract(ir_text, evaluator, limits=short)
            )
            run_loop_turn(loop)
            assert task.done()
            with pytest.raises(GraphValueRejected) as early:
                task.result()
            assert type(result) is PreparedLogicalExecutionContract
            assert_refused(early.value, GraphValueFailure.BUDGET)
            assert_protected(early.value, boundary)
            assert_idle(evaluator.owner, 1)
        invalid = runner.run(
            prepare_logical_execution_contract(invalid_ir, evaluator, limits=LIMITS)
        )
        incomplete = runner.run(
            prepare_logical_execution_contract(
                ir_text, evaluator, limits=replace(LIMITS, rendering_characters=1)
            )
        )
        for reader in readers:
            with pytest.raises(GraphValueRejected) as late:
                runner.run(
                    prepare_logical_execution_contract(
                        ir_text, evaluator, limits=replace(LIMITS, reader=reader)
                    )
                )
            assert_refused(late.value, GraphValueFailure.BUDGET)
            assert_protected(late.value, boundary)
            assert_idle(evaluator.owner, 1)
        runner.run(evaluator.close())
        unavailable = runner.run(closed_under_context(ir_text, None, evaluator))

    assert isinstance(invalid, GraphCelChecks)
    assert invalid.facts is None
    assert [error.code for error in invalid.diagnostics.errors] == ["IR011"]
    assert incomplete == IncompleteRenderings(RenderingRefusal.CAPACITY_EXHAUSTED)
    assert unavailable.reason is UnavailableReason.CLOSED
    assert_protected(unavailable, boundary)


def check_cancellation(*, logical: bool = False) -> None:
    ir_text, owners = source()
    boundary = (
        "prepare_logical_execution_contract"
        if logical
        else "prepare_execution_contract"
    )

    with borrowed() as (runner, evaluator):
        loop = runner.get_loop()
        owner = evaluator.owner
        for prior_deliveries in (0, 1):
            task = loop.create_task(
                prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
                if logical
                else prepare_execution_contract(
                    ir_text, SCOPE, owners, evaluator, limits=LIMITS
                ),
            )
            for _ in range(prior_deliveries):
                run_loop_turn(loop)
                wait_for_delivery(owner)
                run_loop_turn(loop)
            run_loop_turn(loop)
            active = wait_for_delivery(owner)
            assert task.cancel(MARKER)
            with pytest.raises(asyncio.CancelledError) as cancelled:
                loop.run_until_complete(task)
            assert active.outstanding == active.deliveries == 1
            assert cancelled.value.args == ()
            assert_protected(cancelled.value, boundary)
            assert_idle(owner, 1)
        reused = runner.run(
            prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
            if logical
            else prepare_execution_contract(
                ir_text, SCOPE, owners, evaluator, limits=LIMITS
            )
        )

    assert isinstance(reused, PreparedLogicalExecutionContract)
    assert reused.canonical_ir == ir_text
    if logical:
        assert type(reused) is PreparedLogicalExecutionContract
    else:
        assert isinstance(reused, PreparedExecutionContract)
        assert reused.owners == owners


def run(scenario: str) -> None:
    cases = {
        "roundtrip": check_roundtrip,
        "capacity": check_capacity,
        "refusals": check_refusals,
        "cancellation": check_cancellation,
        "logical_roundtrip": check_logical_roundtrip,
        "logical_refusals": check_logical_refusals,
        "logical_cancellation": lambda: check_cancellation(logical=True),
    }
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG, render_format=RenderFormat.JSON),
        "contract-prepare-test",
        clock,
    )
    try:
        with bound_scope(request_id=REQUEST_ID):
            cases[scenario]()
    finally:
        closed = diagnostics.shutdown(5)
    assert closed


@pytest.mark.parametrize(
    ("scenario", "native_completions", "patterns"),
    [
        ("roundtrip", ["complete"], 1),
        ("capacity", ["complete"] * 2, 2),
        ("refusals", ["complete", "capacity_exhausted", "complete"], 2),
        ("cancellation", ["complete"], 1),
        ("logical_roundtrip", ["complete"] * 3, 3),
        (
            "logical_refusals",
            ["complete"] * 3 + ["capacity_exhausted"] + ["complete"] * 2,
            5,
        ),
        ("logical_cancellation", ["complete"], 1),
    ],
)
def test_graph_execution_preparation_uses_real_bounded_native_verification(
    scenario: str,
    native_completions: list[str],
    patterns: int,
) -> None:
    code = (
        f"from tests.api.test_graph_execution_preparation import run\nrun({scenario!r})"
    )

    result = run_child(code, full_path(ROOT))

    assert result.code == 0, result.stderr
    assert result.stdout == ""
    assert MARKER not in result.stderr
    assert OWNER_MARKER not in result.stderr
    assert "Function 'matches' error:" not in result.stderr
    records: list[dict[str, object]] = [
        json.loads(line) for line in result.stderr.splitlines()
    ]
    completions = [record for record in records if record["event"] == COMPLETED.name]
    pattern_completions = [
        record for record in records if record["event"] == PATTERN_COMPLETED.name
    ]
    assert [record["status"] for record in completions] == native_completions
    assert [record["status"] for record in pattern_completions] == [
        "complete"
    ] * patterns
    assert all(
        record["request_id"] == REQUEST_ID and record["level"] == "DEBUG"
        for record in (*completions, *pattern_completions)
    )
