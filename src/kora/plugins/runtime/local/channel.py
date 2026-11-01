import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Annotated, Final, Literal, NamedTuple, TypeVar

from pydantic import Field, StrictBool, StringConstraints, ValidationError

from kora.ir.json import JsonObjectField
from kora.ir.vcs_ref_json import JsonRefusal, canonical_bytes, decode_strict_json
from kora.protocols.common import BoundaryModel
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import ERROR_CODE, KoraProtocolError
from kora.protocols.identity import Count, Identifier, Sha256
from kora.protocols.runtime import RuntimeStatus
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure

PRE_ATTACH_BYTES: Final[int] = 8192
SECTION_FRAME_BYTES: Final[int] = 65536
MAX_SECTIONS: Final[int] = 16
HEADER_DEPTH: Final[int] = 3
ENVELOPE_BYTES: Final[int] = 8192
ENVELOPE_DEPTH: Final[int] = 4
FRAME_PROGRESS_NS: Final[int] = 5 * NANOSECONDS_PER_SECOND
MESSAGE_PROGRESS_NS: Final[int] = 60 * NANOSECONDS_PER_SECOND
WORKLOAD_SECTION_BYTES: Final[int] = 12 * 1048576
WORKLOAD_SECTION_DEPTH: Final[int] = 18
M = TypeVar("M", bound=BoundaryModel)


class FrameType(IntEnum):
    JSON = ord("J")
    BYTES = ord("B")
    JOURNAL = ord("E")


class MessageRefusal(StrEnum):
    CAPACITY = "capacity"
    MALFORMED = "malformed"
    INTEGRITY = "integrity"
    DEADLINE = "deadline"
    CLOSED = "closed"


class EnvelopeEntry(NamedTuple):
    message: str
    retryable: bool


ENVELOPE_ERRORS: Final[dict[str, EnvelopeEntry]] = {
    "operation_unknown": EnvelopeEntry("the control operation is not known", False),
}
REFUSED_MESSAGE: Final[str] = "the control message was refused"
RETRYABLE_REFUSALS: Final[frozenset[MessageRefusal]] = frozenset(
    {MessageRefusal.DEADLINE, MessageRefusal.CLOSED}
)
CHANNEL_REFUSALS: Final[dict[ChannelFailure, MessageRefusal]] = {
    ChannelFailure.CAPACITY: MessageRefusal.CAPACITY,
    ChannelFailure.MALFORMED: MessageRefusal.MALFORMED,
    ChannelFailure.DEADLINE: MessageRefusal.DEADLINE,
    ChannelFailure.CLOSED: MessageRefusal.CLOSED,
}


def message_refused(reason: MessageRefusal) -> KoraProtocolError:
    return KoraProtocolError(
        "message_refused",
        REFUSED_MESSAGE,
        reason in RETRYABLE_REFUSALS,
        {"reason": reason.value},
    )


def operation_unknown() -> KoraProtocolError:
    message, retryable = ENVELOPE_ERRORS["operation_unknown"]
    return KoraProtocolError("operation_unknown", message, retryable, {})


def allowlisted(
    code: str, retryable: bool, details: Mapping[str, object]
) -> str | None:
    allowed = ENVELOPE_ERRORS.get(code)
    if allowed is None or details or retryable != allowed.retryable:
        return None
    return allowed.message


def json_model(model: type[M], payload: bytes | bytearray) -> M | None:
    try:
        return model.model_validate_json(payload)
    except ValidationError:
        return None


@dataclass(frozen=True)
class SectionSpec:
    name: str
    cap: int
    max_depth: int


@dataclass(frozen=True)
class SectionedOperation:
    op: str
    header_cap: int
    sections: tuple[SectionSpec, ...]


ACCEPT_WORKLOAD: Final[SectionedOperation] = SectionedOperation(
    "accept_workload",
    PRE_ATTACH_BYTES,
    (SectionSpec("workload", WORKLOAD_SECTION_BYTES, WORKLOAD_SECTION_DEPTH),),
)


class SectionDeclaration(BoundaryModel):
    name: Identifier
    length: Count
    sha256: Sha256


class SectionedHeader(BoundaryModel):
    op: Identifier
    request_id: Identifier
    sections: Annotated[tuple[SectionDeclaration, ...], Field(max_length=MAX_SECTIONS)]


@dataclass(frozen=True)
class SectionedMessage:
    request_id: str
    sections: tuple[bytearray, ...]


ErrorMessage = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=256)
]


ErrorCode = Annotated[
    str, StringConstraints(strict=True, pattern=f"^{ERROR_CODE.pattern}$")
]


class ErrorBody(BoundaryModel):
    code: ErrorCode
    message: ErrorMessage
    retryable: StrictBool
    details: JsonObjectField


class ErrorEnvelope(BoundaryModel):
    request_id: Identifier
    error: ErrorBody


class ControlRequest(BoundaryModel):
    op: Identifier
    request_id: Identifier


class StatusValue(BoundaryModel):
    status: RuntimeStatus
    holds_workload: StrictBool


class StatusEnvelope(BoundaryModel):
    request_id: Identifier
    result: Literal["status"]
    value: StatusValue


