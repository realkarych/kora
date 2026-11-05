import asyncio
import gc
import json
import traceback
import weakref

import pytest

from kora.plugins.harness.codex.app_server_messages import (
    CompletedMessage,
    MessageFailure,
    MessageRefused,
    TurnMessages,
    decode_completed_message,
    reject_premature_message,
)
from kora.plugins.harness.codex.app_server_rpc import Notification, RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_turn import observe_app_server_turn
from tests.plugins.harness.codex.probe import report_connection_failure
from tests.plugins.harness.codex.test_app_server_turn import completed, frame, started
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

THREAD = "selected-thread"
TURN = "started-turn"
MESSAGE = "message-1"
PRIVATE = "private-message-canary"
TEXT = f"  {PRIVATE}\nданные 🌲  "


def message(
    *,
    thread: object = THREAD,
    turn: object = TURN,
    identity: object = MESSAGE,
    text: object = TEXT,
    phase: object = None,
) -> Notification:
    return Notification(
        "item/completed",
        {
            "threadId": thread,
            "turnId": turn,
            "item": {
                "type": "agentMessage",
                "id": identity,
                "text": text,
                "phase": phase,
            },
        },
        None,
    )


def encoded(notice: Notification) -> bytes:
    return frame({"method": notice.method, "params": notice.params})


@pytest.mark.parametrize(
    "phase", [None, "commentary", "partial_answer", "final_answer"]
)
@pytest.mark.parametrize("text", ["", "  \n", TEXT])
def test_completed_item_preserves_exact_text_and_phase_without_a_start(
    phase: object, text: str
) -> None:
    notice = message(phase=phase, text=text)

    result = decode_completed_message(notice, max_text_bytes=1024)

    assert result is not None
    assert (result.thread_id, result.turn_id, result.message_id) == (
        THREAD,
        TURN,
        MESSAGE,
    )
    assert result.text == text
    assert result.phase == phase
    assert PRIVATE not in repr(result)
    assert THREAD not in repr(result)


def test_optional_phase_and_additive_payloads_are_detached() -> None:
    item: dict[str, object] = {
        "type": "agentMessage",
        "id": MESSAGE,
        "text": TEXT,
        "delivery": {PRIVATE: PRIVATE},
        "questions": [PRIVATE],
        "memoryCitation": {PRIVATE: PRIVATE},
    }
    notice = Notification(
        "item/completed", {"threadId": THREAD, "turnId": TURN, "item": item}, 123
    )

    result = decode_completed_message(notice, max_text_bytes=1024)
    item["text"] = "changed"
    item["phase"] = "final_answer"

    assert result is not None
    assert result.text == TEXT
    assert result.phase is None
    assert set(vars(result)) == {"thread_id", "turn_id", "message_id", "text", "phase"}


@pytest.mark.parametrize(
    "notice",
    [
        Notification("item/started", {PRIVATE: PRIVATE}, None),
        Notification("item/agentMessage/delta", {"delta": TEXT}, None),
        Notification("unknown", None, None),
        Notification("item/completed", {"item": {"type": "reasoning"}}, None),
        Notification("item/completed", {"item": {"type": "futureItem"}}, None),
    ],
)
def test_partial_and_other_items_are_not_completed_messages(
    notice: Notification,
) -> None:
    state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1)

    decoded = decode_completed_message(notice, max_text_bytes=1)
    accepted = state.accept(notice)
    reject_premature_message(notice)

    assert decoded is accepted is None
    assert state.count == 0
    assert state.for_turn(TURN) is None


