import errno
import hashlib
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from functools import partial
from typing import Annotated, Final, Literal, NamedTuple, TypeVar

from pydantic import Field, StrictBool, StringConstraints, ValidationError

from kora.executor.digests import object_digest
from kora.ir.json import JsonObjectField
from kora.ir.scalars import Digest
from kora.ir.vcs_ref_json import JsonRefusal, canonical_bytes, decode_strict_json
from kora.protocols.common import BoundaryModel
from kora.protocols.deadlines import (
    NANOSECONDS_PER_SECOND,
    ExecutionDeadline,
    thresholds,
)
from kora.protocols.errors import ERROR_CODE, KoraProtocolError
from kora.protocols.evidence import NormalCompletionQuiescence
from kora.protocols.identity import Count, Identifier, Sha256
from kora.protocols.runtime import (
    MAX_TRANSFER_BYTES,
    CommandObservation,
    RuntimeStatus,
    TransferItem,
    TransferName,
    Workload,
)
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
from kora.runner.registry import ControlFileRef, TransitionExecutor

PRE_ATTACH_BYTES: Final[int] = 8192
MAX_LOG_BYTES: Final[int] = 67_108_864
SECTION_FRAME_BYTES: Final[int] = 65536
MAX_SECTIONS: Final[int] = 16
HEADER_DEPTH: Final[int] = 3
ENVELOPE_BYTES: Final[int] = 8192
ENVELOPE_DEPTH: Final[int] = 4
OBSERVATION_BYTES: Final[int] = 32768
FRAME_PROGRESS_NS: Final[int] = 5 * NANOSECONDS_PER_SECOND
START_PROGRESS_NS: Final[int] = 15 * NANOSECONDS_PER_SECOND
ACCEPTANCE_PROGRESS_NS: Final[int] = 15 * NANOSECONDS_PER_SECOND
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
    "workload_conflict": EnvelopeEntry(
        "the request conflicts with the attempt's recorded binding", False
    ),
    "deadline_expired": EnvelopeEntry(
        "the attempt's inherited deadline has expired", False
    ),
    "submitter_superseded": EnvelopeEntry("the recorded submitter has changed", True),
    "workload_busy": EnvelopeEntry("a workload message is already admitted", True),
    "workload_kind_unavailable": EnvelopeEntry(
        "the held workload cannot be executed as a command", False
    ),
    "protected_paths_unsupported": EnvelopeEntry(
        "the workload requires protected paths, which this runner does not check",
        False,
    ),
    "workspace_failed": EnvelopeEntry(
        "the command workspace could not be prepared", False
    ),
    "execution_unauthorized": EnvelopeEntry(
        "the attached submitter cannot start execution", False
    ),
    "command_not_started": EnvelopeEntry("the command process did not start", False),
    "journal_unavailable": EnvelopeEntry("the command journal is not durable", True),
    "integrity_error": EnvelopeEntry(
        "the transferred bytes do not match the digest", False
    ),
    "transfer_refused": EnvelopeEntry("the input cannot be transferred", False),
    "command_unobserved": EnvelopeEntry(
        "no command observation was recorded for the attempt", False
    ),
    "session_unobserved": EnvelopeEntry(
        "no session observation was sealed for the attempt", False
    ),
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


class AttachRequest(BoundaryModel):
    op: Literal["attach"]
    request_id: Identifier
    dispatcher_id: Identifier


class TransferRequest(BoundaryModel):
    op: Literal["transfer_in"]
    request_id: Identifier
    item: TransferItem


class OutputRequest(BoundaryModel):
    op: Literal["transfer_out"]
    request_id: Identifier
    name: TransferName


LogName = Literal["stdout", "stderr"]
LogOffset = Annotated[Count, Field(le=MAX_LOG_BYTES)]


class LogRequest(BoundaryModel):
    op: Literal["read_log"]
    request_id: Identifier
    which: LogName
    from_offset: LogOffset


class LogSource(BoundaryModel):
    which: LogName
    from_offset: LogOffset


class OutputName(BoundaryModel):
    name: TransferName


class OutputValue(BoundaryModel):
    size: Annotated[Count, Field(le=MAX_TRANSFER_BYTES)]
    sha256: Digest


class OutputEnvelope(BoundaryModel):
    request_id: Identifier
    result: Literal["output"]
    value: OutputValue


class OutputTrailer(BoundaryModel):
    request_id: Identifier
    sha256: Digest


