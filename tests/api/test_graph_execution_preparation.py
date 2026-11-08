import asyncio
import gc
import hashlib
import json
import pickle
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from inspect import getcoroutinelocals, iscoroutine
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
    GraphDeploymentCelRefusal,
    GraphDeploymentPreparationLimits,
    PreparedExecutionContract,
    PreparedGraphDeploymentInputs,
    PreparedLogicalExecutionContract,
    prepare_execution_contract,
    prepare_graph_deployment_inputs,
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
    CommandResourceContent,
    build_declared_command_manifests,
    check_declared_command_manifests,
)
from kora.engine.graph_execution_contract import (
    MEMBERS,
    check_detached_execution_contract,
)
from kora.engine.graph_projection_paths import (
    ParameterProjectionPaths,
    build_graph_cel_projection_paths,
)
from kora.engine.graph_resource_references import GraphResourceOwner
from kora.engine.graph_version_identity import GraphVersionPin
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings, bound_scope, configure_process
from kora.observability.settings import RenderFormat
from kora.protocols.storage_models import StorageIdentity
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
from tests.engine.test_graph_command_manifests import LIMITS as GROUP_LIMITS
from tests.engine.test_graph_command_manifests import ZERO, authored_manifest, resource
from tests.engine.test_graph_execution_contract import LIMITS as READ_LIMITS
from tests.engine.test_graph_execution_contract import complete_inputs
from tests.engine.test_graph_resource_references import SCOPE, assert_refused, content

