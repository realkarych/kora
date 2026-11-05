import json
import traceback
from collections.abc import Callable

import pytest

from kora.plugins.harness.codex.app_server_errors import (
    ERROR_METHOD,
    ErrorObservation,
    NativeErrorCode,
    NativeErrorFailure,
    NativeErrorInfo,
    NativeErrorRefused,
    TurnErrors,
    decode_error,
    decode_error_info,
    reject_premature_error,
)
from kora.plugins.harness.codex.app_server_rpc import Notification
from kora.plugins.harness.codex.app_server_turn import observe_app_server_turn
from tests.plugins.harness.codex.probe import report_connection_failure
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

THREAD = "selected-thread"
TURN = "started-turn"
CANARY = "private-error-wire-canary"
HTTP_VARIANTS: tuple[tuple[str, NativeErrorCode], ...] = (
    ("httpConnectionFailed", NativeErrorCode.HTTP_CONNECTION_FAILED),
    (
        "responseStreamConnectionFailed",
        NativeErrorCode.RESPONSE_STREAM_CONNECTION_FAILED,
    ),
    ("responseStreamDisconnected", NativeErrorCode.RESPONSE_STREAM_DISCONNECTED),
    (
        "responseTooManyFailedAttempts",
        NativeErrorCode.RESPONSE_TOO_MANY_FAILED_ATTEMPTS,
    ),
)


def detail(code: object) -> dict[str, object]:
    return {
        "message": CANARY,
        "additionalDetails": CANARY,
        "misalignment": {"detailedExplanation": CANARY},
        "codexErrorInfo": code,
    }


def notice(
    code: object = "unauthorized",
    *,
    thread_id: object = THREAD,
    turn_id: object = TURN,
    will_retry: object = False,
) -> Notification:
    return Notification(
        ERROR_METHOD,
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "willRetry": will_retry,
            "error": detail(code),
        },
        123,
    )


def error_frame(*, turn_id: str = TURN, will_retry: bool = False) -> bytes:
    notification = notice(turn_id=turn_id, will_retry=will_retry)
    return (
        json.dumps({"method": ERROR_METHOD, "params": notification.params}).encode()
        + b"\n"
    )


def observe_errors(errors: TurnErrors) -> Callable[[Notification], None]:
    def observe(notification: Notification) -> None:
        errors.accept(notification)

    return observe


@pytest.mark.parametrize(
    ("wire_code", "expected"),
    [
        ("contextWindowExceeded", NativeErrorCode.CONTEXT_WINDOW_EXCEEDED),
        ("sessionBudgetExceeded", NativeErrorCode.SESSION_BUDGET_EXCEEDED),
        ("usageLimitExceeded", NativeErrorCode.USAGE_LIMIT_EXCEEDED),
        ("rateLimitExceeded", NativeErrorCode.RATE_LIMIT_EXCEEDED),
        ("flexUnavailable", NativeErrorCode.FLEX_UNAVAILABLE),
        ("serverOverloaded", NativeErrorCode.SERVER_OVERLOADED),
        ("modelNotFound", NativeErrorCode.MODEL_NOT_FOUND),
        ("policyCapacityUnavailable", NativeErrorCode.POLICY_CAPACITY_UNAVAILABLE),
        ("cyberPolicy", NativeErrorCode.CYBER_POLICY),
        ("misalignmentPolicyViolation", NativeErrorCode.MISALIGNMENT_POLICY_VIOLATION),
        ("tooManyDenials", NativeErrorCode.TOO_MANY_DENIALS),
        ("internalServerError", NativeErrorCode.INTERNAL_SERVER_ERROR),
        ("unauthorized", NativeErrorCode.UNAUTHORIZED),
        ("badRequest", NativeErrorCode.BAD_REQUEST),
        ("threadRollbackFailed", NativeErrorCode.THREAD_ROLLBACK_FAILED),
        ("sandboxError", NativeErrorCode.SANDBOX_ERROR),
        ("other", NativeErrorCode.OTHER),
    ],
)
def test_codex_error_decodes_pinned_unit_variants(
    wire_code: str, expected: NativeErrorCode
) -> None:
    native = notice(wire_code)

    observed = decode_error(native)

    assert observed == ErrorObservation(THREAD, TURN, False, NativeErrorInfo(expected))


@pytest.mark.parametrize(("variant", "expected"), HTTP_VARIANTS)
@pytest.mark.parametrize("status", [None, 0, 401, 65_535])
def test_codex_error_preserves_native_http_domain(
    variant: str, expected: NativeErrorCode, status: int | None
) -> None:
    native = detail({variant: {"httpStatusCode": status}})

    observed = decode_error_info(native)

    assert observed == NativeErrorInfo(expected, http_status_code=status)


@pytest.mark.parametrize(("variant", "expected"), HTTP_VARIANTS)
def test_codex_error_missing_http_status_stays_unknown(
    variant: str, expected: NativeErrorCode
) -> None:
    native = detail({variant: {}})

    observed = decode_error_info(native)

    assert observed == NativeErrorInfo(expected)


