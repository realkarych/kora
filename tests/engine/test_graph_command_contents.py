import pickle
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_command_manifests import (
    MAX_FILES,
    CommandManifestLimits,
    CommandResourceContent,
    PreparedCommandStageGroup,
    build_declared_command_manifests,
    prepare_graph_command_stage_groups,
    verify_graph_command_contents,
)
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.ir.json import JsonObject
from kora.protocols.storage_models import StorageIdentity
from tests.engine.test_graph_command_manifests import (
    LIMITS,
    MARKER,
    ZERO,
    assert_content_free,
    authored_manifest,
    canonical,
    command_document,
    resource,
)

ROOT = ("graph", "nodes", "producer")
BODY = ("graph", "subgraphs", "body", "nodes", "producer")
STAGE_STORE = StorageIdentity(version=1, store_id="Selected-Store")


def source_for(
    root: tuple[CommandResourceContent, ...],
    body: tuple[CommandResourceContent, ...] = (),
) -> str:
    def declaration(contents: tuple[CommandResourceContent, ...]) -> JsonObject:
        return authored_manifest(
            [
                resource(
                    item.name, len(item.data), "sha256:" + sha256(item.data).hexdigest()
                )
                for item in contents
            ]
        )

    return canonical(
        command_document(declaration(root), declaration(body) if body else None)
    )


def test_command_contents_keep_exact_canonical_order_and_original_bytes() -> None:
    payload = b"shared content"
    root_first = CommandResourceContent(ROOT, "A.txt", payload)
    root_empty = CommandResourceContent(ROOT, "é.txt", b"")
    body_shared = CommandResourceContent(BODY, "A.txt", payload)
    ir_text = source_for((root_empty, root_first), (body_shared,))

    verified = verify_graph_command_contents(
        ir_text, (body_shared, root_empty, root_first), limits=LIMITS
    )

    assert verified[0] is root_first
    assert verified[1] is root_empty
    assert verified[2] is body_shared
    assert verified[0].data is verified[2].data is payload


def test_command_contents_accept_an_empty_manifest_without_payloads() -> None:
    ir_text = source_for(())

    verified = verify_graph_command_contents(ir_text, (), limits=LIMITS)

    assert verified == ()


