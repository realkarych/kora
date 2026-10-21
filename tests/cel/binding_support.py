from collections.abc import Callable
from typing import cast

from kora.cel import CheckedExpression, JsonValue, check
from kora.cel_eval.binding import Binding
from tests.cel.support import accepted

BINDING_ERRORS: tuple[type[Exception], ...] = (
    KeyError,
    TypeError,
    IndexError,
    ZeroDivisionError,
    OverflowError,
    ValueError,
    RuntimeError,
)
RECORD: dict[str, JsonValue] = {
    "b": 2,
    "decision": 3,
    "exit_code": 1,
    "as": 4,
    "if": 5,
}
CONTEXT: dict[str, JsonValue] = {
    "a": dict(RECORD),
    "b": [1, 2, 3],
    "inputs": dict(RECORD),
    "response": dict(RECORD),
    "x_1": 7,
}

Typed = tuple[object, ...]


def typed(value: object) -> Typed:
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        pairs = sorted(((typed(key), typed(item)) for key, item in items), key=repr)
        return ("dict", tuple(pairs))
    if isinstance(value, list):
        return ("list", tuple(typed(item) for item in cast(list[object], value)))
    return (type(value).__name__, repr(value))


def unordered(value: object) -> Typed:
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        pairs = sorted(
            ((unordered(key), unordered(item)) for key, item in items), key=repr
        )
        return ("dict", tuple(pairs))
    if isinstance(value, list):
        members = [unordered(item) for item in cast(list[object], value)]
        return ("list", tuple(sorted(members, key=repr)))
    return (type(value).__name__, repr(value))


def outcome(
    binding: Binding,
    run: Callable[[], object],
    describe: Callable[[object], Typed] = typed,
) -> Typed:
    try:
        result = run()
    except (*BINDING_ERRORS, binding.panic) as error:
        return ("error", type(error).__name__)
    return describe(result)


def checked(source: str) -> CheckedExpression:
    result = check(source)
    assert isinstance(result, CheckedExpression), f"rejected: {source[:80]!r}"
    return result


def evaluated(
    binding: Binding,
    source: str,
    context: dict[str, JsonValue] | None = None,
    describe: Callable[[object], Typed] = typed,
) -> Typed:
    rendering = accepted(source)
    return outcome(
        binding,
        lambda: binding.native_compile(rendering).execute(dict(context or {})),
        describe,
    )


def author_evaluated(
    binding: Binding,
    source: str,
    context: dict[str, JsonValue] | None = None,
    describe: Callable[[object], Typed] = typed,
) -> Typed:
    return outcome(
        binding,
        lambda: binding.native_compile(source).execute(dict(context or {})),
        describe,
    )


def is_value(result: Typed) -> bool:
    return result[0] != "error"
