import json
import re
from collections.abc import Sequence
from typing import cast

import pytest
from pydantic import BaseModel, Field, StrictStr, TypeAdapter, ValidationError

from kora.ir import (
    BranchCase,
    BranchConfig,
    ErrorClass,
    FanOutConfig,
    Retry,
    SubgraphNodeConfig,
)
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES
from kora.ir.record import IrRecord
from kora.ir.scalars import BranchTarget, Expression, NodeId
from tests.ir.control_cases import CASE, MINIMAL, POPULATED
from tests.ir.support import (
    CONFIG_ERROR_MESSAGE,
    configuration_errors,
    nested,
    schema_definition,
)

ADAPTERS: dict[str, TypeAdapter[str]] = {
    "NodeId": TypeAdapter(NodeId),
    "BranchTarget": TypeAdapter(BranchTarget),
    "Expression": TypeAdapter(Expression),
}


class WireAliasRecord(IrRecord):
    else_: StrictStr = Field(alias="else")


def test_ir_control_models_preserve_wire_aliases() -> None:
    document = {"else": "target", "x-label": {"value": False}}

    parsed = WireAliasRecord.model_validate(document)
    restored = WireAliasRecord.model_validate_json(json.dumps(document))

    assert parsed.else_ == "target"
    assert parsed.model_dump(mode="json", by_alias=True) == document
    assert restored == parsed


@pytest.mark.parametrize(
    "document",
    [
        {"else_": "target"},
        {"else": "target", "else_": "other"},
        {"else": "target", "extensions": {}},
        {"else": "target", "unknown": False},
        {"else": None},
        {"else": False},
        {True: "target"},
    ],
)
def test_ir_control_wire_aliases_refuse_invalid_names(
    document: dict[object, object],
) -> None:

    with pytest.raises(ValidationError) as raised:
        WireAliasRecord.model_validate(document)

    assert raised.value.error_count() >= 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a", True),
        ("a0_inside_", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("a" * 64 + "\n", False),
        ("a\n", False),
        ("a\r", False),
        ("a\r\n", False),
        ("a\u2028", False),
        ("a\U0001f600", False),
        ("", False),
        ("_a", False),
        ("0a", False),
        ("A", False),
        (1, False),
        (False, False),
        (None, False),
        ([], False),
        ("a\x00", False),
        ("a\x7f", False),
        ("a\u2029", False),
        ("a\v", False),
        ("a\f", False),
    ],
)
@pytest.mark.parametrize("name", ["id", "to", "else", "from"])
def test_ir_identifier_patterns_match_schema_consumers(
    name: str, value: object, expected: bool
) -> None:
    definition = identifier_definition(name)
    pattern = cast("str", definition["pattern"])

    accepted = isinstance(value, str) and re.search(pattern, value) is not None

    assert accepted is expected


def identifier_definition(name: str) -> dict[str, object]:
    if name == "id":
        return cast("dict[str, object]", schema_definition("id"))
    definition = cast(
        "dict[str, object]",
        schema_definition("inputRef" if name == "from" else "branchConfig"),
    )
    properties = cast("dict[str, dict[str, object]]", definition["properties"])
    if name == "from":
        return properties["from"]
    if name == "else":
        return cast("dict[str, object]", properties["else"]["items"])
    case = cast("dict[str, object]", properties["cases"]["items"])
    fields = cast("dict[str, dict[str, object]]", case["properties"])
    return cast("dict[str, object]", fields["to"]["items"])


@pytest.mark.parametrize("name", ["id", "to", "else", "from"])
@pytest.mark.parametrize(
    "reserved", ["$fail_attempt", "$params", "$inputs", "$previous_attempt"]
)
@pytest.mark.parametrize("suffix", ["", "\n", "\r", "\r\n", "\u2028", "x"])
def test_ir_identifier_patterns_preserve_exact_reserved_values(
    name: str, reserved: str, suffix: str
) -> None:
    pattern = cast("str", identifier_definition(name)["pattern"])
    allowed: dict[str, set[str]] = {
        "id": set(),
        "to": {"$fail_attempt"},
        "else": {"$fail_attempt"},
        "from": {"$params", "$inputs", "$previous_attempt"},
    }
    expected = not suffix and reserved in allowed[name]

    accepted = re.search(pattern, reserved + suffix) is not None

    assert accepted is expected


