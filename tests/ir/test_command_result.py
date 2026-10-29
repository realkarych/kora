import base64
import json
import traceback
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from hashlib import sha256
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry

from kora.ir import (
    CommandResult,
    CommandResultValidationError,
    canonical_command_result,
    command_result_digest,
    parse_command_result,
    parse_command_result_json,
    require_command_result_revision,
    summarize_command_result,
    verify_command_result_captures,
)
from kora.ir.json import JsonObject
from tests.ir.command_result_cases import (
    dense,
    fixture,
    fixture_bytes,
    forged,
    resource_path,
    rows,
    stream_bytes,
    structural_invalid_paths,
    with_paths,
    with_resources,
    with_stream,
)
from tests.ir.test_command_result_contract import ARTIFACTS, SchemaValidator


@pytest.mark.parametrize(
    "case", rows("canonical_cases"), ids=lambda case: str(case["name"])
)
def test_command_result_canonical_identity(case: dict[str, object]) -> None:
    wire = cast("str", case["canonical"]).encode()
    native: object = json.loads(wire)

    raw_value = parse_command_result_json(wire)
    native_value = CommandResult(native)
    digest = command_result_digest(raw_value)

    assert canonical_command_result(raw_value) == wire
    assert canonical_command_result(native_value) == wire
    assert digest == case["canonical_sha256"] == case["selected_sha256"]
    assert len(wire) == case["canonical_bytes"]
    assert summarize_command_result(raw_value) == native
    assert canonical_command_result(parse_command_result(raw_value)) == wire


def test_command_result_schema_matches_model() -> None:
    schema = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "CommandResult.schema.json").read_bytes()),
    )
    descriptor: object = json.loads(
        (ARTIFACTS / "CommandResult.revision-1.json").read_bytes()
    )
    validator = cast(
        "SchemaValidator", Draft202012Validator(schema, registry=Registry())
    )
    candidates = [
        fixture(cast("str", case["name"])) for case in rows("canonical_cases")
    ]

    require_command_result_revision(descriptor)
    values = [parse_command_result(candidate).value for candidate in candidates]

    assert all(validator.is_valid(value) for value in values)
    assert all(
        validator.is_valid(json.loads(path.read_bytes())) is False
        for path in structural_invalid_paths()
    )


@pytest.mark.parametrize("path", structural_invalid_paths(), ids=lambda path: path.stem)
def test_command_result_rejects_invalid_payload(path: object) -> None:
    from pathlib import Path

    assert isinstance(path, Path)
    payload = path.read_bytes()

    with pytest.raises(CommandResultValidationError):
        parse_command_result_json(payload)


@pytest.mark.parametrize(
    "case", rows("semantic_cases"), ids=lambda case: str(case["name"])
)
def test_command_result_semantic_vectors(case: dict[str, object]) -> None:
    value = case["value"]

    with pytest.raises(CommandResultValidationError):
        parse_command_result(value)


@pytest.mark.parametrize(
    "case",
    [case for case in rows("replay_cases") if "after" in case],
    ids=lambda case: str(case["name"]),
)
def test_command_result_all_published_identity_changes(case: dict[str, object]) -> None:
    before = parse_command_result(fixture(cast("str", case["before"])))
    after = parse_command_result(case["after"])

    old_digest = command_result_digest(before)
    changed_digest = command_result_digest(after)

    assert old_digest != changed_digest
    assert changed_digest == case["complete_sha256"] == case["selected_sha256"]


@pytest.mark.parametrize("field", ["duration_ms", "exit_code", "signal"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, 2**63, -(2**63)])
def test_command_result_integer_domain(field: str, value: object) -> None:
    candidate: dict[str, object] = dict(fixture())
    candidate[field] = value
    if field == "signal":
        candidate["exit_code"] = -1

    with pytest.raises(CommandResultValidationError):
        parse_command_result(candidate)


