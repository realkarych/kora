import asyncio
import hashlib
import json
import socket
import struct

import pytest

from kora.ir.json import JsonObjectField
from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    FRAME_PROGRESS_NS,
    MESSAGE_PROGRESS_NS,
    PRE_ATTACH_BYTES,
    SECTION_FRAME_BYTES,
    WORKLOAD_SECTION_BYTES,
    FrameType,
    MessageRefusal,
    SectionedHeader,
    SectionedOperation,
    SectionSpec,
    StatusValue,
    control_request,
    decode_section,
    error_envelope,
    message_refused,
    operation_unknown,
    read_error_envelope,
    read_request,
    read_status,
    receive_message,
    send_message,
    status_envelope,
)
from kora.protocols.common import BoundaryModel
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import RuntimeStatus, Workload
from kora.protocols.time import SystemClock
from kora.runner.channels import (
    MAX_TYPED_FRAME_BYTES,
    BoundedChannel,
    ChannelError,
    ChannelFailure,
)
from tests.measurements.runtime_workload import maximal_workload
from tests.plugins.runtime.local.frames_support import (
    StepClock,
    TickingClock,
    bytes_frames,
    channel_pair,
    frame,
    header,
    refused_peak,
    send_raw,
)
from tests.protocols.runtime_documents import workload

pytestmark = pytest.mark.asyncio

ALLOCATION_CEILING: int = 65536
PAIR = SectionedOperation(
    "pair", 1024, (SectionSpec("first", 64, 4), SectionSpec("second", 64, 4))
)
FIRST: bytes = b'{"value":1}'
SECOND: bytes = b'{"value":2}'


class Loose(BoundaryModel):
    value: float
    nested: JsonObjectField


def far_deadline() -> int:
    return SystemClock().monotonic_ns() + 10 * FRAME_PROGRESS_NS


async def test_typed_frame_is_read_within_its_reservation() -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(pair.raw, frame(FrameType.JSON, b"x" * PRE_ATTACH_BYTES))

        kind, payload = await pair.receiver.read_typed_frame(
            far_deadline(), {FrameType.JSON: PRE_ATTACH_BYTES}
        )

        assert (kind, len(payload)) == (FrameType.JSON, PRE_ATTACH_BYTES)
        assert not pair.receiver.closed


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (frame(ord("X"), b"{}"), ChannelFailure.MALFORMED),
        (frame(FrameType.BYTES, b"{}"), ChannelFailure.MALFORMED),
        (frame(FrameType.JSON, b"x" * (PRE_ATTACH_BYTES + 1)), ChannelFailure.CAPACITY),
        (struct.pack("!IB", 2**32 - 1, FrameType.JSON), ChannelFailure.CAPACITY),
    ],
    ids=["unknown-type", "unexpected-type", "pre-attach-cap", "length-cap"],
)
async def test_refused_frame_closes_before_its_payload_is_allocated(
    raw: bytes, reason: ChannelFailure
) -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(pair.raw, raw)

        refused, peak = await refused_peak(
            pair.receiver.read_typed_frame(
                far_deadline(), {FrameType.JSON: PRE_ATTACH_BYTES}
            ),
            ChannelError,
        )

        assert refused.reason is reason
        assert peak < ALLOCATION_CEILING
        assert pair.receiver.closed


async def test_frame_reservation_never_exceeds_the_frame_cap() -> None:
    async with channel_pair(SystemClock()) as pair:
        with pytest.raises(ChannelError) as refused:
            await pair.receiver.read_typed_frame(
                far_deadline(), {FrameType.JSON: MAX_TYPED_FRAME_BYTES + 1}
            )

        assert refused.value.reason is ChannelFailure.CAPACITY


async def test_truncated_frame_closes_the_connection() -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(pair.raw, struct.pack("!IB", 100, FrameType.JSON) + b"x" * 50)
        pair.raw.shutdown(socket.SHUT_WR)

        refused, peak = await refused_peak(
            pair.receiver.read_typed_frame(
                far_deadline(), {FrameType.JSON: PRE_ATTACH_BYTES}
            ),
            ChannelError,
        )

        assert refused.reason is ChannelFailure.CLOSED
        assert peak < ALLOCATION_CEILING
        assert pair.receiver.closed


