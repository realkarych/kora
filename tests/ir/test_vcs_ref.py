import json
from collections import UserDict
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import MISSING, TypeAdapter, ValidationError
from referencing import Registry

from kora.ir import PrincipalRef, VcsRef
from kora.ir.json import JsonObject
from kora.ir.vcs_ref import (
    VcsRefValidationError,
    canonical_vcs_ref,
    parse_vcs_ref,
    parse_vcs_ref_json,
    require_vcs_ref_revision,
    summarize_vcs_ref,
    vcs_ref_digest,
)
from kora.ir.vcs_ref_json import JsonRefusal, decode_vcs_ref_json
from tests.ir.vcs_ref_cases import (
    ARTIFACTS,
    construct,
    document,
    exact_size,
    example,
    oversized_objects,
    principal,
    unchecked,
    wire,
)

SCHEMA_HASH: str = "6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8"
VECTOR_HASH: str = "e978ea1463001e37edfb3970b0e3352cc929fde73efe5958958aedd9c9b5c955"


class SchemaValidator(Protocol):
    def is_valid(self, instance: JsonObject) -> bool: ...


def test_builtin_revision_1_schemas_are_immutable() -> None:
    schema = (ARTIFACTS / "VcsRef.schema.json").read_bytes()
    manifest = (ARTIFACTS / "VcsRef.revision-1.json").read_bytes()
    descriptor: object = json.loads(manifest)

    require_vcs_ref_revision(descriptor)

    assert sha256(schema).hexdigest() == SCHEMA_HASH
    assert sha256(manifest).hexdigest() == (
        "c7454de60a8bce7f06f1d0e98b5c5cb796ed45a7a8020d0c0e1f64fdbba83051"
    )
    assert sha256(
        (ARTIFACTS / "VcsRef.digest-vector.json").read_bytes()
    ).hexdigest() == (
        "37fb01577e0e9b3152b9e1b79832cf73bcbe8ace9a4ac60c04189e0d6f1c0289"
    )
    assert sha256((ARTIFACTS / "VcsRef.url-vectors.json").read_bytes()).hexdigest() == (
        "2b64d201f50890c7c9b6a06baf6bc47d2846e15bf5661c4725f2be6bcb09eac4"
    )


def test_builtin_digest_selection_is_stable() -> None:
    document = example()

    value = parse_vcs_ref(document)
    wire = canonical_vcs_ref(value)

    assert len(wire) == 298
    assert vcs_ref_digest(value) == VECTOR_HASH
    assert parse_vcs_ref_json(wire) == value
    assert summarize_vcs_ref(value) == document
    assert isinstance(value.pushed_by, PrincipalRef)
    assert isinstance(value, VcsRef)


def test_vcs_ref_rejects_invalid_payload() -> None:
    document = {**example(), "unexpected": "private-marker"}

    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(document)

    assert refused.value.violation.path == ()
    assert "private-marker" not in repr(refused.value)


def untitled(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: untitled(item)
            for key, item in cast("dict[str, object]", value).items()
            if key != "title"
        }
    if isinstance(value, list):
        return [untitled(item) for item in cast("list[object]", value)]
    return value


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_vcs_ref_schema_matches_model(mode: str) -> None:
    expected = cast(
        "JsonObject", json.loads((ARTIFACTS / "VcsRef.schema.json").read_bytes())
    )
    schema_mode = cast('Literal["validation", "serialization"]', mode)
    principal_schema = cast("JsonObject", expected["$defs"])["PrincipalRef"]

    actual = untitled(VcsRef.model_json_schema(mode=schema_mode))
    actual_principal = untitled(PrincipalRef.model_json_schema(mode=schema_mode))

    assert actual == expected
    assert actual_principal == principal_schema


