import asyncio
import traceback
import weakref
from functools import partial
from json.encoder import encode_basestring
from typing import Literal

import pytest

from kora.conformance.harness import ConformanceSink, Trace
from kora.ir.agent_session_validation import TRUNCATION_MARKER
from kora.observability.redaction import MASK, SecretRegistry
from kora.plugins.harness.codex.app_server_messages import (
    CompletedMessage,
    MessageFailure,
    MessageRefused,
)
from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import (
    AppServerTurnSession,
    SessionFailure,
    SessionRefused,
)
from kora.plugins.harness.codex.app_server_turn import MAX_FINAL_MESSAGE_BYTES
from kora.plugins.harness.codex.turn_events import MASK_CHUNK_BYTES, CodexTurnEvents
from kora.protocols.harness import MAX_TEXT_BYTES, TurnData, TurnEvent
from tests.plugins.harness.codex.test_app_server_messages import encoded, message
from tests.plugins.harness.codex.test_app_server_output import (
    item,
    terminal_frame,
    terminal_params,
)
from tests.plugins.harness.codex.test_app_server_session import Clock, start_request
from tests.plugins.harness.codex.test_app_server_turn import (
    PROMPT,
    THREAD,
    TURN,
    completed,
    frame,
    started,
)
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

PRIVATE_ID = "native-item-id\nprivate"
PRIVATE_TEXT = "private item body"
DELTA = frame(
    {
        "method": "item/agentMessage/delta",
        "params": {"threadId": THREAD, "turnId": TURN, "itemId": PRIVATE_ID},
    }
)
MARKER = TRUNCATION_MARKER.decode("utf-8")


def turn_data(sink: ConformanceSink) -> tuple[TurnData, ...]:
    result: list[TurnData] = []
    for event in sink.events:
        assert isinstance(event, TurnEvent)
        result.append(event.data)
    return tuple(result)


@pytest.mark.asyncio
async def test_confirmed_items_emit_once_in_order_and_release_earlier_text() -> None:
    sink = ConformanceSink(3, Trace())
    session = AppServerTurnSession(THREAD, max_messages=3, max_tools=1)
    notice_seen: asyncio.Queue[None] = asyncio.Queue()
    waiting = asyncio.Event()
    references: list[weakref.ReferenceType[CompletedMessage]] = []

    async with wire() as connection:
        deadline = future_deadline()
        projector = CodexTurnEvents(
            sink,
            registry=SecretRegistry(),
            check_deadline=partial(connection.rpc.check_deadline, deadline),
        )

        def publish(ordinal: int, observation: CompletedMessage) -> None:
            references.append(weakref.ref(observation))
            projector.observe(ordinal, observation)

        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=deadline,
                on_message=publish,
                on_waiting=waiting.set,
                observe=lambda _: notice_seen.put_nowait(None),
            )
        )
        await start_request(connection)

        await connection.send(
            DELTA,
            encoded(message(identity=PRIVATE_ID, text="")),
            encoded(message(identity="second", text="commentary", phase="commentary")),
        )
        for _ in range(3):
            await notice_seen.get()
        before_ack = turn_data(sink)
        await connection.send(started())
        await waiting.wait()
        after_ack = turn_data(sink)
        first_released = references[0]() is None
        await connection.send(encoded(message(identity="third", text="last 🌲")))
        await notice_seen.get()
        before_terminal = turn_data(sink)
        await connection.send(terminal_frame(terminal_params([item()])))
        terminal = await operation

        assert before_ack == ()
        assert after_ack == before_terminal[:2]
        assert (
            turn_data(sink)
            == before_terminal
            == (
                TurnData(message_id="codex-message-1", text="", truncated=False),
                TurnData(
                    message_id="codex-message-2", text="commentary", truncated=False
                ),
                TurnData(message_id="codex-message-3", text="last 🌲", truncated=False),
            )
        )
        assert terminal.final_message is not None
        assert sink.count.events == 3
        assert sink.enforcement().cause == "max_events"
        assert all(
            "native-item-id" not in event.model_dump_json() for event in sink.events
        )
        assert first_released
        assert references[0]() is references[1]() is None
        assert references[2]() is session.snapshot().latest_message


