from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class Kind(Enum):
    INT = "int"
    UINT = "uint"
    DOUBLE = "double"
    STRING = "string"
    BYTES = "bytes"
    BOOL = "bool"
    NULL = "null_type"
    TIMESTAMP = "google.protobuf.Timestamp"
    DURATION = "google.protobuf.Duration"
    TYPE = "type"


@dataclass(frozen=True)
class Primitive:
    kind: Kind
    allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class Dyn:
    pass


@dataclass(frozen=True)
class ListOf:
    element: "CelType"


@dataclass(frozen=True)
class MapOf:
    key: "CelType"
    value: "CelType"


@dataclass(frozen=True)
class RecordField:
    type: "CelType"
    required: bool


def empty_fields() -> Mapping[str, RecordField]:
    return {}


@dataclass(frozen=True)
class Record:
    fields: Mapping[str, RecordField] = field(default_factory=empty_fields)
    extra: "CelType | None" = field(default_factory=lambda: DYN)


@dataclass(frozen=True)
class Nullable:
    inner: "CelType"


CelType = Primitive | Dyn | ListOf | MapOf | Record | Nullable

INT: Primitive = Primitive(Kind.INT)
UINT: Primitive = Primitive(Kind.UINT)
DOUBLE: Primitive = Primitive(Kind.DOUBLE)
STRING: Primitive = Primitive(Kind.STRING)
BYTES: Primitive = Primitive(Kind.BYTES)
BOOL: Primitive = Primitive(Kind.BOOL)
NULL: Primitive = Primitive(Kind.NULL)
TIMESTAMP: Primitive = Primitive(Kind.TIMESTAMP)
DURATION: Primitive = Primitive(Kind.DURATION)
TYPE: Primitive = Primitive(Kind.TYPE)
DYN: Dyn = Dyn()

SEQUENCE_KINDS: frozenset[Kind] = frozenset({Kind.STRING, Kind.BYTES})


def is_null(cel_type: CelType) -> bool:
    return isinstance(cel_type, Primitive) and cel_type.kind is Kind.NULL


def nullable(cel_type: CelType) -> CelType:
    if isinstance(cel_type, Dyn | Nullable) or is_null(cel_type):
        return cel_type
    return Nullable(cel_type)


def unwrap(cel_type: CelType) -> CelType:
    return cel_type.inner if isinstance(cel_type, Nullable) else cel_type


def is_concatenable(cel_type: CelType) -> bool:
    inner = unwrap(cel_type)
    if isinstance(inner, Primitive):
        return inner.kind in SEQUENCE_KINDS
    return isinstance(inner, ListOf | Dyn)


def member_type(cel_type: CelType, name: str) -> CelType:
    inner = unwrap(cel_type)
    if isinstance(inner, Record):
        declared = inner.fields.get(name)
        if declared is not None:
            return declared.type
        return inner.extra if inner.extra is not None else DYN
    if isinstance(inner, MapOf):
        return inner.value
    return DYN


def element_type(cel_type: CelType) -> CelType:
    inner = unwrap(cel_type)
    if isinstance(inner, ListOf):
        return inner.element
    if isinstance(inner, MapOf):
        return inner.value
    return DYN


def iteration_type(cel_type: CelType) -> CelType:
    inner = unwrap(cel_type)
    if isinstance(inner, ListOf):
        return inner.element
    if isinstance(inner, MapOf):
        return inner.key
    if isinstance(inner, Record):
        return STRING
    return DYN
