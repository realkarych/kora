import json
from collections.abc import Iterable
from dataclasses import asdict
from typing import Literal, cast

import pytest

from kora.ir import (
    Diagnostic,
    DiagnosticCollection,
    DiagnosticCollectionError,
    DiagnosticCollector,
)
from kora.ir.diagnostic_collection import (
    EMPTY_BYTES,
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTICS,
    Severity,
    increment_count,
)
from kora.ir.diagnostics import MAX_INTEGER, SourceLocation

ERROR: Literal["error"] = "error"
WARNING: Literal["warning"] = "warning"


def repeated(
    value: Diagnostic, severity: Severity, count: int
) -> list[tuple[Diagnostic, Severity]]:
    entry: tuple[Diagnostic, Severity] = (value, severity)
    return [entry] * count


def encoded(errors: Iterable[Diagnostic], warnings: Iterable[Diagnostic]) -> bytes:
    return json.dumps(
        {
            "errors": [item.model_dump(mode="json") for item in errors],
            "warnings": [item.model_dump(mode="json") for item in warnings],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def ordering(item: Diagnostic) -> tuple[object, ...]:
    path = (
        ()
        if item.path is None
        else tuple(
            (0, part) if isinstance(part, int) else (1, part) for part in item.path
        )
    )
    return (
        item.path is not None,
        path,
        item.offset is not None,
        item.offset or 0,
        item.code,
        item.location is not None,
        "" if item.location is None else item.location.file,
        0 if item.location is None else item.location.line,
        item.node is not None,
        item.node or "",
        item.message,
        item.hint is not None,
        item.hint or "",
    )


def oracle(items: list[tuple[Diagnostic, Severity]]) -> DiagnosticCollection:
    groups = [
        [item for item, severity in items if severity == wanted]
        for wanted in (ERROR, WARNING)
    ]
    selected: list[list[Diagnostic]] = [[], []]
    full = False
    for index, group in enumerate(groups):
        candidates = sorted(group, key=ordering)[:MAX_DIAGNOSTICS]
        for candidate in candidates:
            proposed = [*selected[index], candidate]
            errors = proposed if index == 0 else selected[0]
            warnings = [] if index == 0 else proposed
            if full or len(encoded(errors, warnings)) > MAX_DIAGNOSTIC_BYTES:
                full = True
                break
            selected[index].append(candidate)
    return DiagnosticCollection(
        tuple(selected[0]),
        tuple(selected[1]),
        len(groups[0]) - len(selected[0]),
        len(groups[1]) - len(selected[1]),
    )


def collect(items: list[tuple[Diagnostic, Severity]]) -> DiagnosticCollection:
    collector = DiagnosticCollector()
    for value, severity in items:
        collector.add(value, severity)
        for prefix in (collector.errors, collector.warnings):
            assert len(prefix.entries) <= MAX_DIAGNOSTICS
            assert (
                EMPTY_BYTES + prefix.weight - bool(prefix.entries)
                <= MAX_DIAGNOSTIC_BYTES
            )
    return collector.finish()


@pytest.mark.parametrize("reverse", [False, True])
def test_diagnostic_collections_are_bounded_and_count_omissions(reverse: bool) -> None:
    items: list[tuple[Diagnostic, Severity]] = [
        (Diagnostic(code="IR002", message="Missing.", path=("nodes", index)), severity)
        for severity in (ERROR, WARNING)
        for index in range(1_050)
    ]
    if reverse:
        items.reverse()

    result = collect(items)

    assert result == oracle(items)
    assert len(result.errors) == len(result.warnings) == 1_000
    assert result.errors_omitted == result.warnings_omitted == 50
    assert not result.valid


@pytest.mark.parametrize("unit", ["a", "é", "😀", "\n", '"', "\\"])
@pytest.mark.parametrize("reverse", [False, True])
def test_collection_byte_prefix_never_skips_an_earlier_item(
    unit: str, reverse: bool
) -> None:
    large = Diagnostic(code="A", message="", path=("a",) + (unit * 64,) * 63)
    later = Diagnostic(code="A", message="", path=("z",))
    items: list[tuple[Diagnostic, Severity]] = (
        repeated(large, ERROR, 400)
        + repeated(later, ERROR, 100)
        + repeated(later, WARNING, 100)
    )
    if reverse:
        items.reverse()

    result = collect(items)

    assert result == oracle(items)
    assert not result.warnings
    assert later not in result.errors
    assert result.errors_omitted > 100
    assert len(encoded(result.errors, result.warnings)) <= MAX_DIAGNOSTIC_BYTES


def exact_budget_items() -> list[tuple[Diagnostic, Severity]]:
    base = Diagnostic(code="A", message="", path=("a" * 256,) * 64)
    size = len(encoded([base], [])) - EMPTY_BYTES
    count = (MAX_DIAGNOSTIC_BYTES - EMPTY_BYTES) // (size + 1)
    needed = MAX_DIAGNOSTIC_BYTES - EMPTY_BYTES - count * (size + 1)
    if (
        needed
        < len(encoded([Diagnostic(code="A", message="", path=("b",))], []))
        - EMPTY_BYTES
    ):
        count -= 1
        needed += size + 1
    for length in range(64):
        path = ("b",) + ("b" * 256,) * length
        empty = Diagnostic(code="A", message="", path=path)
        fill = needed - (len(encoded([empty], [])) - EMPTY_BYTES)
        if 0 <= fill <= 1023:
            last = Diagnostic(code="A", message="x" * fill, path=path)
            return repeated(base, ERROR, count) + repeated(last, ERROR, 1)
    raise AssertionError("No exact diagnostic budget fixture.")


def test_collection_exact_byte_limit_and_one_byte_over() -> None:
    exact = exact_budget_items()
    last = exact[-1][0]
    over = [
        *exact[:-1],
        (
            Diagnostic(code=last.code, message=last.message + "x", path=last.path),
            ERROR,
        ),
    ]

    admitted = collect(exact)
    refused = collect(cast("list[tuple[Diagnostic, Severity]]", over))

    assert len(encoded(admitted.errors, admitted.warnings)) == MAX_DIAGNOSTIC_BYTES
    assert admitted.errors_omitted == 0
    assert refused.errors == admitted.errors[:-1]
    assert refused.errors_omitted == 1


def test_count_exclusions_do_not_block_warnings() -> None:
    value = Diagnostic(code="A", message="")
    items = repeated(value, ERROR, 1_001) + repeated(value, WARNING, 1)

    result = collect(items)

    assert len(result.errors) == 1_000
    assert result.errors_omitted == 1
    assert result.warnings == (value,)


def test_combined_byte_budget_prioritizes_errors_over_warnings() -> None:
    value = Diagnostic(code="A", message="", path=("a" * 256,) * 64)
    items = repeated(value, WARNING, 50) + repeated(value, ERROR, 50)

    result = collect(items)

    assert result == oracle(items)
    assert len(result.errors) == 50
    assert 0 < len(result.warnings) < 50


def test_ordering_preserves_nulls_segment_kinds_and_metadata() -> None:
    paths: list[tuple[str | int, ...] | None] = [
        None,
        (),
        (0,),
        ("0",),
        ("a",),
        ("a", 1),
    ]
    values = [Diagnostic(code="A", message="m", path=path) for path in paths]
    values.extend(
        [
            Diagnostic(code="A", message="m", path=("a",), offset=0),
            Diagnostic(
                code="A",
                message="m",
                path=("a",),
                location=SourceLocation(file="a.py", line=2),
            ),
            Diagnostic(code="A", message="m", path=("a",), node="n"),
            Diagnostic(code="A", message="m", path=("a",), hint="h"),
        ]
    )
    items: list[tuple[Diagnostic, Severity]] = [
        (value, ERROR) for value in reversed(values)
    ]

    result = collect(items)

    assert result == oracle(items)
    assert [item.path for item in result.errors[:4]] == paths[:4]


@pytest.mark.parametrize("field", ["message", "path", "location", "offset"])
def test_collection_refuses_forged_diagnostics_without_retained_exception(
    field: str,
) -> None:
    bad: dict[str, object] = {
        "message": "private-marker" * 100,
        "path": ("x",) * 65,
        "location": SourceLocation.model_construct(file="/private-marker", line=1),
        "offset": True,
    }
    forged = Diagnostic(code="A", message="").model_copy(update={field: bad[field]})
    collector = DiagnosticCollector()

    with pytest.raises(ValueError, match="^Diagnostic fields are invalid.$") as refused:
        collector.add(forged)

    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert collector.finish() == DiagnosticCollection((), (), 0, 0)


def test_collection_copies_nested_metadata_and_preserves_omitted_error_failure() -> (
    None
):
    source = SourceLocation(file="graph.py", line=1)
    value = Diagnostic(code="A", message="m", location=source)

    result = collect([(value, ERROR)])

    assert result.errors[0] is not value
    assert result.errors[0].location is not source
    assert not DiagnosticCollection((), (), 1, 0).valid
    assert DiagnosticCollection((), (value,), 0, 1).valid
    assert asdict(result)["errors_omitted"] == 0


def test_collection_count_overflow_is_content_free() -> None:
    accepted = increment_count(MAX_INTEGER - 1)

    with pytest.raises(DiagnosticCollectionError) as refused:
        increment_count(accepted)

    assert refused.value.kind == "count_overflow"
    assert str(refused.value) == "Diagnostic count exceeds the supported range."
