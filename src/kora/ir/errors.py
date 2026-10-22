from dataclasses import dataclass

from pydantic import ValidationError

PLACEHOLDER_LOCATION: str = "<field>"


@dataclass(frozen=True)
class SafeFieldError:
    loc: tuple[str | int, ...]
    type: str
    msg: str


def safe_location(
    location: tuple[str | int, ...], known: frozenset[str]
) -> tuple[str | int, ...]:
    return tuple(
        part if isinstance(part, int) or part in known else PLACEHOLDER_LOCATION
        for part in location
    )


def safe_field_errors(
    error: ValidationError, known: frozenset[str], message: str
) -> tuple[SafeFieldError, ...]:
    return tuple(
        SafeFieldError(safe_location(item["loc"], known), item["type"], message)
        for item in error.errors(include_input=False, include_context=False)
    )
