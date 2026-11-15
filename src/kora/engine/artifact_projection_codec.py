import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from kora.cel.cost import DATA_DEPTH, Variable
from kora.cel_eval.bounds import MAX_BYTES, MAX_VALUES, admitted, measure
from kora.cel_eval.convert import convert
from kora.cel_eval.errors import ErrorKind
from kora.ir.json import JsonValue
from kora.ir.structured_json import encode_snapshot
from kora.ir.vcs_ref_json import lexical_depth_valid, refuse_constant, unique_object

REPRESENTATION = "kora.artifact-cel-projection.v1"
ENVELOPE_BYTES = 323
MAX_RECORD_BYTES = 2 * MAX_BYTES + ENVELOPE_BYTES
MAX_INTEGER_TOKEN = 310
MAX_FLOAT_TOKEN = 32
CONVERSION_BYTES = MAX_BYTES + MAX_FLOAT_TOKEN * MAX_VALUES
RECORD_VARIABLE_BYTES = 80 * MAX_RECORD_BYTES + 8 * CONVERSION_BYTES
RECORD_ENTRIES = 16 * MAX_RECORD_BYTES + 64 * MAX_VALUES
RECORD_WORK = 64 * MAX_RECORD_BYTES * MAX_RECORD_BYTES
MEMBERS = (
    "format",
    "contract_digest",
    "payload_digest",
    "path_set_digest",
    "original",
    "converted",
)


class ProjectionFailure(StrEnum):
    BINDING = "binding"
    CAPACITY = "capacity"
    ENCODING = "encoding"
    VALUE = "value"
    UNAVAILABLE = "unavailable"
    VALIDATION = "validation"


class ProjectionRejected(ValueError):
    def __init__(self, reason: ProjectionFailure) -> None:
        super().__init__(reason.value)
        self.reason: ProjectionFailure = reason


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    source_bytes: int
    variable_bytes: int
    entries: int
    work: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.source_bytes,
                self.variable_bytes,
                self.entries,
                self.work,
            )
        ):
            raise ProjectionRejected(ProjectionFailure.CAPACITY)


@dataclass(slots=True)
class ProjectionAdmission:
    variable_bytes: int
    entries: int
    work: int

    def reserve(self, variable_bytes: int, entries: int, work: int) -> None:
        if (
            variable_bytes > self.variable_bytes
            or entries > self.entries
            or work > self.work
        ):
            raise ProjectionRejected(ProjectionFailure.CAPACITY)
        self.variable_bytes -= variable_bytes
        self.entries -= entries
        self.work -= work


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionIdentity:
    contract_digest: str
    payload_digest: str
    path_set_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionRecord:
    identity: ProjectionIdentity
    content: bytes = field(repr=False)
    content_digest: str


def digest_valid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def check_identity(identity: ProjectionIdentity) -> None:
    if type(identity) is not ProjectionIdentity or not all(
        digest_valid(value)
        for value in (
            identity.contract_digest,
            identity.payload_digest,
            identity.path_set_digest,
        )
    ):
        raise ProjectionRejected(ProjectionFailure.BINDING)


def checked_conversion(original: JsonValue, view: Variable) -> JsonValue:
    if isinstance(measure(original), ErrorKind):
        raise ProjectionRejected(ProjectionFailure.VALUE)
    converted = convert(original, view.type)
    if (
        isinstance(converted, ErrorKind)
        or isinstance(measure(converted), ErrorKind)
        or not admitted(converted, view.bound)
    ):
        raise ProjectionRejected(ProjectionFailure.VALUE)
    return converted


def encode_record(
    identity: ProjectionIdentity, original: JsonValue, converted: JsonValue
) -> ProjectionRecord:
    check_identity(identity)
    if any(isinstance(measure(value), ErrorKind) for value in (original, converted)):
        raise ProjectionRejected(ProjectionFailure.VALUE)
    header = json.dumps(
        {
            "format": REPRESENTATION,
            "contract_digest": identity.contract_digest,
            "payload_digest": identity.payload_digest,
            "path_set_digest": identity.path_set_digest,
        },
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    content = (
        header[:-1]
        + b',"original":'
        + encode_snapshot(original)
        + b',"converted":'
        + encode_snapshot(converted)
        + b"}"
    )
    if len(content) > MAX_RECORD_BYTES:
        raise ProjectionRejected(ProjectionFailure.CAPACITY)
    return ProjectionRecord(identity, content, hashlib.sha256(content).hexdigest())


def read_integer(token: str) -> int:
    if len(token) > MAX_INTEGER_TOKEN:
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    return int(token)


def read_float(token: str) -> float:
    if len(token) > MAX_FLOAT_TOKEN:
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    value = float(token)
    if not math.isfinite(value):
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    return value


def decode_record(
    content: object,
    identity: ProjectionIdentity,
    content_digest: str,
    content_bytes: int,
    view: Variable,
    admission: ProjectionAdmission,
) -> ProjectionRecord:
    check_identity(identity)
    if (
        type(content) is not bytes
        or type(content_bytes) is not int
        or not 0 < content_bytes <= MAX_RECORD_BYTES
        or len(content) != content_bytes
        or not digest_valid(content_digest)
        or hashlib.sha256(content).hexdigest() != content_digest
    ):
        raise ProjectionRejected(ProjectionFailure.BINDING)
    admission.reserve(RECORD_VARIABLE_BYTES, RECORD_ENTRIES, RECORD_WORK)
    if not lexical_depth_valid(content, DATA_DEPTH + 2):
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    try:
        value: object = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_int=read_integer,
            parse_float=read_float,
            parse_constant=refuse_constant,
        )
    except (ValueError, RecursionError):
        raise ProjectionRejected(ProjectionFailure.ENCODING) from None
    if type(value) is not dict:
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    record = cast(dict[str, JsonValue], value)
    if tuple(record) != MEMBERS or (
        record["format"],
        record["contract_digest"],
        record["payload_digest"],
        record["path_set_digest"],
    ) != (
        REPRESENTATION,
        identity.contract_digest,
        identity.payload_digest,
        identity.path_set_digest,
    ):
        raise ProjectionRejected(ProjectionFailure.BINDING)
    converted = checked_conversion(record["original"], view)
    expected = encode_record(identity, record["original"], converted)
    if expected.content != content:
        raise ProjectionRejected(ProjectionFailure.ENCODING)
    return expected