class EmptyValue(BoundaryModel):
    pass


DeliveryResult = Literal["attached", "accepted", "started", "transferred"]


class DeliveryEnvelope(BoundaryModel):
    request_id: Identifier
    result: DeliveryResult
    value: EmptyValue


def workload_digest(workload: Workload) -> str:
    return object_digest(
        workload.model_dump(
            mode="json", exclude={"dispatcher_id", "dispatcher_process"}
        )
    )


def delivery_error(code: str) -> KoraProtocolError:
    message, retryable = ENVELOPE_ERRORS[code]
    return KoraProtocolError(code, message, retryable, {})


def delivery_envelope(request_id: str, result: DeliveryResult) -> bytes:
    return canonical_bytes(
        DeliveryEnvelope(
            request_id=request_id, result=result, value=EmptyValue()
        ).model_dump(mode="json")
    )


def read_delivery(payload: bytes, request_id: str, result: DeliveryResult) -> None:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(DeliveryEnvelope, payload)
    if (
        envelope is None
        or envelope.request_id != request_id
        or envelope.result != result
    ):
        raise message_refused(MessageRefusal.MALFORMED)


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
    *,
    sink: bool = False,
) -> bytearray:
    buffer = bytearray(
        min(declaration.length, SECTION_FRAME_BYTES) if sink else declaration.length
    )
    view = memoryview(buffer)
    digest = hashlib.sha256()
    offset = 0
    while offset < declaration.length:
        deadline_ns = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline_ns)
        frame = (
            view[: min(SECTION_FRAME_BYTES, declaration.length - offset)]
            if sink
            else view[offset : offset + SECTION_FRAME_BYTES]
        )
        size = await channel.read_typed_frame_into(deadline_ns, FrameType.BYTES, frame)
        digest.update(frame[:size])
        offset += size
    if digest.hexdigest() != declaration.sha256:
        raise message_refused(MessageRefusal.INTEGRITY)
    return bytearray() if sink else buffer


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


def message_header(
    operation: SectionedOperation,
    request_id: str,
    sections: Sequence[bytes],
) -> bytes:
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
    return encoded


async def send_message(
    channel: BoundedChannel,
    operation: SectionedOperation,
    request_id: str,
    sections: Sequence[bytes],
    clock: Clock,
    *,
    executor: TransitionExecutor | None = None,
) -> None:
    encode_header = partial(message_header, operation, request_id, sections)
    encoded = encode_header() if executor is None else await executor.run(encode_header)
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


def checked_output_name(name: str) -> str:
    try:
        return OutputName(name=name).name
    except ValidationError:
        raise delivery_error("transfer_refused") from None


def checked_log(which: str, from_offset: int) -> LogSource:
    try:
        return LogSource.model_validate({"which": which, "from_offset": from_offset})
    except ValidationError:
        raise delivery_error("transfer_refused") from None


def output_envelope(request_id: str, size: int, digest: str) -> bytes:
    envelope = OutputEnvelope(
        request_id=request_id,
        result="output",
        value=OutputValue(size=size, sha256=digest),
    )
    return canonical_bytes(envelope.model_dump(mode="json"))


def output_trailer(request_id: str, digest: str) -> bytes:
    return canonical_bytes(
        OutputTrailer(request_id=request_id, sha256=digest).model_dump(mode="json")
    )


def read_output(payload: bytes) -> OutputRequest | None:
    if isinstance(
        decode_strict_json(payload, PRE_ATTACH_BYTES, HEADER_DEPTH), JsonRefusal
    ):
        return None
    request = json_model(OutputRequest, payload)
    return None if request is None or request.op != "transfer_out" else request


def read_log_request(payload: bytes) -> LogRequest | None:
    if isinstance(
        decode_strict_json(payload, PRE_ATTACH_BYTES, HEADER_DEPTH), JsonRefusal
    ):
        return None
    return json_model(LogRequest, payload)


def read_output_declaration(payload: bytes, request_id: str) -> OutputValue:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(OutputEnvelope, payload)
    if (
        envelope is None
        or envelope.request_id != request_id
        or envelope.result != "output"
    ):
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.value


def read_output_trailer(payload: bytes, request_id: str) -> str:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    trailer = json_model(OutputTrailer, payload)
    if trailer is None or trailer.request_id != request_id:
        raise message_refused(MessageRefusal.MALFORMED)
    return trailer.sha256


