import pickle
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from traceback import walk_tb
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_command_manifests import (
    MAX_FILES,
    CommandResourceContent,
    build_declared_command_manifests,
    verify_graph_command_contents,
)
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.ir.json import JsonObject
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
