import hashlib
import math
import re
from pathlib import Path

import pytest

from kora.cel import Diagnostic
from kora.cel.characters import is_rejected
from kora.cel_eval.binding import Binding
from tests.cel.conformance.cases import (
    BoolValue,
    BytesValue,
    Case,
    CaseModelError,
    DoubleValue,
    DurationValue,
    ExpectedError,
    ExpectedValue,
    IntValue,
    ListValue,
    MapType,
    MapValue,
    NullValue,
    ObjectValue,
    PrimitiveType,
    StringValue,
    read_cases,
    vendored_files,
)
from tests.cel.conformance.deviations import DEVIATIONS
from tests.cel.conformance.manifest import COMMIT, FILES, LICENSE_SHA256
from tests.cel.conformance.running import run_case
from tests.cel.conformance.selection import (
    CEL_STANDARD_FUNCTIONS,
    EXCLUDED_FILES,
    FEATURE_REASONS,
    INCLUDED_FILES,
    KORA_REJECTIONS,
    Feature,
    Rejection,
    rejection_for,
)
from tests.cel.conformance.textproto import (
    Message,
    Number,
    Text,
    TextprotoError,
    Word,
    read_textproto,
)
from tests.cel.support import checked_syntax

HERE = Path(__file__).resolve().parent

SAMPLE = """# a comment
name: "sample"
section {
  name: 'adjacent' 'strings'
  test {
    name: "escapes"
    expr: "'\\\\x41' + \\"\\\\u00e9\\""
    value: { string_value: "A\\303\\251\\u00e9" }
  }
  test <
    name: "list";
    expr: "[1, 2]",
    value: { list_value {
      values { int64_value: -1 } values { double_value: inf }
    } }
  >
  test {
    name: "map"
    expr: "{1: true}"
    type_env: {
      name: "x",
      ident: { type: { map_type: {
        key_type: { primitive: STRING } value_type: { primitive: INT64 }
      } } }
    }
    bindings { key: "x" value: { value: { null_value: NULL_VALUE } } }
    value: { map_value { entries {
      key: { int64_value: 1 } value: { bool_value: true }
    } } }
  }
  test {
    name: "error"
    expr: "1 / 0"
    eval_error: { errors: { message: "divide by zero" } }
  }
  test { name: "default" expr: "true" }
  test { name: "bytes" expr: "b''" value: { bytes_value: "\\377" } }
  test { name: "astral" expr: "x" value: { string_value: "\\U0001F600" } }
  test { name: "infinite" expr: "x" value: { double_value: Infinity } }
  test {
    name: "object"
    expr: "x"
    value: { object_value {
      [type.googleapis.com/google.protobuf.Duration] { seconds: 1 }
    } }
  }
  test {
    name: "wrapped"
    expr: "x"
    value: { object_value {
      [type.googleapis.com/google.protobuf.Int64Value] { value: 1 }
    } }
  }
}
"""


def test_cel_textproto_reader() -> None:
    document = read_textproto(SAMPLE)
    cases = read_cases("sample", SAMPLE).cases

    assert isinstance(document, Message)
    assert document.fields[0].value == Text(b"sample")
    section = document.fields[1].value
    assert isinstance(section, Message)
    assert section.fields[0].value == Text(b"adjacentstrings")
    by_name = {case.name: case for case in cases}
    assert by_name["escapes"].expression == "'\\x41' + \"\\u00e9\""
    assert by_name["escapes"].expected == ExpectedValue(StringValue("A\u00e9\u00e9"))
    listed = by_name["list"].expected
    assert isinstance(listed, ExpectedValue)
    assert isinstance(listed.value, ListValue)
    assert listed.value.values[0] == IntValue(-1)
    second = listed.value.values[1]
    assert isinstance(second, DoubleValue)
    assert math.isinf(second.value)
    mapped = by_name["map"]
    assert mapped.declarations[0].type == MapType(
        PrimitiveType("STRING"), PrimitiveType("INT64")
    )
    assert mapped.bindings == (("x", NullValue()),)
    assert mapped.expected == ExpectedValue(MapValue(((IntValue(1), BoolValue(True)),)))
    assert by_name["error"].expected == ExpectedError(("divide by zero",))
    assert by_name["default"].expected == ExpectedValue(BoolValue(True))
    assert by_name["bytes"].expected == ExpectedValue(BytesValue(b"\xff"))
    assert by_name["astral"].expected == ExpectedValue(StringValue("\U0001f600"))
    infinite = by_name["infinite"].expected
    assert isinstance(infinite, ExpectedValue)
    assert isinstance(infinite.value, DoubleValue)
    assert infinite.value.value == math.inf
    assert by_name["object"].expected == ExpectedValue(DurationValue(1, 0))
    assert by_name["wrapped"].expected == ExpectedValue(
        ObjectValue("type.googleapis.com/google.protobuf.Int64Value")
    )


