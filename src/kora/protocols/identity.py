import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Final, Protocol, cast

from pydantic import (
    MISSING,
    AfterValidator,
    BaseModel,
    BeforeValidator,
    PlainSerializer,
    StringConstraints,
)
from pydantic_core import PydanticCustomError

from kora.ir.json import JsonObjectField
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import (
    MAX_COUNT as MAX_COUNT,
)
from kora.protocols.identity_records import (
    MAX_PID as MAX_PID,
)
from kora.protocols.identity_records import (
    Count as Count,
)
from kora.protocols.identity_records import (
    Identifier as Identifier,
)
from kora.protocols.identity_records import (
    Pid as Pid,
)
from kora.protocols.identity_records import (
    ProcessIdentity as ProcessIdentity,
)

Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


def instant_refused() -> PydanticCustomError:
    return PydanticCustomError("instant_invalid", "the timestamp is invalid")


def instant_input(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise instant_refused() from None
    raise instant_refused()


def utc_instant(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise instant_refused()
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise instant_refused() from None


def instant_wire(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


Instant = Annotated[
    datetime,
    BeforeValidator(instant_input),
    AfterValidator(utc_instant),
    PlainSerializer(instant_wire, return_type=str, when_used="json"),
]


class ProcessIdentitySource(Protocol):
    def current(self) -> ProcessIdentity: ...


class GrantBinding(BoundaryModel):
    dispatcher_id: Identifier
    worker_id: Identifier
    incarnation_id: Identifier
    lease_epoch: Count
    execution_domain_id: Identifier
    process: ProcessIdentity | None


class Handle(BoundaryModel):
    runtime: Identifier
    attempt_id: Identifier
    execution_domain_id: Identifier
    host_hint: Identifier | None
    data: JsonObjectField


NULL_SIZE: Final[int] = 4
INSTANT_SIZE: Final[int] = 29


def text_size(value: str | None) -> int:
    if value is None:
        return NULL_SIZE
    if value.isascii() and value.isprintable():
        return len(value) + 2 + value.count('"') + value.count("\\")
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def integer_size(value: int | None) -> int:
    return NULL_SIZE if value is None else len(str(value))


def boolean_size(value: bool) -> int:
    return 4 if value else 5


def object_size(members: Mapping[str, int]) -> int:
    return (
        2
        + max(0, len(members) - 1)
        + sum(len(name) + 3 + size for name, size in members.items())
    )


def array_size(item_sizes: Iterable[int]) -> int:
    sizes = list(item_sizes)
    return 2 + max(0, len(sizes) - 1) + sum(sizes)


def mapping_size(value: Mapping[object, object], sections: frozenset[str]) -> int:
    members = [
        text_size(str(key)) + 1 + encoded_size(item, sections)
        for key, item in value.items()
    ]
    return 2 + max(0, len(members) - 1) + sum(members)


def model_size(value: BaseModel, sections: frozenset[str]) -> int:
    owner = type(value).__name__
    return object_size(
        {
            name: NULL_SIZE
            if f"{owner}.{name}" in sections
            else encoded_size(getattr(value, name), sections)
            for name in type(value).model_fields
            if getattr(value, name) is not MISSING
        }
    )


def encoded_size(value: object, sections: frozenset[str] = frozenset()) -> int:
    if value is None:
        return NULL_SIZE
    if isinstance(value, bool):
        return boolean_size(value)
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        return len(json.dumps(value))
    if isinstance(value, str):
        return text_size(value)
    if isinstance(value, datetime):
        return INSTANT_SIZE
    if isinstance(value, BaseModel):
        return model_size(value, sections)
    if isinstance(value, Mapping):
        return mapping_size(cast("Mapping[object, object]", value), sections)
    if isinstance(value, list | tuple):
        items = cast("Sequence[object]", value)
        return array_size(encoded_size(item, sections) for item in items)
    raise TypeError("the value has no canonical JSON form")