DECLARED: str = json.dumps(
    [
        {"name": name, "length": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in (("first", FIRST), ("second", SECOND))
    ],
    separators=(",", ":"),
)


@pytest.mark.parametrize(
    "document",
    [
        '{"op":"pair","request_id":"r","request_id":"r","sections":' + DECLARED + "}",
        '{"op":"pair","request_id":"r","sections":'
        + DECLARED.replace('"name":"first"', '"name":"first","name":"first"')
        + "}",
    ],
    ids=["duplicate-member", "duplicate-declaration-member"],
)
async def test_header_must_be_strict_json(document: str) -> None:
    SectionedHeader.model_validate_json(document)
    async with channel_pair(SystemClock()) as pair:
        await send_raw(pair.raw, frame(FrameType.JSON, document.encode()))

        refusal, _ = await refused_peak(
            receive_message(pair.receiver, PAIR, SystemClock())
        )

        assert refusal.details == {"reason": MessageRefusal.MALFORMED.value}
        assert pair.receiver.closed


async def test_missing_header_is_refused_at_its_deadline() -> None:
    clock = StepClock()
    async with channel_pair(clock) as pair:
        receiving = asyncio.create_task(receive_message(pair.receiver, PAIR, clock))
        await clock.waiting.wait()

        clock.advance(FRAME_PROGRESS_NS)
        refusal, _ = await refused_peak(receiving)

        assert refusal.details == {"reason": MessageRefusal.DEADLINE.value}
        assert clock.offset_ns == FRAME_PROGRESS_NS
        assert pair.receiver.closed


async def test_section_ending_with_the_connection_is_refused() -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(
            pair.raw,
            header("pair", [("first", FIRST), ("second", SECOND)])
            + struct.pack("!IB", len(FIRST), FrameType.BYTES)
            + FIRST[:4],
        )
        pair.raw.shutdown(socket.SHUT_WR)

        refusal, _ = await refused_peak(
            receive_message(pair.receiver, PAIR, SystemClock())
        )

        assert refusal.details == {"reason": MessageRefusal.CLOSED.value}
        assert refusal.retryable
        assert pair.receiver.closed


async def test_header_over_its_operation_cap_is_refused() -> None:
    async with channel_pair(SystemClock()) as pair:
        padding = "r" * ACCEPT_WORKLOAD.header_cap
        oversized = json.dumps({"op": "accept_workload", "request_id": padding})
        await send_raw(pair.raw, frame(FrameType.JSON, oversized.encode()))

        refusal, peak = await refused_peak(
            receive_message(pair.receiver, ACCEPT_WORKLOAD, SystemClock())
        )

        assert refusal.details == {"reason": MessageRefusal.CAPACITY.value}
        assert peak < ALLOCATION_CEILING
        assert pair.receiver.closed


async def test_section_declaration_over_its_cap_is_refused_before_allocation() -> None:
    async with channel_pair(SystemClock()) as pair:
        document = {
            "op": ACCEPT_WORKLOAD.op,
            "request_id": "request-1",
            "sections": [
                {
                    "name": "workload",
                    "length": WORKLOAD_SECTION_BYTES + 1,
                    "sha256": "0" * 64,
                }
            ],
        }
        await send_raw(pair.raw, frame(FrameType.JSON, json.dumps(document).encode()))

        refusal, peak = await refused_peak(
            receive_message(pair.receiver, ACCEPT_WORKLOAD, SystemClock())
        )

        assert refusal.code == "message_refused"
        assert refusal.details == {"reason": MessageRefusal.CAPACITY.value}
        assert not refusal.retryable
        assert peak < ALLOCATION_CEILING
        assert pair.receiver.closed


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (
            header("pair", [("first", FIRST), ("second", SECOND), ("third", FIRST)]),
            MessageRefusal.MALFORMED,
        ),
        (header("pair", [("first", FIRST)]), MessageRefusal.MALFORMED),
        (
            header("pair", [("second", SECOND), ("first", FIRST)]),
            MessageRefusal.MALFORMED,
        ),
        (
            header("pair", [("first", FIRST), ("other", SECOND)]),
            MessageRefusal.MALFORMED,
        ),
        (
            header("other", [("first", FIRST), ("second", SECOND)]),
            MessageRefusal.MALFORMED,
        ),
        (
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST[:4], 64)
            + frame(FrameType.JSON, b"{}"),
            MessageRefusal.MALFORMED,
        ),
        (
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST + SECOND, 64),
            MessageRefusal.CAPACITY,
        ),
        (
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST, 64)
            + bytes_frames(FIRST, 64),
            MessageRefusal.INTEGRITY,
        ),
        (
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST, 64)
            + frame(FrameType.BYTES, b""),
            MessageRefusal.MALFORMED,
        ),
    ],
    ids=[
        "extra",
        "missing",
        "reordered",
        "misnamed",
        "wrong-operation",
        "short",
        "spanning",
        "mismatching",
        "empty-frame",
    ],
)
async def test_section_stream_violations_are_refused(
    raw: bytes, reason: MessageRefusal
) -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(pair.raw, raw)

        refusal, _ = await refused_peak(
            receive_message(pair.receiver, PAIR, SystemClock())
        )

        assert refusal.details == {"reason": reason.value}
        assert pair.receiver.closed