@pytest.mark.parametrize(
    "mismatch",
    ["omitted", "extra", "duplicate", "size", "digest", "swapped", "mutable", "path"],
)
def test_command_contents_refuse_inexact_occurrences(mismatch: str) -> None:
    root = CommandResourceContent(ROOT, "file", b"one")
    body = CommandResourceContent(BODY, "file", b"two")
    ir_text = source_for((root,), (body,))
    candidates = {
        "omitted": (root,),
        "extra": (root, body, CommandResourceContent(ROOT, "other", b"")),
        "duplicate": (root, root),
        "size": (root, CommandResourceContent(BODY, "file", b"two!")),
        "digest": (root, CommandResourceContent(BODY, "file", b"one")),
        "swapped": (
            CommandResourceContent(BODY, "file", root.data),
            CommandResourceContent(ROOT, "file", body.data),
        ),
        "mutable": (
            root,
            CommandResourceContent(
                BODY, "file", cast(bytes, cast(object, bytearray(b"two")))
            ),
        ),
        "path": (
            root,
            CommandResourceContent(("graph", "nodes", "other"), "file", body.data),
        ),
    }

    with pytest.raises(GraphValueRejected) as refusal:
        verify_graph_command_contents(ir_text, candidates[mismatch], limits=LIMITS)

    assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_contents_verify_the_manifest_digest_even_when_files_match() -> None:
    data = b"correct file contents"
    file = resource("file", len(data), "sha256:" + sha256(data).hexdigest())
    ir_text = canonical(
        command_document({"resources": [file], "resource_set_digest": ZERO})
    )
    contents = (CommandResourceContent(ROOT, "file", data),)

    with pytest.raises(GraphValueRejected) as refusal:
        verify_graph_command_contents(ir_text, contents, limits=LIMITS)

    assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_contents_admit_the_exact_file_count_and_refuse_one_over() -> None:
    contents = tuple(
        CommandResourceContent(ROOT, f"file-{index:02d}", b"")
        for index in range(MAX_FILES)
    )
    ir_text = source_for(contents)
    over = (*contents, CommandResourceContent(ROOT, "one-over", b""))

    accepted = verify_graph_command_contents(ir_text, contents, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as refusal:
        verify_graph_command_contents(ir_text, over, limits=LIMITS)

    assert accepted == contents
    assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_contents_admit_exact_decoded_limits_and_refuse_aggregate_over() -> (
    None
):
    payload = b"x" * COMMAND_RESOURCE_MAX_BYTES
    root = tuple(
        CommandResourceContent(ROOT, f"file-{index}", payload) for index in range(2)
    )
    body = tuple(
        CommandResourceContent(BODY, f"file-{index}", payload) for index in range(2)
    )
    empty = CommandResourceContent(BODY, "tail", b"")
    ir_text = source_for(root, (*body, empty))
    aggregate_over = (*root, *body, CommandResourceContent(BODY, empty.name, b"x"))
    source_too_small = replace(LIMITS, ir_bytes=1)

    accepted = verify_graph_command_contents(
        ir_text, (*root, *body, empty), limits=LIMITS
    )
    with pytest.raises(GraphValueRejected) as aggregate_refusal:
        verify_graph_command_contents(ir_text, aggregate_over, limits=source_too_small)

    assert accepted == (*root, *body, empty)
    assert all(item.data is payload for item in accepted[:-1])
    assert_content_free(aggregate_refusal.value, GraphValueFailure.INVALID)


def test_command_contents_refuse_a_single_oversized_file_before_reading_ir() -> None:
    root = CommandResourceContent(ROOT, "first", b"x")
    late = CommandResourceContent(BODY, "last", b"y" * COMMAND_RESOURCE_MAX_BYTES)
    over = CommandResourceContent(BODY, late.name, late.data + b"y")
    ir_text = source_for((root,), (late,))
    limits = replace(LIMITS, ir_bytes=1)

    with pytest.raises(GraphValueRejected) as source_refusal:
        verify_graph_command_contents(ir_text, (root, late), limits=limits)
    with pytest.raises(GraphValueRejected) as file_refusal:
        verify_graph_command_contents(ir_text, (root, over), limits=limits)

    assert_content_free(source_refusal.value, GraphValueFailure.BUDGET)
    assert_content_free(file_refusal.value, GraphValueFailure.INVALID)


def test_command_contents_reserve_all_hash_work_before_any_payload_digest() -> None:
    first = CommandResourceContent(ROOT, "first", b"a")
    second = CommandResourceContent(ROOT, "second", b"x" * 131_072)
    ir_text = source_for((first, second))
    wrong_first = CommandResourceContent(ROOT, first.name, b"b")
    limits = replace(LIMITS, work=200_000)

    manifest = build_declared_command_manifests(
        ir_text, limits=replace(LIMITS, work=50_000)
    )
    with pytest.raises(GraphValueRejected) as budget:
        verify_graph_command_contents(ir_text, (wrong_first, second), limits=limits)
    with pytest.raises(GraphValueRejected) as mismatch:
        verify_graph_command_contents(ir_text, (wrong_first, second), limits=LIMITS)

    assert first.name in manifest.text
    assert second.name in manifest.text
    assert_content_free(budget.value, GraphValueFailure.BUDGET)
    assert_content_free(mismatch.value, GraphValueFailure.INVALID)


def test_command_contents_budget_covers_additional_name_comparisons() -> None:
    canonical_order = tuple(
        CommandResourceContent(ROOT, "x" * 80 + str(index), b"") for index in range(8)
    )
    ir_text = source_for(canonical_order)
    low = 0
    high = LIMITS.work
    while low + 1 < high:
        middle = (low + high) // 2
        try:
            verify_graph_command_contents(
                ir_text, canonical_order, limits=replace(LIMITS, work=middle)
            )
        except GraphValueRejected:
            low = middle
        else:
            high = middle
    limits = replace(LIMITS, work=high)

    verified = verify_graph_command_contents(ir_text, canonical_order, limits=limits)
    with pytest.raises(GraphValueRejected) as refusal:
        verify_graph_command_contents(
            ir_text, tuple(reversed(canonical_order)), limits=limits
        )

    assert verified == canonical_order
    assert_content_free(refusal.value, GraphValueFailure.BUDGET)


def test_command_contents_protect_bytes_and_failure_frames() -> None:
    valid = CommandResourceContent(ROOT, MARKER, MARKER.encode())
    valid_tail = CommandResourceContent(ROOT, "tail", b"")
    ir_text = source_for((valid, valid_tail))
    wrong = CommandResourceContent(ROOT, MARKER, b"x" * len(valid.data))
    wide = CommandResourceContent(ROOT, MARKER * 1000, b"")
    limits = replace(LIMITS, scratch=16_000)
    reinitialize = cast(Callable[[tuple[str, ...], str, bytes], None], valid.__init__)

    verified = verify_graph_command_contents(
        ir_text, (valid, valid_tail), limits=LIMITS
    )
    with pytest.raises(AttributeError, match="immutable"):
        verified[0].name = "replacement"
    reinitialize(BODY, valid_tail.name, wrong.data)
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(verified)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(verified)
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        with pytest.raises(GraphValueRejected) as mismatch:
            verify_graph_command_contents(ir_text, (wrong, valid_tail), limits=limits)
        with pytest.raises(GraphValueRejected) as budget:
            verify_graph_command_contents(ir_text, (wrong, wide), limits=limits)

    assert MARKER not in repr(verified)
    assert MARKER not in repr(encoder.value)
    assert verified[0].path == ROOT
    assert verified[0].name == MARKER
    assert verified[0].data == MARKER.encode()
    assert_content_free(mismatch.value, GraphValueFailure.INVALID)
    assert_content_free(budget.value, GraphValueFailure.BUDGET)
    for refusal in (mismatch.value, budget.value):
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refusal.__traceback__)
        }
        assert set(frames["verify_graph_command_contents"]) == {"limits", "result"}
        assert "_contents_result" not in frames
        assert "_admit_contents" not in frames