@pytest.mark.parametrize(
    ("changes", "valid"),
    [
        ({}, True),
        ({"diff_stat": "\b\f\n\r\t\u0001\u0085😀"}, True),
        ({"commit": "native revision " + "😀" * 20}, True),
        ({"commit_url": "https://例え.test:000443/a%ff"}, True),
        ({"url": "https://example.test/r", "opened_by": principal()}, True),
        ({"url": None}, False),
        ({"opened_by": None}, False),
        ({"compare_url": None}, False),
        ({"url": "https://example.test/r"}, False),
        ({"opened_by": principal()}, False),
        ({"changed_paths": ["../a"]}, False),
        ({"changed_paths": ["a", "a"]}, False),
        ({"pushed_by": {**principal(), "kind": "unknown"}}, False),
        ({"pushed_by": {**principal(), "private-key": "private"}}, False),
        ({"commit": "a" * 256}, False),
        ({"commit": True}, False),
        ({"repo": ""}, False),
        ({"private-key": "private"}, False),
    ],
)
def test_vcs_ref_structural_corpus_agrees_with_independent_schema(
    changes: dict[str, object], valid: bool
) -> None:
    schema = cast(
        "JsonObject", json.loads((ARTIFACTS / "VcsRef.schema.json").read_bytes())
    )
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )
    candidate = {**document(), **changes}
    value: VcsRef | None = None
    parsed: VcsRef | None = None

    schema_accepts = validator.is_valid(cast("JsonObject", candidate))
    if valid:
        value = construct(VcsRef, candidate)
        parsed = parse_vcs_ref(candidate)
    else:
        with pytest.raises(VcsRefValidationError):
            construct(VcsRef, candidate)
        with pytest.raises(VcsRefValidationError):
            parse_vcs_ref(candidate)

    assert schema_accepts is valid
    if valid:
        assert value is not None
        assert parsed is not None
        assert summarize_vcs_ref(value) == candidate
        assert value == parsed


@pytest.mark.parametrize(
    "fixture", sorted((ARTIFACTS / "examples/invalid").glob("*.json"))
)
def test_vcs_ref_rejects_published_negative_fixtures(fixture: "Path") -> None:
    payload = fixture.read_bytes()

    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref_json(payload)

    assert payload


@pytest.mark.parametrize(
    "field",
    [
        "repo",
        "branch",
        "commit",
        "base_commit",
        "diff_stat",
        "pushed_by",
        "changed_paths",
    ],
)
def test_vcs_ref_required_fields_are_not_defaulted(field: str) -> None:
    candidate = document()
    del candidate[field]
    bypassed = unchecked(candidate)

    for value in (candidate, bypassed):
        with pytest.raises(VcsRefValidationError) as refused:
            parse_vcs_ref(value)

        assert refused.value.violation.path == (field,)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\xff", "VCS002"),
        (b"\xef\xbb\xbf{}", "VCS002"),
        (b'{"repo":"a","repo":"b"}', "VCS002"),
        (b'{"pushed_by":{"kind":"user","kind":"app"}}', "VCS002"),
        (b'{"unknown":"\\ud800"}', "VCS002"),
        (b'{"unknown":"\\u0000"}', "VCS002"),
        (b'{"unknown":NaN}', "VCS002"),
        (b'{"unknown":Infinity}', "VCS002"),
        (b'{"unknown":-Infinity}', "VCS002"),
        (b'{"unknown":1e9999}', "VCS002"),
        (b'{"unknown":' + b"1" * 5000 + b"}", "VCS002"),
        (b"[" * 17 + b"]" * 17, "VCS002"),
        (b'{"a":"unterminated}', "VCS002"),
        (b'{"a": 1,}', "VCS002"),
        (b"null", "VCS003"),
        (b"[]", "VCS003"),
        (b"\xff" * 1_048_577, "VCS001"),
    ],
)
def test_vcs_ref_raw_refusals_are_safe(payload: bytes, code: str) -> None:
    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref_json(payload)

    error = refused.value
    assert error.violation.code == code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "unknown" not in repr(error)
    assert "unterminated" not in repr(error)


@pytest.mark.parametrize("depth", [16, 17])
def test_vcs_ref_lexical_depth_precedes_decoder(depth: int) -> None:
    payload = b"[" * depth + b"0" + b"]" * depth

    decoded = decode_vcs_ref_json(payload)

    assert (decoded is JsonRefusal.DECODE) is (depth == 17)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/a",
        "a/",
        "a//b",
        ".",
        "..",
        "a/./b",
        "a/../b",
        "a\\b",
        "a\n",
        "a\x7f",
        "\u2028/../a",
        "\u2029/./b",
    ],
)
def test_vcs_ref_rejects_invalid_paths(path: str) -> None:
    candidate = {**document(), "changed_paths": [path]}

    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(candidate)

    assert refused.value.violation.path == ("changed_paths", 0)


@pytest.mark.parametrize("field", ["commit", "base_commit"])
@pytest.mark.parametrize("unit", ["a", "😀"])
def test_vcs_ref_revision_scalar_limit(field: str, unit: str) -> None:
    accepted = {**document(), field: unit * 255}
    refused = {**document(), field: unit * 256}

    value = parse_vcs_ref(accepted)
    with pytest.raises(VcsRefValidationError) as error:
        parse_vcs_ref(refused)

    assert getattr(value, field) == unit * 255
    assert error.value.violation.path == (field,)


