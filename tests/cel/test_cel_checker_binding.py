import pytest

from kora.cel import CheckedExpression, Environment, ResultKind, check_typed
from kora.cel.checker import ARITHMETIC, ORDERED, SIGNATURES, ordered
from kora.cel.functions import Form, lookup
from kora.cel.types import Kind, Primitive
from kora.cel_eval.binding import Binding
from tests.cel.binding_support import evaluated

FIRST: dict[Kind, str] = {
    Kind.INT: "7",
    Kind.UINT: "7u",
    Kind.DOUBLE: "7.5",
    Kind.STRING: "'abc'",
    Kind.BYTES: "b'ab'",
    Kind.BOOL: "true",
    Kind.TIMESTAMP: "timestamp('2026-10-27T10:00:00Z')",
    Kind.DURATION: "duration('3600s')",
}
SECOND: dict[Kind, str] = {
    Kind.INT: "2",
    Kind.UINT: "2u",
    Kind.DOUBLE: "2.5",
    Kind.STRING: "'a'",
    Kind.BYTES: "b'a'",
    Kind.BOOL: "false",
    Kind.TIMESTAMP: "timestamp('2026-10-27T09:00:00Z')",
    Kind.DURATION: "duration('60s')",
}
PARSED_STRINGS: dict[str, str] = {
    "int": "'7'",
    "uint": "'7'",
    "double": "'7.5'",
    "duration": "'60s'",
    "timestamp": "'2026-10-27T10:00:00Z'",
}


def call_source(name: str, kinds: tuple[Kind, ...]) -> str:
    samples = [FIRST[kind] for kind in kinds]
    if kinds[0] is Kind.STRING and name in PARSED_STRINGS:
        samples[0] = PARSED_STRINGS[name]
    if len(kinds) == 2:
        samples[1] = SECOND[kinds[1]]
    if lookup(name, len(kinds), Form.GLOBAL) is not None:
        return f"{name}({', '.join(samples)})"
    return f"{samples[0]}.{name}({', '.join(samples[1:])})"


def overload_cases() -> list[str]:
    calls = [
        call_source(name, kinds)
        for name, signatures in SIGNATURES.items()
        for kinds, _ in signatures
    ]
    operators = [
        f"({FIRST[left]} {operator.value} {SECOND[right]})"
        for operator, entries in ARITHMETIC.items()
        for left, right, _ in entries
    ]
    equal = [f"{expression} == {expression}" for expression in calls + operators]
    orderings = [
        f"({FIRST[left]} < {SECOND[right]}) == ({FIRST[left]} < {SECOND[right]})"
        for left in sorted(ORDERED, key=lambda kind: kind.value)
        for right in sorted(ORDERED, key=lambda kind: kind.value)
        if ordered(Primitive(left), Primitive(right))
    ]
    sizes = [
        "size([1]) == [1].size()",
        "size({'a': 1}) == {'a': 1}.size()",
        "size(b'ab') == b'ab'.size()",
    ]
    return equal + orderings + sizes


def accepted_by_checker(source: str) -> str:
    result = check_typed(source, Environment({}, ResultKind.BOOL))
    assert isinstance(result, CheckedExpression), f"{result}: {source}"
    return source


@pytest.mark.parametrize("source", overload_cases())
def test_cel_checker_overloads_evaluate_in_binding(
    cel_binding: Binding, source: str
) -> None:
    accepted = accepted_by_checker(source)

    result = evaluated(cel_binding, accepted)

    assert result == ("bool", "True")
