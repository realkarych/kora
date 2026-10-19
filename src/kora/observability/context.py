import contextlib
import contextvars
from collections.abc import Generator, Mapping

import structlog.contextvars

from kora.observability.catalog import CONTEXT_INTEGER_KEYS, CONTEXT_STRING_KEYS
from kora.observability.policy import CONTEXT_IDENTIFIER as IDENTIFIER


class ContextError(ValueError):
    pass


def validate_context(values: Mapping[str, object]) -> dict[str, str | int]:
    validated: dict[str, str | int] = {}
    for key, value in values.items():
        if key in CONTEXT_STRING_KEYS:
            if not isinstance(value, str) or not IDENTIFIER.match(value):
                raise ContextError(f"{key} must be a bounded printable identifier")
            validated[key] = value
        elif key in CONTEXT_INTEGER_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextError(f"{key} must be a non-negative integer")
            validated[key] = value
        else:
            raise ContextError(f"{key} is not a context key")
    return validated


@contextlib.contextmanager
def bound_scope(**values: str | int) -> Generator[None, None, None]:
    tokens = structlog.contextvars.bind_contextvars(**validate_context(values))
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def current_context() -> dict[str, str | int]:
    return validate_context(structlog.contextvars.get_contextvars())


def copy_context() -> contextvars.Context:
    return contextvars.copy_context()