@pytest.mark.parametrize("field", ["provider_instance", "stable_id", "display_name"])
@pytest.mark.parametrize("unit", ["a", "😀"])
def test_vcs_ref_principal_scalar_limit(field: str, unit: str) -> None:
    accepted = {**principal(), field: unit * 255}
    refused = {**principal(), field: unit * 256}

    value = PrincipalRef.model_validate(accepted)
    with pytest.raises(VcsRefValidationError) as error:
        PrincipalRef.model_validate(refused)

    assert getattr(value, field) == unit * 255
    assert error.value.violation.path == (field,)


def url_cases() -> list[dict[str, object]]:
    vector = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "VcsRef.url-vectors.json").read_bytes()),
    )
    return cast("list[dict[str, object]]", vector["cases"])


@pytest.mark.parametrize(
    "case", url_cases(), ids=lambda case: cast("str", case["name"])
)
def test_vcs_ref_semantic_url_vectors(case: dict[str, object]) -> None:
    candidate = {**document(), "commit_url": case["url"]}
    value: VcsRef | None = None

    if case["accepted"]:
        value = parse_vcs_ref(candidate)
    else:
        with pytest.raises(VcsRefValidationError):
            parse_vcs_ref(candidate)

    if case["accepted"]:
        assert value is not None
        assert value.commit_url == case["url"]


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://example.test/",
        "https://user@example.test/",
        "https:///",
        "https://[::1",
        "https://::1/",
        "https://example.test/a[b]",
        "https://example.test/?a[b]",
        "https://example.test/\ufdd0",
        "https://example.test/\U0001fffe",
    ],
)
def test_vcs_ref_url_grammar_refuses_non_iri(url: str) -> None:
    candidate = {**document(), "commit_url": url}

    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref(candidate)

    assert candidate["commit_url"] == url


@pytest.mark.parametrize("unit", ["a", "😀", "\u0001"])
def test_vcs_ref_behavior_bounds(unit: str) -> None:
    candidate = exact_size(1_048_576, unit)

    value = parse_vcs_ref(candidate)
    encoded = canonical_vcs_ref(value)
    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(
            {**candidate, "diff_stat": cast("str", candidate["diff_stat"]) + "a"}
        )

    assert len(encoded) == 1_048_576
    assert refused.value.violation.code == "VCS006"


def test_vcs_ref_raw_byte_limit_is_independent_of_canonical_size() -> None:
    payload = wire(document())
    at_limit = payload + b" " * (1_048_576 - len(payload))

    value = parse_vcs_ref_json(at_limit)
    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref_json(at_limit + b" ")

    assert vcs_ref_digest(value) == VECTOR_HASH
    assert refused.value.violation.code == "VCS001"