async def test_valid_sections_are_received_in_their_declared_order() -> None:
    async with channel_pair(SystemClock()) as pair:
        await send_raw(
            pair.raw,
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST, 3)
            + bytes_frames(SECOND, 64),
        )

        message = await receive_message(pair.receiver, PAIR, SystemClock())

        assert message.request_id == "request-1"
        assert message.sections == (bytearray(FIRST), bytearray(SECOND))
        assert not pair.receiver.closed


async def test_stalled_section_is_refused_at_its_frame_deadline() -> None:
    clock = StepClock()
    async with channel_pair(clock) as pair:
        await send_raw(
            pair.raw,
            header("pair", [("first", FIRST), ("second", SECOND)])
            + bytes_frames(FIRST[:4], 64),
        )
        receiving = asyncio.create_task(receive_message(pair.receiver, PAIR, clock))
        await clock.waiting.wait()
        started_ns = clock.monotonic_ns()

        clock.advance(FRAME_PROGRESS_NS)
        refusal, _ = await refused_peak(receiving)

        assert refusal.details == {"reason": MessageRefusal.DEADLINE.value}
        assert refusal.retryable
        assert clock.monotonic_ns() - started_ns == FRAME_PROGRESS_NS
        assert pair.receiver.closed


async def test_trickling_message_is_refused_at_its_message_deadline() -> None:
    clock = TickingClock(NANOSECONDS_PER_SECOND)
    body = b'{"value":' + b"1" * 54 + b"}"
    operation = SectionedOperation("trickle", 1024, (SectionSpec("body", 64, 4),))
    async with channel_pair(clock) as pair:
        await send_raw(pair.raw, header("trickle", [("body", body)]))
        await send_raw(pair.raw, bytes_frames(body, 1))

        refusal, _ = await refused_peak(
            receive_message(pair.receiver, operation, clock)
        )

        assert refusal.details == {"reason": MessageRefusal.DEADLINE.value}
        assert (
            MESSAGE_PROGRESS_NS <= clock.offset_ns < len(body) * NANOSECONDS_PER_SECOND
        )
        assert pair.receiver.closed


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value":1.0,"value":2.0,"nested":{}}',
        b'{"value":NaN,"nested":{}}',
        b'{"value":Infinity,"nested":{}}',
        b'{"value":1.0,"nested":{"a":{"a":{}}}}',
    ],
    ids=["duplicate-key", "nan", "infinity", "too-deep"],
)
async def test_section_json_is_decoded_strictly_before_validation(
    payload: bytes,
) -> None:
    spec = SectionSpec("body", 64, 3)
    Loose.model_validate_json(payload)

    with pytest.raises(KoraProtocolError) as refused:
        decode_section(bytearray(payload), spec, Loose)

    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}
    assert refused.value.__context__ is None


async def test_invalid_section_refusal_keeps_no_validation_error() -> None:
    spec = SectionSpec("body", 64, 3)

    with pytest.raises(KoraProtocolError) as refused:
        decode_section(bytearray(b'{"value":"text","nested":{}}'), spec, Loose)

    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None


async def test_duplicate_workload_member_is_refused() -> None:
    encoded = canonical_bytes(json.loads(json.dumps(workload())))
    duplicated = encoded[:-1] + b',"kind":"command"}'

    with pytest.raises(KoraProtocolError) as refused:
        decode_section(bytearray(duplicated), ACCEPT_WORKLOAD.sections[0], Workload)

    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}


async def test_maximal_workload_round_trips_through_the_codec() -> None:
    clock = SystemClock()
    encoded = canonical_bytes(json.loads(json.dumps(maximal_workload())))
    async with channel_pair(clock) as pair:
        sender = BoundedChannel(pair.raw, clock)

        sending = asyncio.create_task(
            send_message(sender, ACCEPT_WORKLOAD, "request-1", [encoded], clock)
        )
        message = await receive_message(pair.receiver, ACCEPT_WORKLOAD, clock)
        await sending
        decoded = decode_section(
            message.sections[0], ACCEPT_WORKLOAD.sections[0], Workload
        )

        assert len(encoded) > 11 * 1048576
        assert len(encoded) // SECTION_FRAME_BYTES > 100
        assert message.sections[0] == encoded
        assert decoded == Workload.model_validate_json(encoded)


async def test_sender_refuses_a_section_over_its_cap() -> None:
    async with channel_pair(SystemClock()) as pair:
        sender = BoundedChannel(pair.raw, SystemClock())

        with pytest.raises(KoraProtocolError) as refused:
            await send_message(
                sender, PAIR, "request-1", [b"x" * 65, b"{}"], SystemClock()
            )

        assert refused.value.details == {"reason": MessageRefusal.CAPACITY.value}