@pytest.mark.parametrize("token", [b"0.0", b"0e0", b"NaN", b"Infinity", b"true"])
def test_command_result_raw_integer_tokens(token: bytes) -> None:
    raw = fixture_bytes().replace(b'"exit_code":0', b'"exit_code":' + token)

    with pytest.raises(CommandResultValidationError):
        parse_command_result_json(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"a":0,"\\u0061":1}',
        b'"\\ud800"',
        b"[" * 17 + b"]" * 17,
        b"1" * 5000,
    ],
)
def test_command_result_raw_encoding_failures(raw: bytes) -> None:
    with pytest.raises(CommandResultValidationError) as failure:
        parse_command_result_json(raw)

    assert failure.value.violation.code == "CR002"


@pytest.mark.parametrize("size", [1048576, 1048577])
def test_command_result_raw_byte_limit(size: int) -> None:
    wire = fixture_bytes()
    raw = wire + b" " * (size - len(wire))

    if size == 1048577:
        with pytest.raises(CommandResultValidationError) as failure:
            parse_command_result_json(raw)
        assert failure.value.violation.code == "CR001"
    else:
        value = parse_command_result_json(raw)
        assert canonical_command_result(value) == wire


@pytest.mark.parametrize(
    ("factory", "accepted"),
    [
        (lambda: with_resources([f"a{i:02d}" for i in range(64)]), True),
        (lambda: with_resources([f"a{i:02d}" for i in range(65)]), False),
        (lambda: with_resources(["a"], 262144), True),
        (lambda: with_resources(["a"], 262145), False),
        (lambda: with_resources(["a" * 255]), True),
        (lambda: with_resources(["a" * 256]), False),
        (lambda: with_resources([resource_path(4096)]), True),
        (lambda: with_resources([resource_path(4097)]), False),
        (lambda: with_paths(["a"] * 64), True),
        (lambda: with_paths(["a"] * 65), False),
        (lambda: with_paths(["a" * 4096]), True),
        (lambda: with_paths(["a" * 4097]), False),
        (lambda: with_paths(["/".join(["a"] * 32)]), True),
        (lambda: with_paths(["/".join(["a"] * 33)]), False),
        (lambda: with_paths(["a" * 4096] * 16), True),
        (lambda: with_paths(["a" * 4096] * 16 + ["b"]), False),
        (lambda: with_paths(["ñ" * 4096] * 4), True),
        (lambda: with_paths(["ñ" * 4096] * 4 + ["ñ"]), False),
        (lambda: with_paths(["a*" * 127 + "a"] * 64), True),
        (lambda: with_paths(["a*" * 128 + "a"] * 64), False),
        (dense, True),
    ],
)
def test_command_result_behavior_bounds(
    factory: Callable[[], object], accepted: bool
) -> None:
    source = factory()

    if not accepted:
        with pytest.raises(CommandResultValidationError):
            parse_command_result(source)
        return
    value = parse_command_result(source)

    assert len(canonical_command_result(value)) <= 1048576
    assert value.value == source


@pytest.mark.parametrize(
    ("field", "limit"), [("repo", 4096), ("commit", 255), ("base_commit", 255)]
)
@pytest.mark.parametrize("extra", [0, 1])
def test_command_result_revision_and_repo_limits(
    field: str, limit: int, extra: int
) -> None:
    source = fixture()
    source[field] = "a" * (limit + extra)

    if extra:
        with pytest.raises(CommandResultValidationError):
            parse_command_result(source)
    else:
        value = parse_command_result(source)
        assert value.value == source


def test_command_result_repo_utf8_bound_and_native_revision_domain() -> None:
    source = fixture()
    source["repo"] = "😀" * 1024
    source["commit"] = "😀" * 255
    source["base_commit"] = 'native revision / "ñ"'

    value = parse_command_result(source)
    source["repo"] += "a"
    with pytest.raises(CommandResultValidationError):
        parse_command_result(source)

    assert value.value["commit"] == "😀" * 255


@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 67108864])
def test_command_result_capture_integrity_and_limits(size: int) -> None:
    payload = b"a" * size
    source = with_stream(payload)
    value = parse_command_result(source)

    verify_command_result_captures(value, stdout=payload, stderr=b"")
    with pytest.raises(CommandResultValidationError):
        verify_command_result_captures(value, stdout=payload + b"!", stderr=b"")

    assert cast("JsonObject", value.value["stdout"])["size"] == size