def read_transfer(payload: bytes) -> TransferRequest | None:
    if isinstance(
        decode_strict_json(payload, PRE_ATTACH_BYTES, HEADER_DEPTH), JsonRefusal
    ):
        return None
    request = json_model(TransferRequest, payload)
    return None if request is None or request.op != "transfer_in" else request


def write_transfer_bytes(descriptor: int, payload: memoryview) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        offset += written


def transfer_cutoff(clock: Clock, deadline: ExecutionDeadline | None) -> int | None:
    if deadline is None:
        return None
    return thresholds(deadline, clock.sample()).interrupt_at_ns


async def read_transfer_bytes(
    channel: BoundedChannel,
    clock: Clock,
    size: int,
    deadline_ns: int,
    write: Callable[[memoryview], Awaitable[None]] | None = None,
) -> str:
    digest = hashlib.sha256()
    remaining = size
    failed: KoraProtocolError | None = None
    while remaining:
        if clock.monotonic_ns() >= deadline_ns:
            raise ChannelError(ChannelFailure.DEADLINE)
        window = min(SECTION_FRAME_BYTES, remaining)
        view = memoryview(bytearray(window))
        frame_deadline = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, deadline_ns)
        received = await channel.read_typed_frame_into(
            frame_deadline, FrameType.BYTES, view
        )
        piece = view[:received]
        if write is not None and failed is None:
            try:
                await write(piece)
            except KoraProtocolError as error:
                if error.code != "journal_unavailable":
                    raise
                failed = error
        digest.update(piece)
        remaining -= received
    if failed is not None:
        raise failed
    return "sha256:" + digest.hexdigest()


def read_request(payload: bytes) -> ControlRequest | AttachRequest | None:
    if isinstance(decode_strict_json(payload, PRE_ATTACH_BYTES, 1), JsonRefusal):
        return None
    attached = json_model(AttachRequest, payload)
    if attached is not None:
        return attached
    request = json_model(ControlRequest, payload)
    return None if request is not None and request.op == "attach" else request


class ObservationEnvelope(BoundaryModel):
    request_id: Identifier
    result: Literal["observed"]
    value: CommandObservation


def observation_envelope(request_id: str, value: CommandObservation) -> bytes:
    envelope = ObservationEnvelope(
        request_id=request_id, result="observed", value=value
    )
    return canonical_bytes(envelope.model_dump(mode="json"))


def read_observation(payload: bytes, request_id: str) -> CommandObservation:
    decoded = decode_strict_json(payload, OBSERVATION_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(ObservationEnvelope, payload)
    if envelope is None or envelope.request_id != request_id:
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.value


class SessionResultEnvelope(BoundaryModel):
    request_id: Identifier
    result: Literal["session_result"]
    value: ControlFileRef


def session_result_envelope(request_id: str, value: ControlFileRef) -> bytes:
    envelope = SessionResultEnvelope(
        request_id=request_id, result="session_result", value=value
    )
    return canonical_bytes(envelope.model_dump(mode="json"))


def read_session_result(payload: bytes, request_id: str) -> ControlFileRef:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(SessionResultEnvelope, payload)
    if envelope is None or envelope.request_id != request_id:
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.value


class QuiescenceEnvelope(BoundaryModel):
    request_id: Identifier
    result: Literal["quiescence"]
    value: NormalCompletionQuiescence


def quiescence_envelope(request_id: str, value: NormalCompletionQuiescence) -> bytes:
    envelope = QuiescenceEnvelope(
        request_id=request_id, result="quiescence", value=value
    )
    return canonical_bytes(envelope.model_dump(mode="json"))


def read_quiescence(payload: bytes, request_id: str) -> NormalCompletionQuiescence:
    decoded = decode_strict_json(payload, ENVELOPE_BYTES, ENVELOPE_DEPTH)
    if isinstance(decoded, JsonRefusal):
        raise message_refused(MessageRefusal.MALFORMED)
    if isinstance(decoded, dict) and "error" in decoded:
        answered, error = read_error_envelope(payload)
        if answered != request_id:
            raise message_refused(MessageRefusal.MALFORMED)
        raise error
    envelope = json_model(QuiescenceEnvelope, payload)
    if envelope is None or envelope.request_id != request_id:
        raise message_refused(MessageRefusal.MALFORMED)
    return envelope.value


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