@pytest.mark.parametrize(
    ("name", "value", "valid"),
    [
        ("NodeId", "a", True),
        ("NodeId", "a" * 64, True),
        ("NodeId", "a\n", False),
        ("NodeId", 1, False),
        ("BranchTarget", "$fail_attempt", True),
        ("BranchTarget", "$fail_attempt\n", False),
        ("BranchTarget", "$params", False),
        ("BranchTarget", "a0_b", True),
        ("Expression", "\U0001f600" * 4096, True),
        ("Expression", "\U0001f600" * 4097, False),
        ("Expression", "", False),
        ("Expression", "true\n", True),
        ("Expression", True, False),
    ],
    ids=[
        "short-id",
        "max-id",
        "id-newline",
        "id-number",
        "reserved-target",
        "reserved-target-newline",
        "reserved-source-not-target",
        "target-id",
        "max-expression-codepoints",
        "oversize-expression-codepoints",
        "empty-expression",
        "expression-newline",
        "expression-boolean",
    ],
)
def test_ir_control_scalars_preserve_structural_strings(
    name: str, value: object, valid: bool
) -> None:
    adapter = ADAPTERS[name]

    if valid:
        parsed = adapter.validate_python(value)
        restored = adapter.validate_json(json.dumps(value))
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(value))
        return

    assert parsed == value
    assert restored == value


ROUND_TRIPS: list[tuple[type[BaseModel], dict[str, object]]] = [
    *MINIMAL.items(),
    *POPULATED.items(),
    (BranchConfig, {"cases": [CASE], "else": []}),
    (Retry, {"retry_on": [], "non_retryable": []}),
    *[(Retry, {"strategy": strategy}) for strategy in ("fresh", "resume")],
    (Retry, {"backoff_coefficient": 10**400}),
    (Retry, {"backoff_coefficient": 1}),
    (Retry, {"backoff_coefficient": 1.0}),
    (Retry, {"max_attempts": 1}),
    (
        FanOutConfig,
        {"body": "b", "over": "o", "max_parallel": 10**400, "skip_failures": True},
    ),
    *[
        (model, {**document, "x-label": [None, False, {"name": "value"}]})
        for model, document in MINIMAL.items()
    ],
    (
        BranchConfig,
        {
            "cases": [{**CASE, "x-label": False}, {**CASE, "x-label": {"nested": []}}],
            "x-label": True,
        },
    ),
]


@pytest.mark.parametrize(("model", "document"), ROUND_TRIPS)
def test_ir_control_models_preserve_omission_and_wire_aliases(
    model: type[BaseModel], document: dict[str, object]
) -> None:
    encoded = json.dumps(document)

    parsed = model.model_validate(document)
    restored = model.model_validate_json(encoded)

    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == document
    assert (
        json.loads(parsed.model_dump_json(by_alias=True, exclude_unset=True))
        == document
    )
    assert restored == parsed


INVALID_FIELDS: Sequence[tuple[type[BaseModel], str, object]] = (
    [
        (model, name, None)
        for model, fields in POPULATED.items()
        for name in fields
        if not name.startswith("x-")
    ]
    + [
        (model, field, cast("object", {}))
        for model in MINIMAL
        for field in ("unknown", "extensions")
    ]
    + [
        (BranchCase, "to", value)
        for value in ([], "a", {"a"}, cast("object", {}), [False], ["a\n"], ["$params"])
    ]
    + [
        (BranchConfig, "cases", value)
        for value in (
            [],
            cast("object", {}),
            "case",
            [False],
            [{"when": "true"}],
            [{"to": ["a"]}],
        )
    ]
    + [
        (BranchConfig, "else", value)
        for value in ("a", {"a"}, cast("object", {}), [True], ["$fail_attempt\n"])
    ]
    + [(BranchConfig, "else_", [])]
    + [(FanOutConfig, "skip_failures", value) for value in (0, 1, "false")]
    + [
        (model, field, value)
        for model, field in (
            (FanOutConfig, "body"),
            (FanOutConfig, "over"),
            (SubgraphNodeConfig, "body"),
        )
        for value in ("a\n", "a" * 65, "$inputs", 1)
    ]
    + [
        (model, field, value)
        for model, field in ((FanOutConfig, "max_parallel"), (Retry, "max_attempts"))
        for value in (0, -1, 1.5, True, "1", float("nan"), float("inf"))
    ]
    + [(Retry, "max_attempts", 101)]
    + [
        (Retry, "backoff_coefficient", value)
        for value in (0, 0.99, -1, True, "1", float("nan"), float("inf"), -float("inf"))
    ]
    + [
        (Retry, field, value)
        for field in ("retry_on", "non_retryable")
        for value in (
            "timeout",
            {"timeout"},
            cast("object", {}),
            [True],
            [1],
            ["unknown"],
            ["timeout", "timeout"],
            [ErrorClass.TIMEOUT, "timeout"],
        )
    ]
    + [
        (Retry, field, value)
        for field in ("initial_interval", "max_interval")
        for value in ("1", "1s\n", 1)
    ]
    + [(Retry, "strategy", "unknown")]
    + [
        (model, field, value)
        for model, field in (
            (BranchCase, "when"),
            (BranchCase, "reason"),
            (BranchConfig, "else_reason"),
            (Retry, "predicate"),
        )
        for value in ("", "a" * 4097, True, 1)
    ]
)