@pytest.mark.parametrize("turn_kind", ["review", "compact"])
def test_codex_error_preserves_nonsteerable_turn_kind(turn_kind: str) -> None:
    native = detail({"activeTurnNotSteerable": {"turnKind": turn_kind}})

    observed = decode_error_info(native)

    assert observed.code is NativeErrorCode.ACTIVE_TURN_NOT_STEERABLE
    assert observed.turn_kind == turn_kind
    assert observed.http_status_code is None


@pytest.mark.parametrize(
    "unknown", [CANARY, {CANARY: {"content": CANARY}}, "unspecified", "unrecognized"]
)
def test_codex_error_unknown_is_detached_and_distinct_from_missing(
    unknown: object,
) -> None:
    missing: dict[str, object] = {"message": CANARY}

    missing_info = decode_error_info(missing)
    null_info = decode_error_info(detail(None))
    unknown_info = decode_error_info(detail(unknown))

    assert missing_info == null_info == NativeErrorInfo(NativeErrorCode.UNSPECIFIED)
    assert unknown_info == NativeErrorInfo(NativeErrorCode.UNRECOGNIZED)
    assert CANARY not in repr(missing_info) + repr(unknown_info)


@pytest.mark.parametrize(
    "invalid",
    [
        True,
        401,
        [],
        {},
        {"future": {}, "another": {}},
        {"unauthorized": {}},
        "httpConnectionFailed",
        "activeTurnNotSteerable",
        {"httpConnectionFailed": None},
        {"httpConnectionFailed": {}, "future": {}},
        {"httpConnectionFailed": {"httpStatusCode": True}},
        {"httpConnectionFailed": {"httpStatusCode": -1}},
        {"httpConnectionFailed": {"httpStatusCode": 65_536}},
        {"httpConnectionFailed": {"httpStatusCode": 401.0}},
        {"httpConnectionFailed": {"httpStatusCode": CANARY}},
        {"activeTurnNotSteerable": {}},
        {"activeTurnNotSteerable": {"turnKind": None}},
        {"activeTurnNotSteerable": {"turnKind": "future"}},
    ],
)
def test_codex_error_refuses_malformed_known_details(invalid: object) -> None:
    native = detail(invalid)

    with pytest.raises(NativeErrorRefused) as caught:
        decode_error_info(native)

    assert caught.value.reason is NativeErrorFailure.INVALID_DETAIL
    assert CANARY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("invalid", [None, [], {}, {"message": 1}, {"message": None}])
def test_codex_error_requires_native_error_object_and_message(invalid: object) -> None:
    with pytest.raises(NativeErrorRefused) as caught:
        decode_error_info(invalid)

    assert caught.value.reason is NativeErrorFailure.INVALID_DETAIL


@pytest.mark.parametrize("invalid", [None, "", 1, True, "\ud800"])
@pytest.mark.parametrize("field", ["thread_id", "turn_id"])
def test_codex_error_refuses_invalid_identities(field: str, invalid: object) -> None:
    native = notice(**{field: invalid})

    with pytest.raises(NativeErrorRefused) as caught:
        decode_error(native)

    assert caught.value.reason is NativeErrorFailure.INVALID_NOTIFICATION


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", CANARY])
def test_codex_error_retry_flag_is_strict(invalid: object) -> None:
    native = notice(will_retry=invalid)

    with pytest.raises(NativeErrorRefused) as caught:
        decode_error(native)

    assert caught.value.reason is NativeErrorFailure.INVALID_NOTIFICATION


def test_codex_error_drops_content_and_does_not_alias_input() -> None:
    payload: dict[str, object] = {"httpStatusCode": 401}
    native = notice({"httpConnectionFailed": payload}, thread_id=CANARY)
    decoded = decode_error(native)
    assert decoded is not None

    payload["httpStatusCode"] = 429
    replayed = decode_error(native)

    assert decoded.info == NativeErrorInfo(NativeErrorCode.HTTP_CONNECTION_FAILED, 401)
    assert replayed is not None
    assert replayed.info.http_status_code == 429
    assert CANARY not in repr(decoded) + repr(decoded.info)


def test_codex_error_failure_diagnostics_and_trace_are_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    native = notice({"httpConnectionFailed": {"httpStatusCode": CANARY}})

    with pytest.raises(NativeErrorRefused) as caught:
        decode_error(native)
    exit_status = report_connection_failure(caught.value)
    output = capsys.readouterr()

    assert exit_status == 1
    assert output.out == ""
    assert json.loads(output.err) == {
        "level": "ERROR",
        "event": "probe.case_failed",
        "detail": NativeErrorFailure.INVALID_DETAIL.value,
    }
    assert CANARY not in output.err + "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "foreign", [notice(thread_id="other-thread"), notice(turn_id="other-turn")]
)
def test_codex_error_wrong_identity_does_not_replace_latest(
    foreign: Notification,
) -> None:
    errors = TurnErrors(THREAD)
    original = errors.accept(notice(will_retry=True))

    with pytest.raises(NativeErrorRefused) as caught:
        errors.accept(foreign)
    retained = errors.for_turn(TURN)

    assert caught.value.reason is NativeErrorFailure.IDENTITY_MISMATCH
    assert retained is original