def test_command_result_capture_checks_both_bounds_before_work() -> None:
    value = parse_command_result(fixture())
    oversized = b"x" * 67108865

    with pytest.raises(CommandResultValidationError) as failure:
        verify_command_result_captures(value, stdout=b"", stderr=oversized)

    assert failure.value.violation.code == "CR006"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", 67108865),
        ("observed_bytes", 2**63),
        ("observed_bytes", -1),
        ("observed_bytes", True),
        ("size", 1.0),
        ("eof", 1),
        ("truncated", "false"),
        ("storage_ref", "a b"),
        ("storage_ref", "ñ"),
        ("storage_ref", "a" * 257),
        ("tail_base64", "YR=="),
        ("tail_base64", "YQ"),
        ("tail_base64", "a" * 5465),
    ],
)
def test_command_result_stream_domains(field: str, value: object) -> None:
    source: dict[str, object] = dict(fixture())
    stream: dict[str, object] = dict(stream_bytes(b"a"))
    stream[field] = value
    source["stdout"] = stream

    with pytest.raises(CommandResultValidationError):
        parse_command_result(source)


def test_command_result_valid_base64_wrong_retained_suffix() -> None:
    payload = b"a" * 4097
    source = with_stream(payload)
    capture = cast("JsonObject", source["stdout"])
    capture["tail_base64"] = base64.b64encode(b"b" * 4096).decode()
    value = parse_command_result(source)

    with pytest.raises(CommandResultValidationError) as failure:
        verify_command_result_captures(value, stdout=payload, stderr=b"")

    assert failure.value.violation.code == "CR006"


def test_command_result_known_empty_without_eof_and_count_limit() -> None:
    source = fixture()
    source["duration_ms"] = 2**63 - 1
    source["stdout"] = stream_bytes(b"", observed=2**63 - 1, eof=False)

    value = parse_command_result(source)
    verify_command_result_captures(value, stdout=b"", stderr=b"")

    assert cast("JsonObject", value.value["stdout"])["truncated"] is True


def test_command_result_ownership_and_array_order() -> None:
    source = with_paths(["tests/**", "Makefile", "tests/**"])
    value = parse_command_result(source)
    original = canonical_command_result(value)
    detached = value.value
    summary = summarize_command_result(value)

    source["protected_paths"] = []
    detached["repo"] = "changed"
    cast("JsonObject", summary["stdout"])["size"] = 1
    reordered = with_paths(["Makefile", "tests/**", "tests/**"])

    assert canonical_command_result(value) == original
    assert command_result_digest(value) != command_result_digest(
        parse_command_result(reordered)
    )
    assert value.value["protected_paths"] == ["tests/**", "Makefile", "tests/**"]
    assert repr(value) == "CommandResult()"
    with pytest.raises(FrozenInstanceError):
        assign_field(value, "_canonical", b"{}")


def assign_field(value: object, name: str, item: object) -> None:
    setattr(value, name, item)


def detached_value(value: CommandResult) -> JsonObject:
    return value.value


@pytest.mark.parametrize(
    "action",
    [
        canonical_command_result,
        command_result_digest,
        summarize_command_result,
        parse_command_result,
        detached_value,
    ],
)
@pytest.mark.parametrize("raw", [None, b"{}", fixture_bytes() + b"\n", b" " * 1048577])
def test_command_result_forged_exports(
    action: Callable[[CommandResult], object], raw: object
) -> None:
    value = forged(raw)

    with pytest.raises(CommandResultValidationError):
        action(value)


def test_command_result_missing_state() -> None:
    value = object.__new__(CommandResult)

    with pytest.raises(CommandResultValidationError):
        value.canonical_bytes()