@pytest.mark.parametrize(
    "text",
    [
        'name: "unterminated',
        "name: 'a\\q'",
        "name: 'a'\nunknown_field: 1",
        "section { test { name: 'a' surprise: 1 } }",
        "section { test { name: 'a' value: { int64_value: 1 } eval_error: {} } }",
        "section { test { name: 'a' value: { int64_value: 1 bool_value: true } } }",
        "section {",
        "name 'missing colon'",
        "section { test { name: 'a' value: { uint64_value: -1 } } }",
        "section { test { name: 'a' value: { int64_value: 9223372036854775808 } } }",
        "section { test { name: 'a' value: { uint64_value: 18446744073709551616 } } }",
        "section { test { name: 'a' value: { int64_value: 0b101 } } }",
        "section { test { name: 'a' value: { int64_value: 017 } } }",
        "section { test { name: 'a' value: { double_value: 1.5f } } }",
        "section { test { name: 'a' value: { string_value: '\\377' } } }",
        "section { test { name: 'a' description { anything: 1 } } }",
        "section { description: 1.5 test { name: 'a' } }",
        "description: 7",
        "section { test { name: 'a' value: { null_value: 1.5 } } }",
        "section { test { name: 'a' value: { object_value { [] {} } } } }",
        "section { test { name: 'a' value: { object_value { "
        "[type.googleapis.com/google.protobuf.Duration] { days: 1 } } } } }",
        "section { test { name: 'a' "
        "bindings { key: 'x' value { value { int64_value: 1 } } } "
        "bindings { key: 'x' value { value { int64_value: 2 } } } } }",
    ],
)
def test_cel_textproto_reader_refuses_unknown_input(text: str) -> None:
    with pytest.raises((TextprotoError, CaseModelError)):
        read_cases("broken", text)


def test_cel_textproto_scalars_keep_their_kind() -> None:
    document = read_textproto("a: 1 b: -2.5 c: NULL_VALUE d: 'x' e: -inf")

    values = [entry.value for entry in document.fields]

    assert values == [
        Number("1"),
        Number("-2.5"),
        Word("NULL_VALUE"),
        Text(b"x"),
        Number("-inf"),
    ]


def test_cel_conformance_data_matches_manifest() -> None:
    files = {path.name: path for path in vendored_files()}
    licence = (HERE / "LICENSE").read_bytes()

    digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in files.items()
    }

    assert digests == FILES
    assert hashlib.sha256(licence).hexdigest() == LICENSE_SHA256
    assert {name.removesuffix(".textproto") for name in files} == set(INCLUDED_FILES)
    assert not {excluded.name for excluded in EXCLUDED_FILES} & set(INCLUDED_FILES)
    assert re.fullmatch("[0-9a-f]{40}", COMMIT)


def test_cel_conformance_cases_decode_their_values() -> None:
    files = {path.stem: path for path in vendored_files()}

    cases = {
        (case.file, case.section, case.name): case
        for name in ("basic", "fields")
        for case in read_cases(name, files[name].read_text(encoding="utf-8")).cases
    }
    zero = cases[("basic", "self_eval_zeroish", "self_eval_int_zero")]
    bound = cases[("basic", "variables", "self_eval_bound_lookup")]

    assert zero.expected == ExpectedValue(IntValue(0))
    assert bound.bindings == (("x", IntValue(123)),)
    assert bound.declarations[0].type == PrimitiveType("INT64")
    assert bound.expected == ExpectedValue(IntValue(123))


def called_function(expression: str, offset: int) -> str:
    end = offset
    while end < len(expression) and (
        expression[end].isalnum() or expression[end] == "_"
    ):
        end += 1
    return expression[offset:end]


def has_control_character(expression: str, offset: int) -> bool:
    return offset < len(expression) and is_rejected(expression[offset])


def feature_holds(rejection: Rejection, case: Case, outcome: Diagnostic) -> bool:
    if outcome.reason not in FEATURE_REASONS[rejection.feature]:
        return False
    if rejection.feature is Feature.RAW_CONTROL_CHARACTERS:
        return has_control_character(case.expression, outcome.offset)
    if rejection.feature is Feature.QUOTED_FIELDS:
        return "`" in case.expression
    if rejection.feature is Feature.NARROWED_FUNCTIONS:
        return (
            called_function(case.expression, outcome.offset) in CEL_STANDARD_FUNCTIONS
        )
    if rejection.feature is Feature.UNDECLARED_FUNCTIONS:
        name = called_function(case.expression, outcome.offset)
        return bool(name) and name not in CEL_STANDARD_FUNCTIONS
    return True


def test_cel_conformance_cases_parse() -> None:
    cases = [
        case
        for path in vendored_files()
        for case in read_cases(path.stem, path.read_text(encoding="utf-8")).cases
    ]

    outcomes = [(case, checked_syntax(case.expression)) for case in cases]

    wrongly_rejected = [
        case.name
        for case, outcome in outcomes
        if rejection_for(case.file, case.section, case.name) is None
        and isinstance(outcome, Diagnostic)
    ]
    listed = [
        (case, outcome, rejection)
        for case, outcome in outcomes
        if (rejection := rejection_for(case.file, case.section, case.name)) is not None
    ]
    mismatched = [
        case.name
        for case, outcome, rejection in listed
        if not isinstance(outcome, Diagnostic)
        or outcome.reason is not rejection.reason
        or not feature_holds(rejection, case, outcome)
    ]
    used = {
        (rejection.file, rejection.section, rejection.test)
        for _, _, rejection in listed
    }
    assert wrongly_rejected == []
    assert mismatched == []
    assert used == {
        (rejection.file, rejection.section, rejection.test)
        for rejection in KORA_REJECTIONS
    }
    assert len(outcomes) > 1100


def test_cel_conformance_suite(cel_binding: Binding) -> None:
    cases = [
        case
        for path in vendored_files()
        for case in read_cases(path.stem, path.read_text(encoding="utf-8")).cases
        if rejection_for(case.file, case.section, case.name) is None
    ]

    runs = {
        (case.file, case.section, case.name): run_case(cel_binding, case)
        for case in cases
    }

    disagreeing = {
        key: observed for key, (agrees, observed) in runs.items() if not agrees
    }
    recorded = {key: observed for key, (_, observed) in DEVIATIONS.items()}
    assert disagreeing == recorded
    assert len(runs) == len(cases) > 1000
    assert len(runs) - len(disagreeing) > 960