def test_stage_groups_preserve_exact_address_order_and_canonical_ordinals() -> None:
    shared = b"private-stage-bytes"
    separate = b"separate content"
    root_first = CommandResourceContent(ROOT, "A.txt", shared)
    root_case = CommandResourceContent(ROOT, "a.txt", separate)
    root_empty = CommandResourceContent(ROOT, "é.txt", b"")
    body_shared = CommandResourceContent(BODY, "A.txt", bytes(bytearray(shared)))
    ir_text = source_for((root_empty, root_case, root_first), (body_shared,))
    supplied = (body_shared, root_empty, root_case, root_first)

    groups = prepare_graph_command_stage_groups(
        ir_text, supplied, storage_identity=STAGE_STORE, limits=LIMITS
    )

    assert body_shared.data is not root_first.data
    assert [group.ordinals for group in groups] == [(1,), (0, 3), (2,)]
    assert [group.store_id for group in groups] == [STAGE_STORE.store_id] * 3
    assert [group.payload_digest for group in groups] == [
        "sha256:" + sha256(data).hexdigest() for data in (separate, shared, b"")
    ]
    assert [group.total_size for group in groups] == [len(separate), len(shared), 0]
    assert groups[0].data is root_case.data
    assert groups[1].data is root_first.data
    assert groups[2].data is root_empty.data


def test_stage_groups_cover_the_full_distinct_address_inventory() -> None:
    files = tuple(
        CommandResourceContent(ROOT, f"file-{index:02d}", bytes((index,)))
        for index in range(MAX_FILES)
    )
    ir_text = source_for(files)

    groups = prepare_graph_command_stage_groups(
        ir_text, tuple(reversed(files)), storage_identity=STAGE_STORE, limits=LIMITS
    )

    assert len(groups) == MAX_FILES
    assert [group.payload_digest for group in groups] == sorted(
        "sha256:" + sha256(file.data).hexdigest() for file in files
    )
    assert sorted(group.ordinals for group in groups) == [
        (ordinal,) for ordinal in range(MAX_FILES)
    ]
    for group in groups:
        assert group.data is files[group.ordinals[0]].data