@pytest.mark.parametrize("change", ["extra", "integer", "schema", "cycle"])
def test_command_result_exact_descriptor(change: str) -> None:
    descriptor = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "CommandResult.revision-1.json").read_bytes()),
    )
    if change == "extra":
        descriptor["extra"] = True
    elif change == "integer":
        cast("dict[str, object]", descriptor["validation"])["max_json_depth"] = 16.0
    elif change == "schema":
        descriptor["schema_sha256"] = "0" * 64
    else:
        descriptor["behavior"] = descriptor

    with pytest.raises(CommandResultValidationError) as failure:
        require_command_result_revision(descriptor)

    assert failure.value.violation.code == "CR005"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"secret-content":',
        b'"\\ud800-secret-content"',
        b'{"exit_code":0,"secret-content":1}',
    ],
)
def test_command_result_safe_diagnostics(raw: bytes) -> None:
    failure: CommandResultValidationError | None = None

    try:
        parse_command_result_json(raw)
    except CommandResultValidationError as error:
        failure = error

    assert failure is not None
    assert failure.__context__ is None
    assert failure.__cause__ is None
    assert "secret-content" not in str(failure)
    assert "secret-content" not in "".join(traceback.format_exception(failure))
    assert failure.violation.path == ()


def test_command_result_resource_manifest_supplementary_unicode() -> None:
    source = with_resources(['bin/run "ñ".sh', "😀/file", "\ue000/file"])

    value = parse_command_result(source)
    canonical = canonical_command_result(value)
    reversed_members = dict(reversed(list(source.items())))

    assert command_result_digest(value) == sha256(canonical).hexdigest()
    assert canonical_command_result(parse_command_result(reversed_members)) == canonical


class ExternalText(str):
    pass


class ExternalList(list[object]):
    pass


class ExternalMap(dict[str, object]):
    pass


@pytest.mark.parametrize(
    "candidate",
    [ExternalMap(fixture()), (), set[object](), None, 0, ExternalText("result")],
)
def test_command_result_exact_native_root(candidate: object) -> None:
    with pytest.raises(CommandResultValidationError):
        parse_command_result(candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo", ExternalText("repository")),
        ("protected_paths", ExternalList(["tests/**"])),
        ("resources", ()),
        ("stdout", ExternalMap(stream_bytes(b""))),
        ("termination", "teardown_after_result"),
        ("stop_reason", "unknown"),
        ("repo", "private\ud800"),
        ("commit", "line\nrevision"),
    ],
)
def test_command_result_no_native_coercion(field: str, value: object) -> None:
    source: dict[str, object] = dict(fixture())
    source[field] = value

    with pytest.raises(CommandResultValidationError):
        parse_command_result(source)


@pytest.mark.parametrize(
    "name", ["", "/a", "a//b", "./a", "a/../b", "a\\b", "a\x7f", "e\u0301", "a\ud800"]
)
def test_command_result_resource_name_domain(name: str) -> None:
    source = with_resources(["valid"])
    cast("JsonObject", cast("list[object]", source["resources"])[0])["name"] = name

    with pytest.raises(CommandResultValidationError):
        parse_command_result(source)


def test_command_result_large_native_values_and_cycles_refused() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    candidates: list[object] = [
        {str(index): None for index in range(10000)},
        {**fixture(), "repo": "a" * 1048577},
        {**fixture(), "resources": [None] * 1048576},
        {**fixture(), "protected_paths": cycle},
    ]

    for candidate in candidates:
        with pytest.raises(CommandResultValidationError):
            parse_command_result(candidate)


def test_command_result_raw_guard_precedes_structure_and_decode() -> None:
    raw = b"\xff" + b"[" * 1048576

    with pytest.raises(CommandResultValidationError) as failure:
        parse_command_result_json(raw)

    assert failure.value.violation.code == "CR001"


def test_command_result_complete_capture_digest_covers_bytes_before_tail() -> None:
    retained = b"prefix" + b"a" * 4096
    changed = b"PREFIX" + b"a" * 4096
    value = parse_command_result(with_stream(retained))

    with pytest.raises(CommandResultValidationError) as failure:
        verify_command_result_captures(value, stdout=changed, stderr=b"")

    assert len(retained) == len(changed)
    assert retained[-4096:] == changed[-4096:]
    assert failure.value.violation.code == "CR006"
