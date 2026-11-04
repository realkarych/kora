import json
import traceback

import pytest

from kora.plugins.harness.codex.app_server_rpc import MAX_RPC_INTEGER, Notification
from kora.plugins.harness.codex.app_server_turn import observe_app_server_turn
from kora.plugins.harness.codex.app_server_usage import (
    USAGE_METHOD,
    TokenCounts,
    TurnUsage,
    UsageChange,
    UsageError,
    UsageFailure,
    UsageSnapshot,
    decode_usage,
    reject_premature_usage,
)
from tests.plugins.harness.codex.probe import report_connection_failure
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

THREAD = "selected-thread"
TURN = "started-turn"
CANARY = "private-usage-wire-canary"
FIRST: dict[str, object] = {
    "inputTokens": 100,
    "cachedInputTokens": 20,
    "cacheWriteInputTokens": 30,
    "outputTokens": 10,
    "reasoningOutputTokens": 4,
    "totalTokens": 110,
}
SECOND: dict[str, object] = {
    "inputTokens": 160,
    "cachedInputTokens": 25,
    "cacheWriteInputTokens": 40,
    "outputTokens": 30,
    "reasoningOutputTokens": 10,
    "totalTokens": 190,
}
ZERO: dict[str, object] = dict.fromkeys(FIRST, 0)


def notice(
    counts: object, *, thread_id: object = THREAD, turn_id: object = TURN
) -> Notification:
    return Notification(
        USAGE_METHOD,
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {"total": counts, "last": FIRST},
        },
        123,
    )


def snapshot(
    counts: dict[str, object], *, thread_id: str = THREAD, turn_id: str = TURN
) -> UsageSnapshot:
    decoded = decode_usage(notice(counts, thread_id=thread_id, turn_id=turn_id))
    assert decoded is not None
    return decoded


def usage_frame(turn_id: str = TURN) -> bytes:
    notification = notice(FIRST, turn_id=turn_id)
    return (
        json.dumps(
            {"method": notification.method, "params": notification.params}
        ).encode()
        + b"\n"
    )


def test_codex_usage_replaces_total_and_replay_does_not_add() -> None:
    usage = TurnUsage(THREAD)
    first = snapshot(FIRST)
    second = snapshot(SECOND)

    observed = (usage.accept(first), usage.accept(second), usage.accept(second))
    latest = usage.for_turn(TURN)

    assert observed == (
        UsageChange.ADVANCED,
        UsageChange.ADVANCED,
        UsageChange.DUPLICATE,
    )
    assert latest == second
    assert second.counts == TokenCounts(160, 25, 40, 30, 10)
    assert second.counts.input_tokens == 95
    assert not usage.observation_gap


def test_codex_usage_distinguishes_no_observation_missing_and_zero() -> None:
    usage = TurnUsage(THREAD)
    unknown = ZERO.copy()
    del unknown["cacheWriteInputTokens"]
    completion = Notification("turn/completed", None, None)

    unrelated = decode_usage(completion)
    empty = usage.for_turn(TURN)
    before = snapshot(unknown)
    after = snapshot(ZERO)
    advanced = (usage.accept(before), usage.accept(after))

    assert empty is unrelated is None
    assert before.counts.cache_creation_tokens is None
    assert before.counts.input_tokens is None
    assert after.counts.cache_creation_tokens == after.counts.input_tokens == 0
    assert advanced == (UsageChange.ADVANCED, UsageChange.ADVANCED)
    assert usage.for_turn(TURN) == after
    assert not usage.observation_gap


@pytest.mark.parametrize(
    "counts",
    [
        pytest.param(FIRST, id="older-snapshot"),
        pytest.param(SECOND | {"cachedInputTokens": 26}, id="uncached-decreases"),
        pytest.param(
            SECOND | {"reasoningOutputTokens": 11}, id="ordinary-output-decreases"
        ),
        pytest.param(SECOND | {"cacheWriteInputTokens": 39}, id="write-decreases"),
        pytest.param(
            {
                key: value
                for key, value in SECOND.items()
                if key != "cacheWriteInputTokens"
            },
            id="known-write-disappears",
        ),
    ],
)
def test_codex_usage_regression_preserves_previous_and_gap(
    counts: dict[str, object],
) -> None:
    usage = TurnUsage(THREAD)
    previous = snapshot(SECOND)
    later = snapshot(SECOND | {"inputTokens": 161, "totalTokens": 191})
    rejected = snapshot(counts)
    usage.accept(previous)

    regression = usage.accept(rejected)
    preserved = usage.for_turn(TURN)
    advance = usage.accept(later)
    latest = usage.for_turn(TURN)

    assert regression is UsageChange.REGRESSION
    assert preserved == previous
    assert advance is UsageChange.ADVANCED
    assert latest == later
    assert usage.observation_gap


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(notice(None), id="missing-total"),
        pytest.param(notice(FIRST, thread_id=""), id="empty-thread"),
        pytest.param(notice(FIRST, turn_id=1), id="nontext-turn"),
        pytest.param(notice(FIRST, turn_id="\ud800"), id="invalid-scalar"),
        pytest.param(notice(FIRST | {"outputTokens": True}), id="boolean"),
        pytest.param(notice(FIRST | {"outputTokens": 10.0}), id="float"),
        pytest.param(notice(FIRST | {"cachedInputTokens": -1}), id="negative"),
        pytest.param(
            notice(FIRST | {"inputTokens": MAX_RPC_INTEGER + 1}), id="integer-overflow"
        ),
        pytest.param(notice(FIRST | {"cacheWriteInputTokens": None}), id="null-write"),
        pytest.param(
            notice(FIRST | {"cachedInputTokens": 80}), id="cache-exceeds-input"
        ),
        pytest.param(
            notice(FIRST | {"reasoningOutputTokens": 11}), id="reasoning-exceeds-output"
        ),
        pytest.param(
            notice(ZERO | {"totalTokens": 128_000}), id="synthetic-context-fill"
        ),
    ],
)
def test_codex_usage_refuses_invalid_native_observation(
    message: Notification,
) -> None:
    with pytest.raises(UsageError) as caught:
        decode_usage(message)

    assert caught.value.reason is UsageFailure.INVALID_SNAPSHOT