def test_stage_group_storage_identity_is_required_for_even_an_empty_file() -> None:
    empty = source_for(())
    occurrence = CommandResourceContent(ROOT, "empty", b"")
    declared_empty_file = source_for((occurrence,))

    fileless = prepare_graph_command_stage_groups(
        empty, (), storage_identity=None, limits=LIMITS
    )
    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_command_stage_groups(
            declared_empty_file, (occurrence,), storage_identity=None, limits=LIMITS
        )

    assert fileless == ()
    assert_content_free(refused.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize("mismatch", ["missing", "duplicate", "extra", "case", "bytes"])
def test_stage_groups_reject_inexact_occurrences(mismatch: str) -> None:
    first = CommandResourceContent(ROOT, "A.txt", b"one")
    second = CommandResourceContent(BODY, "B.txt", b"two")
    ir_text = source_for((first,), (second,))
    candidates = {
        "missing": (first,),
        "duplicate": (first, first),
        "extra": (first, second, CommandResourceContent(ROOT, "extra", b"")),
        "case": (CommandResourceContent(ROOT, "a.txt", first.data), second),
        "bytes": (first, CommandResourceContent(BODY, second.name, b"bad")),
    }

    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_command_stage_groups(
            ir_text, candidates[mismatch], storage_identity=STAGE_STORE, limits=LIMITS
        )

    assert_content_free(refused.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize(
    "identity",
    [
        StorageIdentity.model_construct(),
        StorageIdentity.model_construct(version=True, store_id=MARKER),
        StorageIdentity.model_construct(version=2, store_id=MARKER),
        StorageIdentity.model_construct(version=1, store_id=f"{MARKER} invalid"),
        StorageIdentity.model_construct(version=1, store_id=MARKER * 1000),
    ],
)
def test_stage_groups_refuse_malformed_selected_identity(
    identity: StorageIdentity,
) -> None:
    file = CommandResourceContent(ROOT, MARKER, MARKER.encode())
    ir_text = source_for((file,))

    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_command_stage_groups(
            ir_text, (file,), storage_identity=identity, limits=LIMITS
        )

    assert_content_free(refused.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize("limit", ["file", "aggregate", "occurrences"])
def test_stage_group_path_preserves_occurrence_and_byte_limits(limit: str) -> None:
    full_file = b"x" * COMMAND_RESOURCE_MAX_BYTES
    files = tuple(
        CommandResourceContent(ROOT, f"file-{index}", full_file) for index in range(4)
    )
    empty_files = tuple(
        CommandResourceContent(ROOT, f"empty-{index}", b"")
        for index in range(MAX_FILES)
    )
    empty_tail = CommandResourceContent(ROOT, "tail", b"")
    cases = {
        "file": (
            (files[0],),
            (CommandResourceContent(ROOT, files[0].name, full_file + b"x"),),
        ),
        "aggregate": (
            (*files, empty_tail),
            (*files, CommandResourceContent(ROOT, empty_tail.name, b"x")),
        ),
        "occurrences": (
            empty_files,
            (*empty_files, CommandResourceContent(ROOT, "tail", b"")),
        ),
    }
    exact, candidate = cases[limit]
    ir_text = source_for(exact)
    small_source = replace(LIMITS, ir_bytes=1)

    groups = prepare_graph_command_stage_groups(
        ir_text, exact, storage_identity=STAGE_STORE, limits=LIMITS
    )
    with pytest.raises(GraphValueRejected) as source_refused:
        prepare_graph_command_stage_groups(
            ir_text, exact, storage_identity=STAGE_STORE, limits=small_source
        )
    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_command_stage_groups(
            ir_text, candidate, storage_identity=STAGE_STORE, limits=small_source
        )

    assert sorted(ordinal for group in groups for ordinal in group.ordinals) == list(
        range(len(exact))
    )
    assert_content_free(source_refused.value, GraphValueFailure.BUDGET)
    assert_content_free(refused.value, GraphValueFailure.INVALID)


BudgetDimension = Literal["scratch", "work"]


def with_budget(
    limits: CommandManifestLimits, dimension: BudgetDimension, amount: int
) -> CommandManifestLimits:
    return (
        replace(limits, scratch=amount)
        if dimension == "scratch"
        else replace(limits, work=amount)
    )


def required_budget(
    operation: Callable[[CommandManifestLimits], object],
    dimension: BudgetDimension,
) -> int:
    low = 0
    high = LIMITS.scratch if dimension == "scratch" else LIMITS.work
    while low + 1 < high:
        middle = (low + high) // 2
        try:
            operation(with_budget(LIMITS, dimension, middle))
        except GraphValueRejected as refused:
            if refused.reason is not GraphValueFailure.BUDGET:
                raise
            low = middle
        else:
            high = middle
    return high


@pytest.mark.parametrize("dimension", ["scratch", "work"])
def test_stage_group_cumulative_budget_extends_the_existing_verification(
    dimension: BudgetDimension,
) -> None:
    first = CommandResourceContent(ROOT, "first", b"x" * 8_192)
    second = CommandResourceContent(BODY, "second", bytes(bytearray(first.data)))
    ir_text = source_for((first,), (second,))
    supplied = (first, second)

    def verify(limits: CommandManifestLimits) -> tuple[CommandResourceContent, ...]:
        return verify_graph_command_contents(ir_text, supplied, limits=limits)

    def prepare(limits: CommandManifestLimits) -> tuple[PreparedCommandStageGroup, ...]:
        return prepare_graph_command_stage_groups(
            ir_text, supplied, storage_identity=STAGE_STORE, limits=limits
        )

    verified_limit = required_budget(verify, dimension)
    group_limit = required_budget(prepare, dimension)

    verified = verify(with_budget(LIMITS, dimension, verified_limit))
    groups = prepare(with_budget(LIMITS, dimension, group_limit))
    with pytest.raises(GraphValueRejected) as grouping_at_verifier_limit:
        prepare(with_budget(LIMITS, dimension, verified_limit))
    with pytest.raises(GraphValueRejected) as one_below:
        prepare(with_budget(LIMITS, dimension, group_limit - 1))

    assert first.data is not second.data
    assert verified == supplied
    assert len(groups) == 1
    assert group_limit > verified_limit
    if dimension == "scratch":
        assert group_limit > verified_limit + 4 * len(STAGE_STORE.store_id)
    assert_content_free(grouping_at_verifier_limit.value, GraphValueFailure.BUDGET)
    assert_content_free(one_below.value, GraphValueFailure.BUDGET)


def test_stage_group_work_charges_each_duplicate_comparison_width() -> None:
    small, large = 16, 16_384

    def grouping_overhead(width: int) -> int:
        first = CommandResourceContent(ROOT, "first", b"x" * width)
        second = CommandResourceContent(BODY, "second", bytes(bytearray(first.data)))
        source = source_for((first,), (second,))
        supplied = (first, second)
        original = required_budget(
            lambda limits: verify_graph_command_contents(
                source, supplied, limits=limits
            ),
            "work",
        )
        grouped = required_budget(
            lambda limits: prepare_graph_command_stage_groups(
                source, supplied, storage_identity=STAGE_STORE, limits=limits
            ),
            "work",
        )
        return grouped - original

    difference = grouping_overhead(large) - grouping_overhead(small)

    assert difference == large - small


def test_stage_group_values_and_refusals_protect_private_data() -> None:
    file = CommandResourceContent(ROOT, MARKER, MARKER.encode())
    ir_text = source_for((file,))
    invalid_identity = StorageIdentity.model_construct(
        version=1, store_id=f"{MARKER} invalid"
    )
    tiny_budget = replace(LIMITS, scratch=1)

    groups = prepare_graph_command_stage_groups(
        ir_text, (file,), storage_identity=STAGE_STORE, limits=LIMITS
    )
    with pytest.raises(AttributeError, match="immutable"):
        groups[0].ordinals = ()
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(groups)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(groups)
    try:
        raise RuntimeError(MARKER)
    except RuntimeError:
        with pytest.raises(GraphValueRejected) as invalid:
            prepare_graph_command_stage_groups(
                ir_text, (file,), storage_identity=invalid_identity, limits=LIMITS
            )
        with pytest.raises(GraphValueRejected) as budget:
            prepare_graph_command_stage_groups(
                ir_text, (file,), storage_identity=STAGE_STORE, limits=tiny_budget
            )

    assert groups[0].data is file.data
    assert groups[0].ordinals == (0,)
    assert MARKER not in repr(groups)
    assert STAGE_STORE.store_id not in repr(groups)
    assert MARKER not in repr(encoder.value)
    assert_content_free(invalid.value, GraphValueFailure.INVALID)
    assert_content_free(budget.value, GraphValueFailure.BUDGET)
    for refusal in (invalid.value, budget.value):
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refusal.__traceback__)
        }
        assert set(frames["prepare_graph_command_stage_groups"]) == {"limits", "result"}
        assert "_stage_groups_result" not in frames
        assert "_stage_store_id" not in frames
