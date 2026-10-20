import hashlib
import math
from pathlib import Path

import pytest

from kora.cel import Diagnostic
from tests.cel.conformance.cases import (
    BoolValue,
    BytesValue,
    CaseModelError,
    DoubleValue,
    ExpectedError,
    ExpectedValue,
    IntValue,
    ListValue,
    MapType,
    MapValue,
    NullValue,
    PrimitiveType,
    StringValue,
    read_cases,
    vendored_files,
)
from tests.cel.conformance.manifest import COMMIT, FILES, LICENSE_SHA256
from tests.cel.conformance.selection import (
    EXCLUDED_FILES,
    INCLUDED_FILES,
    KORA_REJECTIONS,
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
    assert len(COMMIT) == 40


def test_cel_conformance_cases_parse() -> None:
    cases = [
        case
        for path in vendored_files()
        for case in read_cases(path.stem, path.read_text(encoding="utf-8")).cases
    ]
    used: set[tuple[str, str, str | None]] = set()

    outcomes = [(case, checked_syntax(case.expression)) for case in cases]

    for case, outcome in outcomes:
        rejection = rejection_for(case.file, case.section, case.name)
        if rejection is None:
            assert not isinstance(outcome, Diagnostic), (
                case.file,
                case.section,
                case.name,
            )
            continue
        used.add((rejection.file, rejection.section, rejection.test))
        assert isinstance(outcome, Diagnostic), (case.file, case.section, case.name)
        assert outcome.reason is rejection.reason, (case.file, case.section, case.name)
    assert used == {
        (rejection.file, rejection.section, rejection.test)
        for rejection in KORA_REJECTIONS
    }
    assert len(outcomes) > 1100
