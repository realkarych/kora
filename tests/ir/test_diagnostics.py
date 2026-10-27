import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from kora.cel import Diagnostic as CelDiagnostic
from kora.cel import check
from kora.ir import ConfigWarning, ContractViolation, Diagnostic, SourceLocation
from kora.ir.diagnostics import from_cel
from tests.ir.support import raw_error_forms

MAX_INTEGER: int = 9_007_199_254_740_991
BASE: dict[str, object] = {"code": "provider.reason-1", "message": "Invalid value."}
MARKER: str = "private-payload-marker"


def parse(document: dict[str, object], wire: bool) -> Diagnostic:
    if wire:
        return Diagnostic.model_validate_json(json.dumps(document))
    return Diagnostic.model_validate(document)


@pytest.mark.parametrize(
    ("path", "exact", "expected"),
    [
        (("nodes", "review", "condition"), True, 9),
        (("nodes",), False, None),
        (None, True, None),
        ((), True, 9),
    ],
)
def test_shared_diagnostics_preserve_source_and_expression_offsets(
    path: tuple[str | int, ...] | None, exact: bool, expected: int | None
) -> None:
    parsed = check("'😀' == x @")
    assert isinstance(parsed, CelDiagnostic)
    source = SourceLocation(file="graphs/review.py", line=19)

    converted = from_cel(
        parsed,
        path=path,
        exact_expression_field=exact,
        location=source,
        node="review",
        hint="Check the expression.",
    )
    restored = Diagnostic.model_validate_json(converted.model_dump_json())

    assert restored.code == "IR011"
    assert restored.message == parsed.message
    assert restored.offset == expected
    assert restored.path == path
    assert restored.location == source
    assert restored.node == "review"
    assert restored.hint == "Check the expression."


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("code", ["X", "provider.new_code-2", "A" * 64])
def test_shared_diagnostic_codes_are_extensible(code: str, wire: bool) -> None:
    document = {**BASE, "code": code}

    value = parse(document, wire)
    rendered = value.model_dump(mode="json")

    assert rendered == {
        "code": code,
        "message": "Invalid value.",
        "path": None,
        "location": None,
        "offset": None,
        "node": None,
        "hint": None,
    }
    assert ConfigWarning is Diagnostic
    assert ContractViolation is Diagnostic


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "1bad"),
        ("code", "X\n"),
        ("code", "é"),
        ("code", ""),
        ("code", "A" * 65),
        ("code", "a/b"),
        ("path", ["a"] * 65),
        ("path", ["😀" * 64 + "a"]),
        ("path", [True]),
        ("path", [-1]),
        ("path", [2**53]),
        ("path", [0.0]),
        ("path", "nodes"),
        ("path", {"nodes": 1}),
        ("offset", -1),
        ("offset", True),
        ("offset", 1.0),
        ("offset", "1"),
        ("offset", 2**53),
        ("message", "a" * 1025),
        ("message", "😀" * 256 + "a"),
        ("node", "a" * 257),
        ("node", "😀" * 64 + "a"),
        ("hint", "a" * 513),
        ("hint", "😀" * 128 + "a"),
        ("location", {"file": "graph.py", "line": 0}),
        ("location", {"file": "graph.py", "line": -1}),
        ("location", {"file": "graph.py", "line": True}),
        ("location", {"file": "graph.py", "line": 1.0}),
        ("location", {"file": "graph.py", "line": "1"}),
        ("location", {"file": "graph.py", "line": 2**53}),
        ("location", {"file": "a" * 1025, "line": 1}),
        ("location", {"file": "😀" * 256 + "a", "line": 1}),
        ("location", {"file": "/private/graph.py", "line": 1}),
        ("location", {"file": "//host/graph.py", "line": 1}),
        ("location", {"file": "https://host/graph.py", "line": 1}),
        ("location", {"file": "file:///private/graph.py", "line": 1}),
    ],
)
def test_shared_diagnostic_bounds_refuse_invalid_values(
    field: str, value: object, wire: bool
) -> None:
    document: dict[str, object] = {**BASE, "path": [], field: value}

    with pytest.raises(ValidationError) as refused:
        parse(document, wire)

    assert refused.value.error_count() >= 1


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("unit", ["a", "😀", "\n"])
def test_shared_diagnostic_bounds(unit: str, wire: bool) -> None:
    width = len(unit.encode())
    document = {
        **BASE,
        "message": unit * (1024 // width),
        "path": [unit * (256 // width)] * 62 + ["0", MAX_INTEGER],
        "node": unit * (256 // width),
        "hint": unit * (512 // width),
        "offset": MAX_INTEGER,
        "location": {"file": unit * (1024 // width), "line": MAX_INTEGER},
    }

    value = parse(document, wire)
    restored = Diagnostic.model_validate(value.model_dump(mode="json"))

    assert restored.model_dump(mode="json") == document
    assert restored.path is not None
    assert restored.path[-2:] == ("0", MAX_INTEGER)


def test_shared_diagnostic_path_is_an_immutable_snapshot() -> None:
    path: list[str | int] = ["nodes", "0", 0]

    value = Diagnostic.model_validate({**BASE, "path": path})
    path[0] = "replaced"

    assert value.path == ("nodes", "0", 0)


@pytest.mark.parametrize("path", [None, []])
def test_shared_diagnostic_null_and_root_paths_remain_distinct(
    path: list[str] | None,
) -> None:
    document = {**BASE, "path": path, "message": "", "hint": "", "node": ""}

    value = Diagnostic.model_validate(document)

    assert value.model_dump(mode="json")["path"] == path
    assert value.message == ""
    assert value.hint == ""
    assert value.node == ""


def test_shared_diagnostic_offset_requires_a_path() -> None:
    document = {**BASE, "offset": 0}

    with pytest.raises(ValidationError) as refused:
        Diagnostic.model_validate(document)

    assert refused.value.error_count() == 1


@pytest.mark.parametrize("factory", [lambda: {"a"}, lambda: iter(["a"])])
def test_shared_diagnostic_path_refuses_arbitrary_iterables(
    factory: Callable[[], object],
) -> None:
    document = {**BASE, "path": factory()}

    with pytest.raises(ValidationError) as refused:
        Diagnostic.model_validate(document)

    assert refused.value.error_count() == 1


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "code",
        "message",
        "path",
        "location",
        "node",
        "hint",
        "extra",
        "nested_extra",
        "file",
    ],
)
def test_shared_diagnostics_exclude_input_content(field: str, wire: bool) -> None:
    value: object = {MARKER: MARKER}
    document = dict(BASE)
    if field == "extra":
        document[MARKER] = MARKER
    elif field == "nested_extra":
        document["location"] = {"file": "graph.py", "line": 1, MARKER: MARKER}
    elif field == "file":
        document["location"] = {"file": f"https://{MARKER}/graph.py", "line": 1}
    else:
        document[field] = value

    with pytest.raises(ValidationError) as refused:
        parse(document, wire)
    rendered = raw_error_forms(refused.value)

    assert MARKER not in rendered


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("codepoint", [0xD800, 0xDFFF])
@pytest.mark.parametrize("field", ["code", "message", "node", "hint", "path", "file"])
def test_shared_diagnostics_refuse_lone_surrogates(
    field: str,
    codepoint: int,
    wire: bool,
) -> None:
    surrogate = chr(codepoint)
    document = dict(BASE)
    if field == "path":
        document["path"] = [surrogate]
    elif field == "file":
        document["location"] = {"file": surrogate, "line": 1}
    else:
        document[field] = surrogate

    with pytest.raises(ValidationError) as refused:
        parse(document, wire)

    assert refused.value.error_count() >= 1