def test_vcs_ref_path_cardinality_and_aggregate_bytes() -> None:
    paths = [f"{index:05d}" for index in range(10_000)]
    prefix = "😀" * (524_288 // 4 - 1)
    at_limit = [prefix + "aaaa"]

    value = parse_vcs_ref({**document(), "changed_paths": paths})
    bounded = parse_vcs_ref({**document(), "changed_paths": at_limit})
    with pytest.raises(VcsRefValidationError) as cardinality:
        parse_vcs_ref({**document(), "changed_paths": [*paths, "extra"]})
    with pytest.raises(VcsRefValidationError) as aggregate:
        parse_vcs_ref({**document(), "changed_paths": [at_limit[0] + "a"]})

    assert len(value.changed_paths) == 10_000
    assert bounded.changed_paths == tuple(at_limit)
    assert cardinality.value.violation.code == "VCS003"
    assert aggregate.value.violation.code == "VCS004"


@pytest.mark.parametrize(
    ("name", "code", "path"),
    [
        ("wide", "VCS003", ()),
        ("path_count", "VCS003", ("changed_paths",)),
        ("long_paths", "VCS004", ("changed_paths",)),
        ("last_control", "VCS003", ("changed_paths", 9999)),
        ("principal_before_bytes", "VCS003", ("pushed_by", "kind")),
        ("display", "VCS006", ()),
        ("url", "VCS006", ()),
    ],
)
def test_vcs_ref_oversized_object_phase_order(
    name: str, code: str, path: tuple[str | int, ...]
) -> None:
    candidate = oversized_objects()[name]
    bypassed = unchecked(candidate)

    for action in (
        lambda: parse_vcs_ref(candidate),
        lambda: construct(VcsRef, candidate),
        lambda: parse_vcs_ref(bypassed),
        lambda: canonical_vcs_ref(bypassed),
        lambda: vcs_ref_digest(bypassed),
        lambda: summarize_vcs_ref(bypassed),
    ):
        with pytest.raises(VcsRefValidationError) as refused:
            action()

        assert refused.value.violation.code == code
        assert refused.value.violation.path == path


def test_vcs_ref_shape_precedes_semantic_url() -> None:
    candidate = {
        **document(),
        "url": "https://[::1]suffix/",
        "opened_by": {**principal(), "private-key": "private-marker"},
    }

    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(candidate)

    assert refused.value.violation.path == ("opened_by",)


def test_vcs_ref_shape_uses_schema_field_order() -> None:
    candidate = {**document(), "repo": None}
    del candidate["branch"]

    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(candidate)

    assert refused.value.violation.path == ("repo",)


@pytest.mark.parametrize("field", ["url", "opened_by", "commit_url", "compare_url"])
def test_vcs_ref_optional_null_is_refused_in_every_object_route(field: str) -> None:
    candidate = {**document(), field: None}
    bypassed = unchecked(candidate)

    for action in (
        lambda: construct(VcsRef, candidate),
        lambda: VcsRef.model_validate(candidate, strict=False, extra="ignore"),
        lambda: VcsRef.model_validate(bypassed),
    ):
        with pytest.raises(VcsRefValidationError) as refused:
            action()

        assert refused.value.violation.path == (field,)


def test_vcs_ref_immutable_snapshot_and_fresh_summary() -> None:
    candidate = document()
    paths = cast("list[str]", candidate["changed_paths"])
    publisher = cast("dict[str, object]", candidate["pushed_by"])
    value = parse_vcs_ref(candidate)
    digest = vcs_ref_digest(value)

    paths.append("replaced")
    publisher["display_name"] = "replaced"
    summary = summarize_vcs_ref(value)
    cast("dict[str, object]", summary["pushed_by"])["display_name"] = "changed"
    cast("list[str]", summary["changed_paths"]).append("changed")
    with pytest.raises(ValidationError):
        value.pushed_by.display_name = "changed"
    with pytest.raises(ValidationError):
        value.commit = "changed"

    assert vcs_ref_digest(value) == digest
    assert value.changed_paths == ("src/café.txt",)
    assert value.pushed_by.display_name == "Publisher ñ"


def test_vcs_ref_optional_omission_and_principal_identity() -> None:
    value = parse_vcs_ref(document())
    changed = value.pushed_by.model_copy(
        update={"display_name": "Other", "kind": "local_service"}
    )

    dumps = [
        value.model_dump(
            mode="json", exclude_none=exclude_none, exclude_unset=exclude_unset
        )
        for exclude_none in (False, True)
        for exclude_unset in (False, True)
    ]

    assert all(result == document() for result in dumps)
    assert value.url is MISSING
    assert value.pushed_by != changed
    assert value.pushed_by.identity() == changed.identity()


def test_vcs_ref_present_optional_fields_survive_serialization_flags() -> None:
    candidate = {
        **document(),
        "url": "https://example.test/change",
        "opened_by": principal(),
        "commit_url": "https://example.test/commit",
        "compare_url": "https://example.test/compare",
    }
    value = parse_vcs_ref(candidate)

    dumped = [
        value.model_dump(
            mode="json",
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
        )
        for exclude_none in (False, True)
        for exclude_unset in (False, True)
        for exclude_defaults in (False, True)
    ]

    assert all(result == candidate for result in dumped)


def test_vcs_ref_canonical_identity_preserves_recorded_content() -> None:
    candidate = {
        **document(),
        "changed_paths": ["😀/a", "é/b"],
        "diff_stat": '\b\f\n\r\t\u0001\u007f\u0085"\\😀',
        "url": "https://EXAMPLE.test:000443/a/../b%2f",
        "opened_by": principal(),
    }
    value = parse_vcs_ref(candidate)
    reordered = dict(reversed(list(candidate.items())))
    reordered["pushed_by"] = dict(reversed(list(principal().items())))
    expected = wire(candidate)

    encoded = canonical_vcs_ref(value)
    stable = parse_vcs_ref(reordered)
    swapped = parse_vcs_ref({**candidate, "changed_paths": ["é/b", "😀/a"]})
    renamed = parse_vcs_ref(
        {**candidate, "pushed_by": {**principal(), "display_name": "Other"}}
    )

    assert encoded == expected
    assert encoded.endswith(b"}")
    assert b"\\u0001" in encoded
    assert "😀".encode() in encoded
    assert vcs_ref_digest(stable) == vcs_ref_digest(value)
    assert vcs_ref_digest(swapped) != vcs_ref_digest(value)
    assert vcs_ref_digest(renamed) != vcs_ref_digest(value)
    assert value.url == candidate["url"]


def test_vcs_ref_unchecked_nested_principal_is_revalidated() -> None:
    publisher = PrincipalRef.model_construct().model_copy(
        update={**principal(), "kind": "private-marker"}
    )
    value = parse_vcs_ref(document()).model_copy(update={"pushed_by": publisher})

    for action in (
        lambda: PrincipalRef.model_validate(publisher),
        lambda: publisher.identity(),
        lambda: parse_vcs_ref(value),
        lambda: canonical_vcs_ref(value),
        lambda: vcs_ref_digest(value),
        lambda: summarize_vcs_ref(value),
    ):
        with pytest.raises(VcsRefValidationError) as refused:
            action()

        assert "private-marker" not in repr(refused.value)


@pytest.mark.parametrize("factory", [dict, UserDict])
def test_vcs_ref_refuses_arbitrary_mapping_and_scalar_subclasses(
    factory: Callable[[dict[str, object]], object],
) -> None:
    class ForeignText(str):
        pass

    candidate = factory({**document(), "commit": ForeignText("revision:2")})

    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref(candidate)

    assert candidate is not None


def test_vcs_ref_raw_override_preserves_preflight_and_flags_cannot_relax_shape() -> (
    None
):
    duplicate = wire(document()).replace(
        b'"repo":"sample"', b'"repo":"sample","repo":"sample"'
    )
    unknown = wire({**document(), "private-key": "private-marker"})

    for payload in (duplicate, unknown):
        with pytest.raises(VcsRefValidationError) as refused:
            VcsRef.model_validate_json(payload, strict=False, extra="ignore")

        assert refused.value.__context__ is None
        assert "private-marker" not in repr(refused.value)


def test_vcs_ref_generic_raw_ingress_cannot_recover_duplicate_evidence() -> None:
    duplicate = wire(document()).replace(
        b'"repo":"sample"', b'"repo":"sample","repo":"sample"'
    )
    adapter = TypeAdapter(VcsRef)

    generic = adapter.validate_json(duplicate)
    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref_json(duplicate)

    assert generic == parse_vcs_ref(document())


def test_vcs_ref_dense_refusal_keeps_only_one_safe_violation() -> None:
    candidate = {
        **document(),
        **{
            f"private-key-marker-{index}": "secret-payload-marker"
            for index in range(100_000)
        },
    }

    with pytest.raises(VcsRefValidationError) as refused:
        parse_vcs_ref(candidate)
    error = refused.value
    rendered = repr(error) + repr(error.__dict__) + error.violation.model_dump_json()

    assert set(error.__dict__) == {"violation"}
    assert error.violation.path == ()
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "private-key-marker" not in rendered
    assert "secret-payload-marker" not in rendered


@pytest.mark.parametrize("field", ["changed_paths", "pushed_by"])
def test_vcs_ref_collection_subclasses_are_not_admitted(field: str) -> None:
    class ForeignList(list[str]):
        pass

    class ForeignDict(dict[str, object]):
        pass

    item: object = (
        ForeignList(["a"]) if field == "changed_paths" else ForeignDict(principal())
    )
    candidate = {**document(), field: item}

    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref(candidate)
    with pytest.raises(VcsRefValidationError):
        parse_vcs_ref(ForeignDict(document()))

    assert candidate[field] is item


@pytest.mark.parametrize(
    "target",
    [
        "name",
        "builtin_revision",
        "schema",
        "parent",
        "behavior",
        "validation",
        "digest_selection",
        "schema_sha256",
        "unknown",
    ],
)
def test_vcs_ref_revision_descriptor_refuses_changed_bindings(target: str) -> None:
    descriptor = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "VcsRef.revision-1.json").read_bytes()),
    )
    descriptor[target] = "private-marker"

    with pytest.raises(VcsRefValidationError) as refused:
        require_vcs_ref_revision(descriptor)

    assert refused.value.violation.code == "VCS007"
    assert "private-marker" not in repr(refused.value)