@pytest.mark.parametrize("outcome", ["mismatch", "duplicate", "cancel", "eof"])
@pytest.mark.asyncio
async def test_unconfirmed_items_do_not_reach_the_sink(
    outcome: Literal["mismatch", "duplicate", "cancel", "eof"],
) -> None:
    session = AppServerTurnSession(THREAD, max_messages=2, max_tools=1)
    received = asyncio.Event()
    delivered: list[int] = []
    pending = encoded(message(identity=PRIVATE_ID, text=PRIVATE_TEXT))

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _: received.set(),
                on_message=lambda ordinal, _: delivered.append(ordinal),
            )
        )
        await start_request(connection)
        await connection.send(pending)
        await received.wait()

        if outcome == "cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await operation
            error = cancelled.value
        elif outcome == "eof":
            connection.peer_writer.write_eof()
            with pytest.raises(RpcError) as disconnected:
                await operation
            assert disconnected.value.reason is RpcFailure.DISCONNECTED
            error = disconnected.value
        else:
            refusal = (
                started({"turn": {"id": "foreign-turn", "status": "inProgress"}})
                if outcome == "mismatch"
                else pending
            )
            await connection.send(refusal)
            with pytest.raises(MessageRefused) as refused:
                await operation
            assert refused.value.reason is (
                MessageFailure.IDENTITY_MISMATCH
                if outcome == "mismatch"
                else MessageFailure.DUPLICATE_MESSAGE
            )
            error = refused.value

        diagnostic = "".join(traceback.format_exception(error))
        assert PRIVATE_TEXT not in diagnostic
        assert "native-item-id" not in diagnostic
        assert delivered == []
        assert session.snapshot().latest_message is None


@pytest.mark.asyncio
async def test_start_callback_failure_discards_pending_message_delivery() -> None:
    session = AppServerTurnSession(THREAD, max_messages=1, max_tools=1)
    delivered: list[int] = []

    def refuse(_: str) -> None:
        raise ValueError("start callback refused")

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                on_started=refuse,
                on_message=lambda ordinal, _: delivered.append(ordinal),
            )
        )
        await start_request(connection)

        await connection.send(encoded(message()), started())
        with pytest.raises(ValueError, match="^start callback refused$"):
            await operation

        assert delivered == []
        assert session.snapshot().terminal_status is None


@pytest.mark.asyncio
async def test_deferred_callback_failure_releases_earlier_delivered_messages() -> None:
    session = AppServerTurnSession(THREAD, max_messages=3, max_tools=1)
    delivered: list[weakref.ReferenceType[CompletedMessage]] = []

    def publish(ordinal: int, message: CompletedMessage) -> None:
        if ordinal == 2:
            raise ValueError("message callback refused")
        delivered.append(weakref.ref(message))

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                on_message=publish,
            )
        )
        await start_request(connection)

        await connection.send(
            *(encoded(message(identity=f"native-{i}")) for i in range(3)), started()
        )
        with pytest.raises(ValueError, match="^message callback refused$") as caught:
            await operation

        assert caught.value.__traceback__ is not None
        assert len(delivered) == 1
        assert delivered[0]() is None
        assert session.snapshot().message_count == 3
        assert session.snapshot().terminal_status is None


@pytest.mark.asyncio
async def test_disconnect_keeps_a_confirmed_prefix() -> None:
    session = AppServerTurnSession(THREAD, max_messages=1, max_tools=1)
    delivered: list[int] = []
    seen = asyncio.Event()

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                on_message=lambda ordinal, _: delivered.append(ordinal),
                observe=lambda _: seen.set(),
            )
        )
        await start_request(connection)

        await connection.send(started(), encoded(message()), DELTA)
        await seen.wait()
        connection.peer_writer.write_eof()
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is RpcFailure.DISCONNECTED
        assert delivered == [1]
        assert session.snapshot().terminal_status is None


