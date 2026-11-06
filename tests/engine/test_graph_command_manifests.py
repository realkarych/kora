import json
import pickle
from dataclasses import replace
from hashlib import sha256
from typing import cast

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    build_declared_command_manifests,
    check_declared_command_manifests,
)
from kora.ir.json import JsonObject, JsonValue
from tests.engine.graph_output_cases import child, output_document

LIMITS = CommandManifestLimits(200_000, 2_000_000, 8_000_000)
SECTION = "kora.command-resources.v1"
EMPTY = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
EXAMPLE = "sha256:ac238bb1de5db1d8c8cee691626af721119ffc241431f22603ce77c22af1e305"
FILE = "sha256:62bdb208de6dc6b169a467f646f50ca39e6c203fa6f6732431624d8dec1e77fb"
ZERO = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
MARKER = "private-command-filename"


def resource(
    name: str, size: int = 0, digest: str = ZERO, *, executable: bool = False
) -> JsonObject:
    return {"name": name, "sha256": digest, "size": size, "executable": executable}


def authored_manifest(files: list[JsonObject]) -> JsonObject:
    ordered = sorted(files, key=lambda entry: cast(str, entry["name"]).encode())
    text = json.dumps(
        ordered, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    digest = "sha256:" + sha256(text.encode()).hexdigest()
    return {"resources": list(files), "resource_set_digest": digest}


def command_document(
    config: JsonObject, body_config: JsonObject | None = None
) -> JsonObject:
    raw = output_document("command")
    child(raw, "graph", "nodes", "producer", "config").update(config)
    if body_config is not None:
        body = output_document("command")
        child(body, "graph", "nodes", "producer", "config").update(body_config)
        child(raw, "graph")["subgraphs"] = {
            "body": {"nodes": child(body, "graph", "nodes")}
        }
    return raw


def canonical(raw: JsonObject) -> str:
    return render_object(raw, TextBudget(LIMITS.ir_bytes))


def entries(section: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], section["commands"])


def assert_content_free(refusal: GraphValueRejected, reason: GraphValueFailure) -> None:
    assert refusal.reason is reason
    assert refusal.args == (reason.value,)
    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    assert MARKER not in repr(refusal)


@pytest.mark.parametrize(
    ("config", "admitted"),
    [
        ({}, True),
        ({"resources": []}, True),
        ({"resources": [], "resource_set_digest": EMPTY}, True),
        ({"resources": [], "resource_set_digest": ZERO}, False),
        ({"resource_set_digest": EMPTY}, False),
        ({"resource_set_digest": ZERO}, False),
        ({"resources": [resource("file")]}, False),
        ({"resources": [resource("file")], "resource_set_digest": EMPTY}, False),
        ({"resources": None}, False),
        ({"resources": [], "resource_set_digest": None}, False),
    ],
)
def test_command_manifest_admits_only_defined_empty_and_missing_cases(
    config: JsonObject, admitted: bool
) -> None:
    text = canonical(command_document(config))
    expected: JsonObject = {
        "interpretation": SECTION,
        "commands": [
            {
                "path": ["graph", "nodes", "producer"],
                "resource_set_digest": None,
                "resources": [],
            }
        ],
    }

    if not admitted:
        with pytest.raises(GraphValueRejected) as refusal:
            build_declared_command_manifests(text, limits=LIMITS)

        assert_content_free(refusal.value, GraphValueFailure.INVALID)
    else:
        result = build_declared_command_manifests(text, limits=LIMITS)
        checked = check_declared_command_manifests(text, expected, limits=LIMITS)

        assert result.text == checked.text == canonical(expected)