def parse_header(payload: bytes, operation: SectionedOperation) -> SectionedHeader:
    if isinstance(
        decode_strict_json(payload, operation.header_cap, HEADER_DEPTH), JsonRefusal
    ):
        raise message_refused(MessageRefusal.MALFORMED)
    header = json_model(SectionedHeader, payload)
    if header is None:
        raise message_refused(MessageRefusal.MALFORMED)
    names = tuple(section.name for section in header.sections)
    if header.op != operation.op or names != tuple(
        spec.name for spec in operation.sections
    ):
        raise message_refused(MessageRefusal.MALFORMED)
    for declared, spec in zip(header.sections, operation.sections, strict=True):
        if declared.length > spec.cap:
            raise message_refused(MessageRefusal.CAPACITY)
    return header


async def receive_section(
    channel: BoundedChannel,
    declaration: SectionDeclaration,
    clock: Clock,
    message_deadline_ns: int,
) -> bytearray:
    buffer = bytearray(declaration.length)
    view = memoryview(buffer)
    digest = hashlib.sha256()
    offset = 0
    while offset < declaration.length:
        deadline_ns = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline_ns)
        frame = view[offset : offset + SECTION_FRAME_BYTES]
        size = await channel.read_typed_frame_into(deadline_ns, FrameType.BYTES, frame)
        digest.update(frame[:size])
        offset += size
    if digest.hexdigest() != declaration.sha256:
        raise message_refused(MessageRefusal.INTEGRITY)
    return buffer


async def receive_message(
    channel: BoundedChannel, operation: SectionedOperation, clock: Clock
) -> SectionedMessage:
    started_ns = clock.monotonic_ns()
    message_deadline_ns = started_ns + MESSAGE_PROGRESS_NS
    reservations = {FrameType.JSON.value: operation.header_cap}
    try:
        _, payload = await channel.read_typed_frame(
            started_ns + FRAME_PROGRESS_NS, reservations
        )
        header = parse_header(payload, operation)
        sections = [
            await receive_section(channel, declaration, clock, message_deadline_ns)
            for declaration in header.sections
        ]
    except ChannelError as error:
        if error.reason not in CHANNEL_REFUSALS:
            raise
        channel.close()
        raise message_refused(CHANNEL_REFUSALS[error.reason]) from error
    except KoraProtocolError:
        channel.close()
        raise
    return SectionedMessage(header.request_id, tuple(sections))


async def send_message(
    channel: BoundedChannel,
    operation: SectionedOperation,
    request_id: str,
    sections: Sequence[bytes],
    clock: Clock,
) -> None:
    if len(sections) != len(operation.sections) or any(
        len(section) > spec.cap
        for section, spec in zip(sections, operation.sections, strict=True)
    ):
        raise message_refused(MessageRefusal.CAPACITY)
    header = SectionedHeader(
        op=operation.op,
        request_id=request_id,
        sections=tuple(
            SectionDeclaration(
                name=spec.name,
                length=len(section),
                sha256=hashlib.sha256(section).hexdigest(),
            )
            for section, spec in zip(sections, operation.sections, strict=True)
        ),
    )
    encoded = canonical_bytes(header.model_dump(mode="json"))
    if len(encoded) > operation.header_cap:
        raise message_refused(MessageRefusal.CAPACITY)
    message_deadline_ns = clock.monotonic_ns() + MESSAGE_PROGRESS_NS
    await channel.write_typed_frame(
        FrameType.JSON,
        encoded,
        min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline_ns),
    )
    for section in sections:
        view = memoryview(section)
        for offset in range(0, len(section), SECTION_FRAME_BYTES):
            deadline_ns = min(
                clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline_ns
            )
            await channel.write_typed_frame(
                FrameType.BYTES,
                view[offset : offset + SECTION_FRAME_BYTES],
                deadline_ns,
            )


def decode_section(payload: bytearray, spec: SectionSpec, model: type[M]) -> M:
    if isinstance(decode_strict_json(payload, spec.cap, spec.max_depth), JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    value = json_model(model, payload)
    if value is None:
        raise message_refused(MessageRefusal.MALFORMED)
    return value


def error_envelope(request_id: str, error: KoraProtocolError) -> bytes:
    message = allowlisted(error.code, error.retryable, error.details)
    if message is None:
        raise ValueError("error outside the envelope allowlist")
    envelope = ErrorEnvelope.model_validate(
        {
            "request_id": request_id,
            "error": {
                "code": error.code,
                "message": message,
                "retryable": error.retryable,
                "details": {},
            },
        }
    )
    return canonical_bytes(envelope.model_dump(mode="json"))


def read_error_envelope(payload: bytes) -> tuple[str, KoraProtocolError]:
    if isinstance(
        decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH), JsonRefusal
    ):
        raise message_refused(MessageRefusal.MALFORMED)
    envelope = json_model(ErrorEnvelope, payload)
    if envelope is None:
        raise message_refused(MessageRefusal.MALFORMED)
    body = envelope.error
    if allowlisted(body.code, body.retryable, body.details) != body.message:
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.request_id, KoraProtocolError(
        body.code, body.message, body.retryable, {}
    )


def control_request(op: str, request_id: str) -> bytes:
    request = ControlRequest(op=op, request_id=request_id)
    return canonical_bytes(request.model_dump(mode="json"))


def read_request(payload: bytes) -> ControlRequest | None:
    if isinstance(decode_strict_json(payload, PRE_ATTACH_BYTES, 1), JsonRefusal):
        return None
    return json_model(ControlRequest, payload)


def status_envelope(request_id: str, value: StatusValue) -> bytes:
    envelope = StatusEnvelope(request_id=request_id, result="status", value=value)
    return canonical_bytes(envelope.model_dump(mode="json"))


def read_status(payload: bytes, request_id: str) -> StatusValue:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(StatusEnvelope, payload)
    if envelope is None or envelope.request_id != request_id:
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.value