async def test_error_envelope_round_trips() -> None:
    error = operation_unknown()
    encoded = error_envelope("request-1", error)

    request_id, decoded = read_error_envelope(encoded)

    assert request_id == "request-1"
    assert (decoded.code, decoded.message, decoded.retryable, decoded.details) == (
        "operation_unknown",
        error.message,
        False,
        {},
    )


async def test_error_envelope_carries_only_reviewed_text() -> None:
    error = KoraProtocolError("operation_unknown", "input value: secret-op", False, {})

    encoded = error_envelope("request-1", error)

    assert b"secret-op" not in encoded
    assert json.loads(encoded)["error"]["message"] == operation_unknown().message


@pytest.mark.parametrize(
    "error",
    [
        KoraProtocolError("operation_unknown", "x", False, {"op": "secret-op"}),
        KoraProtocolError("operation_unknown", "x", True, {}),
        KoraProtocolError("workload_unlisted", "x", False, {}),
        message_refused(MessageRefusal.MALFORMED),
    ],
    ids=["unlisted-detail", "wrong-retryable", "unlisted-code", "refused-message"],
)
async def test_error_envelope_refuses_unlisted_errors(
    error: KoraProtocolError,
) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        error_envelope("request-1", error)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"request_id":"r","error":{"code":"operation_unknown","message":'
        b'"the control operation is not known","retryable":false,'
        b'"retryable":true,"details":{}}}',
        b'{"request_id":"r","error":{"code":"Bad-Code","message":"m",'
        b'"retryable":false,"details":{}}}',
        b'{"request_id":"r","error":{"code":"operation_unknown","message":'
        b'"the control operation is not known","retryable":0,"details":{}}}',
        b'{"request_id":"r","error":{"code":"made_up","message":"m",'
        b'"retryable":false,"details":{}}}',
        b'{"request_id":"r","error":{"code":"operation_unknown","message":'
        b'"peer text","retryable":false,"details":{}}}',
        b'{"request_id":"r","error":{"code":"operation_unknown","message":'
        b'"the control operation is not known","retryable":false,'
        b'"details":{"op":"x"}}}',
        b'{"request_id":"r","error":{"code":"operation_unknown","message":'
        b'"the control operation is not known","retryable":true,"details":{}}}',
        b'{"request_id":"r","result":"page","value":{}}',
    ],
    ids=[
        "duplicate-member",
        "bad-code",
        "loose-boolean",
        "unlisted-code",
        "peer-message",
        "unlisted-detail",
        "wrong-retryable",
        "not-an-error",
    ],
)
async def test_malformed_error_envelope_is_refused(payload: bytes) -> None:
    with pytest.raises(KoraProtocolError) as refused:
        read_error_envelope(payload)

    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}


def observed_status() -> StatusValue:
    status = RuntimeStatus(
        state="starting",
        exit_code=None,
        phase="preparation",
        observed_at=StepClock().now(),
    )
    return StatusValue(status=status, holds_workload=False)


async def test_status_request_and_response_round_trip() -> None:
    request = read_request(control_request("status", "request-1"))
    value = observed_status()

    decoded = read_status(status_envelope("request-1", value), "request-1")

    assert request is not None
    assert (request.op, request.request_id) == ("status", "request-1")
    assert decoded == value


@pytest.mark.parametrize(
    "payload",
    [
        b'{"op":"status","request_id":"r","extra":1}',
        b'{"op":"status","request_id":"r","request_id":"s"}',
        b'{"op":"status","request_id":{"nested":1}}',
        b"[]",
    ],
    ids=["extra-member", "duplicate-member", "nested", "not-an-object"],
)
async def test_malformed_request_is_refused(payload: bytes) -> None:
    assert read_request(payload) is None


@pytest.mark.parametrize(
    "envelope",
    [
        {"request_id": "other", "result": "status"},
        {"request_id": "request-1", "result": "page"},
        {"request_id": "request-1", "result": "status", "extra": 1},
    ],
    ids=["foreign-request-id", "mismatching-tag", "extra-member"],
)
async def test_mismatching_status_response_is_refused(
    envelope: dict[str, object],
) -> None:
    value = json.loads(status_envelope("request-1", observed_status()))["value"]
    payload = json.dumps({**envelope, "value": value}).encode()

    with pytest.raises(KoraProtocolError) as refused:
        read_status(payload, "request-1")

    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}


async def test_status_error_response_is_raised_for_its_request() -> None:
    answered = error_envelope("request-1", operation_unknown())
    foreign = error_envelope("other", operation_unknown())

    with pytest.raises(KoraProtocolError) as raised:
        read_status(answered, "request-1")
    with pytest.raises(KoraProtocolError) as refused:
        read_status(foreign, "request-1")

    assert raised.value.code == "operation_unknown"
    assert refused.value.details == {"reason": MessageRefusal.MALFORMED.value}