def test_codex_usage_refusal_and_representations_are_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid = snapshot(FIRST, thread_id=CANARY)
    invalid = notice(FIRST | {"inputTokens": CANARY}, thread_id=CANARY)

    with pytest.raises(UsageError) as caught:
        decode_usage(invalid)
    exit_status = report_connection_failure(caught.value)
    output = capsys.readouterr()
    trace = "".join(traceback.format_exception(caught.value))

    assert exit_status == 1
    assert output.out == ""
    assert json.loads(output.err) == {
        "level": "ERROR",
        "event": "probe.case_failed",
        "detail": UsageFailure.INVALID_SNAPSHOT.value,
    }
    assert CANARY not in output.err + trace + repr(valid) + repr(valid.counts)


@pytest.mark.parametrize(
    "foreign",
    [
        pytest.param(snapshot(SECOND, thread_id="other-thread"), id="thread"),
        pytest.param(snapshot(SECOND, turn_id="other-turn"), id="turn"),
    ],
)
def test_codex_usage_refuses_different_identity_without_replacement(
    foreign: UsageSnapshot,
) -> None:
    usage = TurnUsage(THREAD)
    original = snapshot(FIRST)
    usage.accept(original)

    with pytest.raises(UsageError) as caught:
        usage.accept(foreign)
    retained = usage.for_turn(TURN)

    assert caught.value.reason is UsageFailure.IDENTITY_MISMATCH
    assert retained == original
    assert not usage.observation_gap


@pytest.mark.asyncio
async def test_codex_usage_already_read_before_send_refuses_without_writing() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        setup = connection.start(
            connection.rpc.call(
                "thread/start", {}, deadline=deadline, observe=lambda _: None
            )
        )
        await connection.peer_reader.readline()
        raw = usage_frame()
        await connection.send(b'{"id":1,"result":null}\n' + raw[:12])
        assert await setup is None
        await connection.send(raw[12:])
        seen: list[Notification] = []

        with pytest.raises(UsageError) as caught:
            await observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="usage probe",
                deadline=deadline,
                observe=seen.append,
                observe_before_send=reject_premature_usage,
            )
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is UsageFailure.PREMATURE_SNAPSHOT
        assert seen == []
        assert remaining == b""


@pytest.mark.parametrize("usage_turn", [TURN, "other-turn"])
@pytest.mark.asyncio
async def test_codex_usage_after_send_before_reply_needs_correlated_terminal(
    usage_turn: str,
) -> None:
    async with wire() as connection:
        usage = TurnUsage(THREAD)

        def accept(notification: Notification) -> None:
            decoded = decode_usage(notification)
            if decoded is not None:
                usage.accept(decoded)

        observation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="usage probe",
                deadline=future_deadline(),
                observe=accept,
                observe_before_send=reject_premature_usage,
            )
        )
        request = await connection.peer_reader.readline()

        await connection.send(
            usage_frame(usage_turn),
            b'{"id":1,"result":{"turn":{"id":"started-turn","status":"inProgress"}}}\n',
            b'{"method":"turn/completed","params":{"threadId":"selected-thread","turn":{"id":"started-turn","status":"completed"}}}\n',
        )
        terminal = await observation
        if usage_turn == TURN:
            evidence = usage.for_turn(terminal.turn_id)
            assert evidence == snapshot(FIRST)
        else:
            with pytest.raises(UsageError) as caught:
                usage.for_turn(terminal.turn_id)
            assert caught.value.reason is UsageFailure.IDENTITY_MISMATCH

        assert json.loads(request)["method"] == "turn/start"
        assert terminal.turn_id == TURN