@pytest.mark.parametrize(("model", "field", "value"), INVALID_FIELDS)
def test_ir_control_models_enforce_structural_boundaries(
    model: type[BaseModel], field: str, value: object
) -> None:
    document = {**MINIMAL[model], field: value}

    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)

    assert raised.value.error_count() >= 1


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (model, {key: value for key, value in fields.items() if key != required})
        for model, fields in MINIMAL.items()
        for required in fields
    ],
)
def test_ir_control_models_require_declared_fields(
    model: type[BaseModel], document: dict[str, object]
) -> None:

    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)

    assert raised.value.error_count() >= 1


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (BranchCase, "when"),
        (BranchCase, "reason"),
        (BranchConfig, "else_reason"),
        (Retry, "predicate"),
    ],
)
@pytest.mark.parametrize(
    "expression",
    ["a", "\U0001f600" * 4096, "true\n", "not parsed !!!"],
    ids=["one-codepoint", "max-codepoints", "raw-newline", "no-compilation"],
)
def test_ir_control_models_preserve_raw_expressions(
    model: type[BaseModel], field: str, expression: str
) -> None:
    document = {**MINIMAL[model], field: expression}

    parsed = model.model_validate_json(json.dumps(document))

    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == document


@pytest.mark.parametrize(
    ("model", "field"), [(Retry, "max_attempts"), (FanOutConfig, "max_parallel")]
)
def test_ir_control_models_accept_integral_json_numbers(
    model: type[BaseModel], field: str
) -> None:
    document = {**MINIMAL[model], field: 1.0}

    parsed = model.model_validate(document)

    assert parsed.model_dump(mode="json", exclude_unset=True) == {**document, field: 1}
    assert type(getattr(parsed, field)) is int


MARKER: str = "confidential-input-7319"


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (BranchCase, {"when": MARKER * 500, "to": [MARKER]}),
        (BranchConfig, {"cases": [{"when": "true", "to": [MARKER]}], "else": [MARKER]}),
        (
            BranchConfig,
            {"cases": [{**CASE, "x-data": nested(DEFAULT_MAX_DEPTH + 1, MARKER)}]},
        ),
        (BranchConfig, {"cases": [{**CASE, MARKER: False}]}),
        (FanOutConfig, {"body": MARKER, "over": MARKER}),
        (Retry, {"retry_on": [MARKER, 7319], "predicate": MARKER * 500}),
        (Retry, {"x-data": {MARKER: "x" * MAX_CANONICAL_BYTES}}),
    ],
)
def test_ir_control_errors_exclude_input_content(
    model: type[BaseModel], document: dict[str, object]
) -> None:

    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)
    safe = configuration_errors(raised.value)

    assert len(safe) == raised.value.error_count()
    assert MARKER not in repr(safe)
    assert "7319" not in repr(safe)
    assert all(item.msg == CONFIG_ERROR_MESSAGE for item in safe)
    assert all(
        all(segment == PLACEHOLDER_LOCATION for segment in item.loc) for item in safe
    )
    assert all(item.type for item in safe)
