import json
import re
from typing import cast

import pytest
from pydantic import Field, StrictStr, TypeAdapter, ValidationError

from kora.ir.record import IrRecord
from kora.ir.scalars import BranchTarget, Expression, NodeId
from tests.ir.support import schema_definition

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