def test_command_manifest_reproduces_published_jcs_and_preserves_raw_ir() -> None:
    main = resource("select_best.py", 11, FILE)
    native = resource('bin/run "ñ".sh', executable=True)
    authored = dict(main)
    authored["size"] = 11.0
    authored["x-retained"] = -0.0
    raw = command_document(
        {"resources": [authored, native], "resource_set_digest": EXAMPLE}
    )
    text = canonical(raw)

    prepared = build_declared_command_manifests(text, limits=LIMITS)
    checked = check_declared_command_manifests(
        text, ObjectReader(prepared.text).object(), limits=LIMITS
    )

    assert text == canonical(raw)
    assert '"size":11.0' in text
    assert '"x-retained":-0.0' in text
    assert (
        prepared.text
        == checked.text
        == canonical(
            {
                "interpretation": SECTION,
                "commands": [
                    {
                        "path": ["graph", "nodes", "producer"],
                        "resource_set_digest": EXAMPLE,
                        "resources": [native, main],
                    }
                ],
            }
        )
    )


def test_command_manifest_orders_paths_and_utf8_names() -> None:
    config = authored_manifest([resource("é"), resource("z")])
    raw = command_document(config, config)
    nodes = child(raw, "graph", "nodes")
    nodes["z"] = nodes["producer"]
    nodes["a"] = nodes.pop("producer")
    text = canonical(raw)

    prepared = build_declared_command_manifests(text, limits=LIMITS)
    records = entries(ObjectReader(prepared.text).object())

    assert [record["path"] for record in records] == [
        ["graph", "nodes", "a"],
        ["graph", "nodes", "z"],
        ["graph", "subgraphs", "body", "nodes", "producer"],
    ]
    assert all(
        record["resources"] == [resource("z"), resource("é")] for record in records
    )