MARKER = "private-preparation-837"
OWNER_MARKER = "second-object"
PRIVATE_APPROVAL = "private_approval_641"
PRIVATE_STORE = "Private-Deployment-Store-863"
DEPLOYMENT_ENTRY = "prepare_graph_deployment_inputs"
DEPLOYMENT_STORE = StorageIdentity(version=1, store_id=PRIVATE_STORE)
COMMAND_PATH = ("graph", "nodes", "producer")
LIMITS = ExecutionPreparationLimits(
    READ_LIMITS, 100_000, 100_000, 100_000, 200_000_000, 400_000_000_000_000
)
DEPLOYMENT_LIMITS = GraphDeploymentPreparationLimits(
    LIMITS,
    GROUP_LIMITS,
    LIMITS.max_retained_bytes + GROUP_LIMITS.ir_bytes + GROUP_LIMITS.scratch + 32,
    LIMITS.max_work + GROUP_LIMITS.work + 32,
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
    allowed = {"error"} if boundary == DEPLOYMENT_ENTRY else {"limits", "error"}
    assert set(frames[boundary]) == allowed
    assert (
        not {"_prepare", "_draft", "_prepare_logical", "_checked_pair"} & frames.keys()
    )
    assert error.__context__ is None
    assert error.__cause__ is None
    assert MARKER not in repr(error)
    assert OWNER_MARKER not in repr(error)
    assert PRIVATE_STORE not in repr(error)
    assert PRIVATE_APPROVAL not in repr(error)


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


async def closed_under_context(preparation: Awaitable[object]) -> EvaluatorUnavailable:
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
        unavailable = runner.run(
            closed_under_context(
                prepare_execution_contract(
                    valid_ir, SCOPE, owners, evaluator, limits=LIMITS
                )
            )
        )

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


def check_released_paths_on_manifest_refusal(*, logical: bool = False) -> None:
    ir_text, owners = source()
    manifest = logical_before_comparison(ir_text)
    if logical:
        manifest = replace(manifest, work=manifest.work - 1)
    reader = replace(
        READ_LIMITS, resources=replace(READ_LIMITS.resources, logical=manifest)
    )
    limits = replace(LIMITS, reader=reader)
    existing = tuple(
        item for item in gc.get_objects() if type(item) is ParameterProjectionPaths
    )
    collecting = gc.isenabled()

    with borrowed() as (runner, evaluator):
        gc.disable()
        try:
            if logical:
                operation = prepare_logical_execution_contract(
                    ir_text, evaluator, limits=limits
                )
            else:
                operation = prepare_execution_contract(
                    ir_text, SCOPE, owners, evaluator, limits=limits
                )
            with pytest.raises(GraphValueRejected) as refused:
                runner.run(operation)
            retained = any(
                type(item) is ParameterProjectionPaths
                and all(item is not prior for prior in existing)
                for item in gc.get_objects()
            )
        finally:
            if collecting:
                gc.enable()

    assert_refused(refused.value, GraphValueFailure.BUDGET)
    assert not retained


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
        unavailable = runner.run(
            closed_under_context(
                prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
            )
        )

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


def deployment_contents() -> tuple[CommandResourceContent, ...]:
    payload = MARKER.encode("ascii")
    return (
        CommandResourceContent(COMMAND_PATH, "é.sh", b""),
        CommandResourceContent(COMMAND_PATH, "z.sh", bytes(bytearray(payload))),
        CommandResourceContent(COMMAND_PATH, "a.sh", payload),
    )


def deployment_source(
    contents: tuple[CommandResourceContent, ...], *, invalid_pattern: bool = False
) -> str:
    ir_text, _ = source(invalid_pattern=invalid_pattern)
    raw = ObjectReader(ir_text).object()
    nodes = child(raw, "graph", "nodes")
    nodes[PRIVATE_APPROVAL] = nodes.pop("approval")
    child(nodes, "producer", "config").update(
        authored_manifest(
            [
                resource(
                    item.name,
                    len(item.data),
                    "sha256:" + hashlib.sha256(item.data).hexdigest(),
                )
                for item in contents
            ]
        )
    )
    return render_object(raw, TextBudget(READ_LIMITS.identity.max_encoded_bytes))


def assert_private_deployment(result: object, member: str) -> None:
    original: object = getattr(result, member)
    reinitialize = cast(Callable[[object, object], None], result.__init__)

    with pytest.raises(AttributeError, match="immutable"):
        setattr(result, member, None)
    with pytest.raises(AttributeError, match="immutable"):
        delattr(result, member)
    reinitialize(None, None)
    with pytest.raises(TypeError, match="explicit access") as pickling:
        pickle.dumps(result)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(result)

    assert getattr(result, member) is original
    for rendered in (repr(result), repr(pickling.value), repr(encoder.value)):
        assert all(
            marker not in rendered
            for marker in (MARKER, PRIVATE_STORE, PRIVATE_APPROVAL)
        )


def check_deployment_roundtrip() -> None:
    files = deployment_contents()
    empty_file = (files[0],)
    canonical_first = files[-1]
    ir_text = deployment_source(files)
    no_files = deployment_source(())
    zero_file = deployment_source(empty_file)

    with borrowed() as (runner, evaluator):
        expected = runner.run(
            prepare_logical_execution_contract(ir_text, evaluator, limits=LIMITS)
        )
        variants = [
            runner.run(
                prepare_graph_deployment_inputs(
                    text,
                    contents,
                    evaluator,
                    storage_identity=storage,
                    limits=DEPLOYMENT_LIMITS,
                )
            )
            for text, contents, storage in (
                (ir_text, files, DEPLOYMENT_STORE),
                (no_files, (), None),
                (zero_file, empty_file, DEPLOYMENT_STORE),
            )
        ]
        assert_idle(evaluator.owner, 1)

    complete, absent, zero = variants
    assert isinstance(expected, PreparedLogicalExecutionContract)
    assert isinstance(complete, PreparedGraphDeploymentInputs)
    assert isinstance(absent, PreparedGraphDeploymentInputs)
    assert isinstance(zero, PreparedGraphDeploymentInputs)
    assert complete.logical.canonical_ir == expected.canonical_ir == ir_text
    assert complete.logical.canonical_contract == expected.canonical_contract
    assert complete.logical.digests == expected.digests
    assert absent.groups == ()
    assert len(zero.groups) == 1
    assert zero.groups[0].ordinals == (0,)
    assert zero.groups[0].total_size == 0
    assert zero.groups[0].payload_digest == ZERO
    assert len(complete.groups) == 2
    assert all(group.store_id == PRIVATE_STORE for group in complete.groups)
    assert tuple(group.payload_digest for group in complete.groups) == tuple(
        sorted(group.payload_digest for group in complete.groups)
    )
    indexed = {group.ordinals: group for group in complete.groups}
    assert indexed[(0, 1)].data is canonical_first.data
    assert indexed[(2,)].payload_digest == ZERO
    assert_private_deployment(complete, "groups")


def check_deployment_refusals() -> None:
    files = deployment_contents()
    ir_text = deployment_source(files)
    invalid_ir = deployment_source(files, invalid_pattern=True)
    zero = deployment_source((files[0],))
    first = files[-1]
    bad_bytes = CommandResourceContent(first.path, first.name, b"?" * len(first.data))
    bad_name = CommandResourceContent(first.path, "different.sh", first.data)
    oversized = CommandResourceContent(
        first.path, first.name, b"?" * (COMMAND_RESOURCE_MAX_BYTES + 1)
    )
    short_aggregate = (
        replace(
            DEPLOYMENT_LIMITS,
            max_retained_bytes=DEPLOYMENT_LIMITS.max_retained_bytes - 1,
        ),
        replace(DEPLOYMENT_LIMITS, max_work=DEPLOYMENT_LIMITS.max_work - 1),
    )
    invalid_limits = (
        *short_aggregate,
        replace(DEPLOYMENT_LIMITS, max_work=True),
        replace(DEPLOYMENT_LIMITS, max_work=cast(int, cast(object, PRIVATE_STORE))),
        replace(DEPLOYMENT_LIMITS, contents=replace(GROUP_LIMITS, work=1)),
        replace(DEPLOYMENT_LIMITS, contents=replace(GROUP_LIMITS, scratch=1)),
    )

    with borrowed() as (runner, evaluator):
        loop = runner.get_loop()
        cases = (
            [
                (ir_text, files, DEPLOYMENT_STORE, limits, GraphValueFailure.BUDGET)
                for limits in invalid_limits
            ]
            + [
                (
                    ir_text,
                    (*files[:-1], bad_bytes),
                    DEPLOYMENT_STORE,
                    limits,
                    GraphValueFailure.BUDGET,
                )
                for limits in short_aggregate
            ]
            + [
                (
                    ir_text,
                    mismatched,
                    DEPLOYMENT_STORE,
                    DEPLOYMENT_LIMITS,
                    GraphValueFailure.INVALID,
                )
                for mismatched in (
                    files[:-1],
                    (*files[:-1], bad_bytes),
                    (*files[:-1], bad_name),
                    (*files[:-1], oversized),
                )
            ]
            + [(zero, (files[0],), None, DEPLOYMENT_LIMITS, GraphValueFailure.INVALID)]
        )
        for text, contents, storage, limits, reason in cases:
            task = loop.create_task(
                prepare_graph_deployment_inputs(
                    text, contents, evaluator, storage_identity=storage, limits=limits
                )
            )
            run_loop_turn(loop)
            assert task.done()
            with pytest.raises(GraphValueRejected) as refused:
                task.result()
            assert_refused(refused.value, reason)
            assert_protected(refused.value, DEPLOYMENT_ENTRY)
            assert_idle(evaluator.owner, 1)
        invalid = runner.run(
            prepare_graph_deployment_inputs(
                invalid_ir,
                files,
                evaluator,
                storage_identity=DEPLOYMENT_STORE,
                limits=DEPLOYMENT_LIMITS,
            )
        )
        incomplete = runner.run(
            prepare_graph_deployment_inputs(
                ir_text,
                files,
                evaluator,
                storage_identity=DEPLOYMENT_STORE,
                limits=replace(
                    DEPLOYMENT_LIMITS, execution=replace(LIMITS, rendering_characters=1)
                ),
            )
        )
        runner.run(evaluator.close())
        unavailable = runner.run(
            closed_under_context(
                prepare_graph_deployment_inputs(
                    ir_text,
                    files,
                    evaluator,
                    storage_identity=DEPLOYMENT_STORE,
                    limits=DEPLOYMENT_LIMITS,
                )
            )
        )

    assert isinstance(invalid, GraphDeploymentCelRefusal)
    assert isinstance(invalid.result, GraphCelChecks)
    [diagnostic] = invalid.result.diagnostics.errors
    assert diagnostic.code == "IR011"
    assert diagnostic.path == (
        "graph",
        "nodes",
        PRIVATE_APPROVAL,
        "config",
        "approve_when",
    )
    assert invalid.result.facts is None
    assert isinstance(incomplete, GraphDeploymentCelRefusal)
    assert incomplete.result == IncompleteRenderings(
        RenderingRefusal.CAPACITY_EXHAUSTED
    )
    for refusal in (invalid, incomplete):
        assert not hasattr(refusal, "groups")
        assert not hasattr(refusal, "logical")
        assert_private_deployment(refusal, "result")
    assert unavailable.reason is UnavailableReason.CLOSED
    assert_protected(unavailable, DEPLOYMENT_ENTRY)


def assert_deployment_suspended(
    task: asyncio.Task[PreparedGraphDeploymentInputs | GraphDeploymentCelRefusal],
) -> None:
    coroutine = task.get_coro()
    assert iscoroutine(coroutine)
    locals_at_wait: dict[str, object] = getcoroutinelocals(coroutine)

    assert locals_at_wait["contents"] == ()
    assert locals_at_wait["storage_identity"] is None
    assert type(locals_at_wait["groups"]) is tuple
    groups = cast(tuple[object, ...], locals_at_wait["groups"])
    assert len(groups) == 2


def check_deployment_cancellation() -> None:
    files = deployment_contents()
    ir_text = deployment_source(files)

    with borrowed() as (runner, evaluator):
        loop = runner.get_loop()
        owner = evaluator.owner
        for prior_deliveries in (0, 1):
            task = loop.create_task(
                prepare_graph_deployment_inputs(
                    ir_text,
                    files,
                    evaluator,
                    storage_identity=DEPLOYMENT_STORE,
                    limits=DEPLOYMENT_LIMITS,
                )
            )
            for _ in range(prior_deliveries):
                run_loop_turn(loop)
                wait_for_delivery(owner)
                run_loop_turn(loop)
            run_loop_turn(loop)
            active = wait_for_delivery(owner)
            assert_deployment_suspended(task)
            assert task.cancel(PRIVATE_STORE)
            with pytest.raises(asyncio.CancelledError) as cancelled:
                loop.run_until_complete(task)
            assert active.outstanding == active.deliveries == 1
            assert active.bytes > 0
            assert cancelled.value.args == ()
            assert_protected(cancelled.value, DEPLOYMENT_ENTRY)
            assert_idle(owner, 1)
        reused = runner.run(
            prepare_graph_deployment_inputs(
                ir_text,
                files,
                evaluator,
                storage_identity=DEPLOYMENT_STORE,
                limits=DEPLOYMENT_LIMITS,
            )
        )

    assert isinstance(reused, PreparedGraphDeploymentInputs)
    assert reused.logical.canonical_ir == ir_text
    assert len(reused.groups) == 2


def run(scenario: str) -> None:
    cases = {
        "roundtrip": check_roundtrip,
        "capacity": check_capacity,
        "refusals": check_refusals,
        "cancellation": check_cancellation,
        "logical_roundtrip": check_logical_roundtrip,
        "logical_refusals": check_logical_refusals,
        "path_refusal": check_released_paths_on_manifest_refusal,
        "path_build_refusal": lambda: check_released_paths_on_manifest_refusal(
            logical=True
        ),
        "logical_cancellation": lambda: check_cancellation(logical=True),
        "deployment_roundtrip": check_deployment_roundtrip,
        "deployment_refusals": check_deployment_refusals,
        "deployment_cancellation": check_deployment_cancellation,
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
        ("path_refusal", ["complete"], 1),
        ("path_build_refusal", ["complete"], 1),
        ("logical_cancellation", ["complete"], 1),
        ("deployment_roundtrip", ["complete"] * 4, 4),
        ("deployment_refusals", ["complete", "capacity_exhausted"], 1),
        ("deployment_cancellation", ["complete"], 1),
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
    assert PRIVATE_STORE not in result.stderr
    assert PRIVATE_APPROVAL not in result.stderr
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