@pytest.mark.parametrize(
    "notice",
    [
        Notification("item/completed", None, None),
        Notification("item/completed", {"item": []}, None),
        Notification("item/completed", {"item": {}}, None),
        message(thread=""),
        message(turn=False),
        message(identity="\ud800"),
        message(identity="x" * 257),
        message(identity="🌲" * 65),
        message(text=None),
        message(text="\ud800"),
        message(phase=PRIVATE),
        message(phase={PRIVATE: PRIVATE}),
        message(phase=[]),
    ],
)
def test_malformed_recognized_messages_fail_with_safe_errors(
    notice: Notification,
) -> None:
    with pytest.raises(MessageRefused) as caught:
        decode_completed_message(notice, max_text_bytes=1024)

    assert caught.value.reason is MessageFailure.INVALID_MESSAGE
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("limit", [len(TEXT), len(TEXT.encode()) - 1, 1])
def test_utf8_byte_overflow_refuses_without_truncation(limit: int) -> None:
    notice = message()

    with pytest.raises(MessageRefused) as caught:
        decode_completed_message(notice, max_text_bytes=limit)

    assert caught.value.reason is MessageFailure.TEXT_LIMIT
    assert PRIVATE not in str(caught.value)


def test_exact_utf8_text_and_identity_limits_are_admitted() -> None:
    notice = message(identity="🌲" * 64)

    result = decode_completed_message(notice, max_text_bytes=len(TEXT.encode()))

    assert result is not None
    assert result.text == TEXT
    assert result.message_id == "🌲" * 64


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_construction_and_decoder_budgets_refuse(limit: int) -> None:
    with pytest.raises(ValueError, match="^invalid completed-message budget$"):
        TurnMessages(THREAD, max_messages=limit, max_text_bytes=1024)
    with pytest.raises(ValueError, match="^invalid completed-message budget$"):
        TurnMessages(THREAD, max_messages=1, max_text_bytes=limit)
    with pytest.raises(ValueError, match="^invalid completed-message budget$"):
        decode_completed_message(message(), max_text_bytes=limit)


@pytest.mark.parametrize(
    ("notice", "reason"),
    [
        (
            message(thread=PRIVATE, identity="message-2"),
            MessageFailure.IDENTITY_MISMATCH,
        ),
        (message(turn=PRIVATE, identity="message-2"), MessageFailure.IDENTITY_MISMATCH),
        (message(text="changed"), MessageFailure.DUPLICATE_MESSAGE),
        (message(identity="message-2", text=TEXT * 40), MessageFailure.TEXT_LIMIT),
        (message(identity="message-2", phase=PRIVATE), MessageFailure.INVALID_MESSAGE),
        (message(identity=""), MessageFailure.INVALID_MESSAGE),
    ],
)
def test_refused_admission_preserves_latest_message_and_duplicate_capacity(
    notice: Notification, reason: MessageFailure
) -> None:
    state = TurnMessages(THREAD, max_messages=2, max_text_bytes=1024)
    first = state.accept(message())

    with pytest.raises(MessageRefused) as caught:
        state.accept(notice)
    unchanged = state.for_turn(TURN)
    unchanged_count = state.count
    second = state.accept(
        message(identity="message-2", text="last", phase="final_answer")
    )
    with pytest.raises(MessageRefused) as full:
        state.accept(message(identity="message-3"))

    assert caught.value.reason is reason
    assert unchanged is first
    assert unchanged_count == 1
    assert full.value.reason is MessageFailure.MESSAGE_LIMIT
    assert state.count == 2
    assert state.for_turn(TURN) is second


def test_replacement_releases_the_previous_text_value_and_checks_acknowledgement() -> (
    None
):
    state = TurnMessages(THREAD, max_messages=2, max_text_bytes=1024)
    first = state.accept(message())
    assert first is not None
    reference = weakref.ref(first)

    state.accept(message(identity="message-2", text=""))
    del first
    gc.collect()
    with pytest.raises(MessageRefused) as caught:
        state.for_turn(PRIVATE)

    assert reference() is None
    assert caught.value.reason is MessageFailure.IDENTITY_MISMATCH
    assert state.count == 2
    assert state.for_turn(TURN) == CompletedMessage(THREAD, TURN, "message-2", "", None)