@pytest.mark.parametrize("name", ["e\u0301", "\ud800", "../file", "foo//bar", "a\\b"])
def test_command_manifest_refuses_unsafe_or_noncanonical_names(name: str) -> None:
    text = canonical(
        command_document({"resources": [resource(name)], "resource_set_digest": ZERO})
    )

    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(text, limits=LIMITS)

    assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_manifest_rejects_duplicates_even_with_matching_digest() -> None:
    text = canonical(
        command_document(authored_manifest([resource("same"), resource("same")]))
    )

    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(text, limits=LIMITS)

    assert_content_free(refusal.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize("boundary", ["files", "content"])
def test_command_manifest_counts_each_occurrence_across_bodies(boundary: str) -> None:
    if boundary == "files":
        files = [resource(f"file-{index}") for index in range(32)]
    else:
        files = [resource("first", 262144), resource("second", 262144)]
    extra = resource("excess", 1 if boundary == "content" else 0)
    config = authored_manifest(files)
    exact = canonical(command_document(config, config))
    excess = canonical(command_document(config, authored_manifest([*files, extra])))

    result = build_declared_command_manifests(exact, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(excess, limits=LIMITS)

    assert len(entries(ObjectReader(result.text).object())) == 2
    assert_content_free(refusal.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize(
    "change",
    [
        "interpretation",
        "order",
        "path",
        "digest",
        "integer",
        "file-order",
        "extra",
        "duplicate",
    ],
)
def test_saved_command_manifest_requires_exact_order_and_native_values(
    change: str,
) -> None:
    text = canonical(
        command_document(authored_manifest([resource("a", 1), resource("b")]))
    )
    expected = build_declared_command_manifests(text, limits=LIMITS)
    saved = ObjectReader(expected.text).object()
    commands = entries(saved)
    entry = commands[0]
    files = cast(list[JsonObject], entry["resources"])
    if change == "interpretation":
        saved["interpretation"] = "kora.command-resources.v2"
    elif change == "order":
        entry["path"] = entry.pop("path")
    elif change == "path":
        entry["path"] = ["graph", "nodes", "other"]
    elif change == "digest":
        entry["resource_set_digest"] = ZERO
    elif change == "integer":
        files[0]["size"] = 1.0
    elif change == "file-order":
        files.reverse()
    elif change == "extra":
        files[0]["x-protected"] = MARKER
    else:
        commands.append(entry.copy())

    with pytest.raises(GraphValueRejected) as refusal:
        check_declared_command_manifests(text, saved, limits=LIMITS)

    assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_manifest_budget_precedes_large_unrelated_number_parsing() -> None:
    raw = command_document({})
    raw["x-number"] = 10**1400 + 19
    numeric = canonical(raw)
    raw["x-number"] = '\\"' + "1" * 1401
    quoted = canonical(raw)
    limits = replace(LIMITS, work=100_000)

    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(numeric, limits=limits)
    number = build_declared_command_manifests(numeric, limits=LIMITS)
    string = build_declared_command_manifests(quoted, limits=limits)

    assert_content_free(refusal.value, GraphValueFailure.BUDGET)
    assert number.text == string.text


@pytest.mark.parametrize("dimension", ["source", "scratch", "work"])
def test_command_manifest_requires_all_bounded_inputs(dimension: str) -> None:
    text = canonical(command_document({}))
    ir_bytes = len(text) if dimension != "source" else len(text) - 1
    limits = CommandManifestLimits(
        ir_bytes,
        1 if dimension == "scratch" else LIMITS.scratch,
        1 if dimension == "work" else LIMITS.work,
    )

    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(text, limits=limits)

    assert_content_free(refusal.value, GraphValueFailure.BUDGET)


def test_command_manifest_refuses_invalid_inputs_without_exposing_them() -> None:
    invalid_json = '{"' + MARKER + '": "'
    invalid_structure = canonical(command_document({"resources": MARKER}))
    text = canonical(command_document(authored_manifest([resource(MARKER)])))
    prepared = build_declared_command_manifests(text, limits=LIMITS)
    protected_slot = "_text"
    invalid_member: JsonValue = {"interpretation": MARKER, "commands": []}

    with pytest.raises(GraphValueRejected) as syntax:
        build_declared_command_manifests(invalid_json, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as structure:
        build_declared_command_manifests(invalid_structure, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as saved:
        check_declared_command_manifests(text, invalid_member, limits=LIMITS)
    with pytest.raises(TypeError, match="require explicit access"):
        pickle.dumps(prepared)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(prepared)
    with pytest.raises(AttributeError, match="are immutable"):
        setattr(prepared, protected_slot, MARKER)
    with pytest.raises(AttributeError, match="are immutable"):
        delattr(prepared, protected_slot)

    assert all(MARKER not in str(error) for error in encoder.value.args)
    assert MARKER not in repr(prepared)
    for refusal in (syntax, structure, saved):
        assert_content_free(refusal.value, GraphValueFailure.INVALID)


def test_command_manifest_comparison_spends_the_same_work_allowance() -> None:
    text = canonical(command_document(authored_manifest([resource("bounded")])))
    prepared = build_declared_command_manifests(text, limits=LIMITS)
    saved = ObjectReader(prepared.text).object()
    low, high = 1, LIMITS.work
    while low < high:
        middle = (low + high) // 2
        try:
            build_declared_command_manifests(text, limits=replace(LIMITS, work=middle))
        except GraphValueRejected as refusal:
            assert_content_free(refusal, GraphValueFailure.BUDGET)
            low = middle + 1
        else:
            high = middle
    tight = replace(LIMITS, work=high)

    exact = build_declared_command_manifests(text, limits=tight)
    with pytest.raises(GraphValueRejected) as comparison:
        check_declared_command_manifests(text, saved, limits=tight)

    assert exact.text == prepared.text
    assert_content_free(comparison.value, GraphValueFailure.BUDGET)


def test_command_manifest_construction_spends_scratch_after_source_admission() -> None:
    text = canonical(command_document(authored_manifest([resource("bounded")])))
    source_only = replace(LIMITS, scratch=3 * len(text))

    with pytest.raises(GraphValueRejected) as refusal:
        build_declared_command_manifests(text, limits=source_only)

    assert_content_free(refusal.value, GraphValueFailure.BUDGET)