def test_codex_error_replaces_one_latest_notice_and_ignores_unrelated() -> None:
    errors = TurnErrors(THREAD)
    initial = errors.for_turn(TURN)
    first = errors.accept(notice("rateLimitExceeded", will_retry=True))

    ignored = errors.accept(Notification("item/started", None, None))
    last = errors.accept(notice("other"))
    retained = errors.for_turn(TURN)

    assert initial is ignored is None
    assert first is not None
    assert first.will_retry
    assert last is retained
    assert last is not None
    assert not last.will_retry
    assert last.info.code is NativeErrorCode.OTHER


@pytest.mark.asyncio
async def test_codex_error_read_ahead_refuses_without_sending_turn() -> None:
    async with wire() as connection:
        deadline = future_deadline()
        setup = connection.start(
            connection.rpc.call(
                "thread/start", {}, deadline=deadline, observe=lambda _: None
            )
        )
        await connection.peer_reader.readline()
        raw = error_frame()
        await connection.send(b'{"id":1,"result":null}\n' + raw[:12])
        assert await setup is None
        await connection.send(raw[12:])
        errors = TurnErrors(THREAD)

        with pytest.raises(NativeErrorRefused) as caught:
            await observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="error probe",
                deadline=deadline,
                observe=observe_errors(errors),
                observe_before_send=reject_premature_error,
            )
        connection.writer.write_eof()
        remaining = await connection.peer_reader.read()

        assert caught.value.reason is NativeErrorFailure.PREMATURE_ERROR
        assert errors.for_turn(TURN) is None
        assert remaining == b""


@pytest.mark.parametrize("error_turn", [TURN, "other-turn"])
@pytest.mark.asyncio
async def test_codex_error_early_notice_requires_acknowledged_turn(
    error_turn: str,
) -> None:
    async with wire() as connection:
        errors = TurnErrors(THREAD)
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="error probe",
                deadline=future_deadline(),
                observe=observe_errors(errors),
                observe_before_send=reject_premature_error,
            )
        )
        request = await connection.peer_reader.readline()

        await connection.send(
            error_frame(turn_id=error_turn),
            b'{"id":1,"result":{"turn":{"id":"started-turn","status":"inProgress"}}}\n',
            b'{"method":"turn/completed","params":{"threadId":"selected-thread","turn":{"id":"started-turn","status":"failed","error":{"message":"failed","codexErrorInfo":"unauthorized"}}}}\n',
        )
        terminal = await operation

        assert terminal.status == "failed"
        assert json.loads(request)["method"] == "turn/start"
        if error_turn == TURN:
            observed = errors.for_turn(terminal.turn_id)
            assert observed is not None
            assert observed.info == decode_error_info(terminal.error)
        else:
            with pytest.raises(NativeErrorRefused) as caught:
                errors.for_turn(terminal.turn_id)
            assert caught.value.reason is NativeErrorFailure.IDENTITY_MISMATCH


@pytest.mark.asyncio
async def test_codex_error_retry_notice_does_not_override_completed_turn() -> None:
    async with wire() as connection:
        errors = TurnErrors(THREAD)
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="error probe",
                deadline=future_deadline(),
                observe=observe_errors(errors),
            )
        )
        await connection.peer_reader.readline()

        await connection.send(
            error_frame(will_retry=True),
            b'{"id":1,"result":{"turn":{"id":"started-turn","status":"inProgress"}}}\n',
            b'{"method":"turn/completed","params":{"threadId":"selected-thread","turn":{"id":"started-turn","status":"completed"}}}\n',
        )
        terminal = await operation
        observed = errors.for_turn(terminal.turn_id)

        assert terminal.status == "completed"
        assert terminal.error is None
        assert observed is not None
        assert observed.will_retry


@pytest.mark.asyncio
async def test_codex_error_late_notice_is_observed_only_by_explicit_owner_read() -> (
    None
):
    async with wire() as connection:
        errors = TurnErrors(THREAD)
        operation = connection.start(
            observe_app_server_turn(
                connection.rpc,
                thread_id=THREAD,
                prompt="error probe",
                deadline=future_deadline(),
                observe=observe_errors(errors),
            )
        )
        await connection.peer_reader.readline()
        await connection.send(
            b'{"id":1,"result":{"turn":{"id":"started-turn","status":"inProgress"}}}\n',
            b'{"method":"turn/completed","params":{"threadId":"selected-thread","turn":{"id":"started-turn","status":"completed"}}}\n',
            error_frame(),
        )
        terminal = await operation
        before = errors.for_turn(terminal.turn_id)

        await connection.rpc.receive(
            deadline=future_deadline(), observe=observe_errors(errors)
        )
        after = errors.for_turn(terminal.turn_id)

        assert before is None
        assert terminal.status == "completed"
        assert after is not None
        assert not after.will_retry