@pytest.mark.parametrize("reason", list(MessageFailure))
def test_actual_fixture_reports_only_the_fixed_message_reason(
    reason: MessageFailure, capsys: pytest.CaptureFixture[str]
) -> None:
    error = MessageRefused(reason)

    status = report_connection_failure(error)
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "level": "ERROR",
        "event": "probe.case_failed",
        "detail": reason.value,
    }
    assert PRIVATE not in captured.err


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.asyncio
async def test_completed_messages_correlate_with_early_or_late_start_reply(
    early: bool,
) -> None:
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1024)

        def observe(notice: Notification) -> None:
            state.accept(notice)

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                observe=observe,
                observe_before_send=reject_premature_message,
            )
        )
        await connection.peer_reader.readline()
        events = (
            (encoded(message()), started())
            if early
            else (started(), encoded(message()))
        )

        await connection.send(*events, completed())
        terminal = await operation

        assert terminal.status == "completed"
        assert state.count == 1
        assert state.for_turn(terminal.turn_id) == CompletedMessage(
            THREAD, TURN, MESSAGE, TEXT, None
        )


@pytest.mark.asyncio
async def test_wrong_provisional_turn_cannot_be_selected_after_acknowledgement() -> (
    None
):
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1024)

        def observe(notice: Notification) -> None:
            state.accept(notice)

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                observe=observe,
                on_started=lambda identity: state.for_turn(identity) and None,
            )
        )
        await connection.peer_reader.readline()

        await connection.send(encoded(message(turn=PRIVATE)), started())
        with pytest.raises(MessageRefused) as caught:
            await operation

        assert caught.value.reason is MessageFailure.IDENTITY_MISMATCH
        assert PRIVATE not in str(caught.value)


@pytest.mark.asyncio
async def test_already_read_premature_message_writes_no_turn_bytes() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        setup = connection.start(
            connection.rpc.call(
                "thread/start",
                {},
                deadline=deadline,
                observe=lambda _: None,
            )
        )
        await connection.peer_reader.readline()
        payload = encoded(message())
        await connection.send(frame({"id": 1, "result": {}}), payload[:13])
        await setup
        await connection.send(payload[13:])

        with pytest.raises(MessageRefused) as caught:
            await observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=deadline,
                observe_before_send=reject_premature_message,
            )
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is MessageFailure.PREMATURE_MESSAGE
        assert remaining == b""


@pytest.mark.asyncio
async def test_trailing_completed_message_needs_an_explicit_owner_read() -> None:
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1024)

        def observe(notice: Notification) -> None:
            state.accept(notice)

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                observe=observe,
            )
        )
        await connection.peer_reader.readline()

        await connection.send(started(), completed(), encoded(message()))
        terminal = await operation
        before = state.for_turn(terminal.turn_id)
        await connection.rpc.receive(deadline=future_deadline(), observe=observe)

        assert before is None
        assert state.count == 1
        assert state.for_turn(terminal.turn_id) is not None


@pytest.mark.asyncio
async def test_partial_delta_then_eof_produces_no_completed_message_or_turn() -> None:
    async with wire() as connection:
        state = TurnMessages(THREAD, max_messages=1, max_text_bytes=1)
        delta_seen = asyncio.Event()

        def observe(notice: Notification) -> None:
            state.accept(notice)
            delta_seen.set()

        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="fixed",
                deadline=future_deadline(),
                observe=observe,
            )
        )
        await connection.peer_reader.readline()

        await connection.send(
            started(),
            encoded(Notification("item/agentMessage/delta", {"delta": TEXT}, None)),
        )
        await delta_seen.wait()
        connection.peer_writer.write_eof()
        with pytest.raises(RpcError) as caught:
            await operation

        assert caught.value.reason is RpcFailure.DISCONNECTED
        assert state.count == 0
        assert state.for_turn(TURN) is None