@pytest.mark.parametrize(
    "outcome", ["exact", "overflow", "no-observer", "item-overflow"]
)
@pytest.mark.asyncio
async def test_staging_budget_counts_the_utf8_bytes_of_every_identity_and_item(
    outcome: Literal["exact", "overflow", "no-observer", "item-overflow"],
) -> None:
    identities = tuple(f"native-id-{index}-é" for index in range(12))
    identity_bytes = sum(
        len(value.encode("utf-8"))
        for identity in identities
        for value in (THREAD, TURN, identity)
    )
    text_budget = MAX_FINAL_MESSAGE_BYTES - identity_bytes
    short_text = "é" + "a" * (text_budget // len(identities) - 2)
    final_text = "é" + "b" * (
        text_budget - (len(identities) - 1) * len(short_text.encode("utf-8")) - 2
    )
    if outcome in ("overflow", "no-observer"):
        final_text += "c"
    notices = tuple(
        encoded(message(identity=identity, text=text))
        for identity, text in zip(
            identities,
            (short_text,) * (len(identities) - 1) + (final_text,),
            strict=True,
        )
    )
    limit = len(identities) - int(outcome == "item-overflow")
    session = AppServerTurnSession(THREAD, max_messages=limit, max_tools=1)
    delivered: list[int] = []

    def publish(ordinal: int, _: CompletedMessage) -> None:
        delivered.append(ordinal)

    async with wire() as connection:
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                on_message=None if outcome == "no-observer" else publish,
            )
        )
        await start_request(connection)

        await connection.send(*notices)
        if outcome == "overflow":
            with pytest.raises(SessionRefused) as caught:
                await operation
            assert caught.value.reason is SessionFailure.PENDING_MESSAGE_LIMIT
        elif outcome == "item-overflow":
            with pytest.raises(MessageRefused) as item_refused:
                await operation
            assert item_refused.value.reason is MessageFailure.MESSAGE_LIMIT
        else:
            await connection.send(started(), completed())
            await operation

        if outcome == "exact":
            assert delivered == list(range(1, len(identities) + 1))
        else:
            assert delivered == []
        assert session.snapshot().message_count == (
            0 if outcome in ("overflow", "item-overflow") else len(identities)
        )


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.asyncio
async def test_deferred_delivery_rechecks_deadline_even_with_an_early_terminal(
    count: int,
) -> None:
    clock = Clock()
    clock.value = asyncio.get_running_loop().time()
    deadline = future_deadline()
    session = AppServerTurnSession(THREAD, max_messages=count, max_tools=1)
    delivered: list[int] = []

    def publish(ordinal: int, _: CompletedMessage) -> None:
        delivered.append(ordinal)
        clock.value = deadline

    async with wire(clock=clock.now) as connection:
        operation = connection.start(
            session.run(
                connection.rpc, prompt=PROMPT, deadline=deadline, on_message=publish
            )
        )
        await start_request(connection)

        await connection.send(
            *(encoded(message(identity=f"native-{i}")) for i in range(count)),
            completed(),
            started(),
        )
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is RpcFailure.DEADLINE
        assert delivered == [1]
        assert session.snapshot().terminal_status is None


@pytest.mark.parametrize("suffix", ["", "é", "\x01"], ids=["exact", "utf8", "escaped"])
def test_turn_text_clips_at_the_canonical_json_limit(suffix: str) -> None:
    sink = ConformanceSink(None, Trace())
    emitter = CodexTurnEvents(
        sink, registry=SecretRegistry(), check_deadline=lambda: None
    )
    text = "a" * (MAX_TEXT_BYTES - 10) + '"\\🌲'
    observation = CompletedMessage(THREAD, TURN, PRIVATE_ID, text + suffix, None)

    emitter.observe(1, observation)

    (data,) = turn_data(sink)
    assert data.text is not None
    assert len(encode_basestring(data.text).encode("utf-8")) == MAX_TEXT_BYTES
    assert data.truncated is bool(suffix)
    assert data.text == (
        "a" * (MAX_TEXT_BYTES - len(encode_basestring(MARKER).encode("utf-8"))) + MARKER
        if suffix
        else text
    )


@pytest.mark.parametrize("location", ["piece", "clip"])
def test_turn_text_masks_before_piece_and_clipping_boundaries(location: str) -> None:
    registry = SecretRegistry()
    canary = "turn-secret-example"
    registration = registry.register(canary)
    sink = ConformanceSink(None, Trace())
    emitter = CodexTurnEvents(sink, registry=registry, check_deadline=lambda: None)
    clip_chars = MAX_TEXT_BYTES - len(encode_basestring(MARKER).encode("utf-8"))
    prefix = "a" * ((MASK_CHUNK_BYTES if location == "piece" else clip_chars) - 4)
    suffix = "after" if location == "piece" else "b" * 100
    observation = CompletedMessage(
        THREAD, TURN, PRIVATE_ID, prefix + canary + suffix, None
    )

    try:
        emitter.observe(1, observation)
    finally:
        registration.release()

    (data,) = turn_data(sink)
    assert data.text == (
        prefix + MASK + suffix if location == "piece" else prefix + MASK[:4] + MARKER
    )
    assert data.truncated is (location == "clip")
    assert data.message_id == "codex-message-1"


def test_projection_checks_deadline_again_before_emitting() -> None:
    sink = ConformanceSink(None, Trace())
    checks = iter((0.0, 1.0))

    def check_deadline() -> None:
        if next(checks) >= 1.0:
            raise RpcError(RpcFailure.DEADLINE)

    emitter = CodexTurnEvents(
        sink, registry=SecretRegistry(), check_deadline=check_deadline
    )
    observation = CompletedMessage(THREAD, TURN, PRIVATE_ID, PRIVATE_TEXT, None)

    with pytest.raises(RpcError) as caught:
        emitter.observe(1, observation)

    assert caught.value.reason is RpcFailure.DEADLINE
    assert sink.events == []
