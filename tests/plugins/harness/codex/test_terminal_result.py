import asyncio

import pytest

from kora.conformance.harness import ConformanceSink, Trace
from kora.ir.node import ErrorClass
from kora.plugins.harness.codex.app_server_errors import (
    NativeErrorCode,
    NativeErrorFailure,
    NativeErrorInfo,
    NativeErrorRefused,
)
from kora.plugins.harness.codex.app_server_output import OutputFailure, OutputRefused
from kora.plugins.harness.codex.app_server_rpc import RpcError, RpcFailure
from kora.plugins.harness.codex.app_server_session import (
    AppServerTurnSession,
    TurnObservations,
)
from kora.plugins.harness.codex.app_server_turn import (
    TerminalStatus,
    TurnError,
    TurnFailure,
)
from kora.plugins.harness.codex.terminal_result import (
    OutputDiagnostic,
    TerminalResult,
    project_terminal_result,
)
from kora.protocols.harness import ResultEvent
from tests.plugins.harness.codex.test_app_server_errors import CANARY, detail, notice
from tests.plugins.harness.codex.test_app_server_messages import encoded, message
from tests.plugins.harness.codex.test_app_server_output import (
    item,
    terminal_frame,
    terminal_params,
)
from tests.plugins.harness.codex.test_app_server_turn import (
    PROMPT,
    THREAD,
    completed,
    started,
)
from tests.plugins.harness.codex.wire_fixture import future_deadline, wire

OUTPUT_CANARY = "private-terminal-output-canary"


async def project(
    *terminal_frames: bytes, output_required: bool = True
) -> TerminalResult | None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await connection.peer_reader.readline()
        await connection.send(started(), *terminal_frames)
        await operation
        return project_terminal_result(
            session.snapshot(), thread_id=THREAD, output_required=output_required
        )


def http_error(
    status: int, variant: str = "responseTooManyFailedAttempts"
) -> dict[str, object]:
    return detail({variant: {"httpStatusCode": status}})


@pytest.mark.parametrize(
    ("error", "expected_class", "reason", "http_status"),
    [
        (None, ErrorClass.HARNESS_ERROR, "missing", None),
        (detail(None), ErrorClass.HARNESS_ERROR, "unspecified", None),
        (detail(CANARY), ErrorClass.HARNESS_ERROR, "unrecognized", None),
        (
            detail("usageLimitExceeded"),
            ErrorClass.PROVIDER_LIMITED,
            "usage_limit_exceeded",
            None,
        ),
        (
            detail("rateLimitExceeded"),
            ErrorClass.PROVIDER_LIMITED,
            "rate_limit_exceeded",
            None,
        ),
        (detail("unauthorized"), ErrorClass.CONFIG_ERROR, "unauthorized", None),
        (
            detail("serverOverloaded"),
            ErrorClass.HARNESS_ERROR,
            "server_overloaded",
            None,
        ),
        (
            http_error(401, "httpConnectionFailed"),
            ErrorClass.HARNESS_ERROR,
            "http_connection_failed",
            401,
        ),
        (
            http_error(503, "httpConnectionFailed"),
            ErrorClass.HARNESS_ERROR,
            "http_connection_failed",
            503,
        ),
        *[
            (
                http_error(status),
                ErrorClass.HARNESS_ERROR,
                "response_too_many_failed_attempts",
                shared,
            )
            for status, shared in [
                (99, None),
                (100, 100),
                (429, 429),
                (599, 599),
                (600, None),
            ]
        ],
    ],
    ids=[
        "missing",
        "unspecified",
        "unknown",
        "usage-limit",
        "rate-limit",
        "auth",
        "overload",
        "401",
        "503",
        "below",
        "lower",
        "429",
        "upper",
        "above",
    ],
)
@pytest.mark.asyncio
async def test_only_closed_terminal_failure_data_crosses_into_the_result_event(
    error: object,
    expected_class: ErrorClass,
    reason: str,
    http_status: int | None,
) -> None:
    terminal = completed(status="failed", error=error)
    sink = ConformanceSink(None, Trace())

    result = await project(terminal)
    assert result is not None
    event_payload = result.data.model_dump(mode="json")
    sink.emit("result", event_payload)

    assert result.failure_class is expected_class
    assert result.http_status == http_status
    assert result.output is None
    assert result.output_diagnostic is None
    assert event_payload == {
        "subtype": "codex_failed",
        "is_error": True,
        "terminal_reason": f"codex_error_{reason}",
    }
    (event,) = sink.events
    assert isinstance(event, ResultEvent)
    assert event.data == result.data
    assert CANARY not in repr((result, event))


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.asyncio
async def test_earlier_limit_retry_does_not_classify_a_later_terminal(
    status: TerminalStatus,
) -> None:
    retry = encoded(notice("usageLimitExceeded", will_retry=True))
    error = None if status == "completed" else http_error(429)
    terminal = completed(status=status, error=error)
    expected_class = None if status == "completed" else ErrorClass.HARNESS_ERROR

    result = await project(retry, terminal, output_required=False)

    assert result is not None
    assert result.data.subtype == f"codex_{status}"
    assert result.failure_class is expected_class
    assert result.data.terminal_reason == (
        None
        if status == "completed"
        else "codex_error_response_too_many_failed_attempts"
    )


@pytest.mark.parametrize("has_error", [False, True])
@pytest.mark.asyncio
async def test_interrupted_terminal_has_no_attributed_runner_cause(
    has_error: bool,
) -> None:
    error = detail("rateLimitExceeded") if has_error else None
    terminal = completed(status="interrupted", error=error)

    result = await project(terminal)

    assert result is not None
    assert result.data.subtype == "codex_interrupted"
    assert not result.data.is_error
    assert result.data.terminal_reason == (
        "codex_error_rate_limit_exceeded" if has_error else None
    )
    assert result.failure_class is None
    assert result.output is None
    assert result.output_diagnostic is None


@pytest.mark.asyncio
async def test_eof_after_acknowledged_error_notice_does_not_create_a_result() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        observed = asyncio.Event()
        retry = encoded(notice("usageLimitExceeded", will_retry=True))
        operation = connection.start(
            session.run(
                connection.rpc,
                prompt=PROMPT,
                deadline=future_deadline(),
                observe=lambda _notice: observed.set(),
            )
        )
        await connection.peer_reader.readline()

        await connection.send(started(), retry)
        await observed.wait()
        connection.peer_writer.close()
        await connection.peer_writer.wait_closed()
        with pytest.raises(RpcError) as caught:
            await operation
        state = session.snapshot()
        result = project_terminal_result(state, thread_id=THREAD, output_required=True)

        assert caught.value.reason is RpcFailure.DISCONNECTED
        assert state.latest_error is not None
        assert state.latest_error.will_retry
        assert state.terminal_status is None
        assert result is None


@pytest.mark.parametrize(
    ("text", "required", "has_candidate", "value", "diagnostic"),
    [
        (f'{{"value":"{OUTPUT_CANARY}"}}', True, True, {"value": OUTPUT_CANARY}, None),
        ("null", True, True, None, None),
        (f"{OUTPUT_CANARY}: invalid", True, False, None, OutputFailure.INVALID_JSON),
        (f"{OUTPUT_CANARY}: invalid", False, False, None, None),
    ],
    ids=["valid", "null", "invalid", "unselected"],
)
@pytest.mark.asyncio
async def test_completed_result_keeps_output_separate_and_only_decodes_when_selected(
    text: str,
    required: bool,
    has_candidate: bool,
    value: object,
    diagnostic: OutputDiagnostic | None,
) -> None:
    terminal = terminal_frame(terminal_params([item(text)]))

    result = await project(terminal, output_required=required)

    assert result is not None
    assert result.data.subtype == "codex_completed"
    assert not result.data.is_error
    assert result.data.terminal_reason is None
    assert result.failure_class is None
    assert result.output_diagnostic == diagnostic
    assert (result.output is not None) is has_candidate
    if result.output is not None:
        assert result.output.source == "result_text"
        assert result.output.value == value
    assert OUTPUT_CANARY not in repr((result, result.data, result.output_diagnostic))


@pytest.mark.asyncio
async def test_no_terminal_summary_does_not_fall_back_to_streamed_message() -> None:
    earlier = encoded(message(text="null"))
    terminal = completed()

    result = await project(earlier, terminal)

    assert result is not None
    assert result.data.subtype == "codex_completed"
    assert result.output is None
    assert result.output_diagnostic == "output_missing"


@pytest.mark.asyncio
async def test_malformed_terminal_summary_refuses_even_when_output_is_unselected() -> (
    None
):
    terminal = terminal_frame(terminal_params([item({"private": OUTPUT_CANARY})]))

    with pytest.raises(TurnError) as caught:
        await project(terminal, output_required=False)

    assert caught.value.reason is TurnFailure.INVALID_COMPLETION
    assert OUTPUT_CANARY not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_terminal_error_does_not_produce_a_generic_result() -> None:
    terminal = completed(status="failed", error=detail("httpConnectionFailed"))

    with pytest.raises(NativeErrorRefused) as caught:
        await project(terminal)

    assert caught.value.reason is NativeErrorFailure.INVALID_DETAIL
    assert CANARY not in str(caught.value)


@pytest.mark.asyncio
async def test_terminal_output_identity_refusal_cannot_become_missing_output() -> None:
    async with wire() as connection:
        session = AppServerTurnSession(THREAD, max_messages=2, max_tools=16)
        terminal = terminal_frame(terminal_params([item("null")]))
        operation = connection.start(
            session.run(connection.rpc, prompt=PROMPT, deadline=future_deadline())
        )
        await connection.peer_reader.readline()

        await connection.send(started(), terminal)
        await operation
        with pytest.raises(OutputRefused) as caught:
            project_terminal_result(
                session.snapshot(), thread_id="different-thread", output_required=True
            )

        assert caught.value.reason is OutputFailure.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "turn_id", [None, "started-turn"], ids=["missing-id", "completed-error"]
)
def test_inconsistent_terminal_evidence_refuses(turn_id: str | None) -> None:
    observations = TurnObservations(
        turn_id=turn_id,
        terminal_status="completed",
        terminal_error=NativeErrorInfo(NativeErrorCode.UNAUTHORIZED)
        if turn_id is not None
        else None,
    )

    with pytest.raises(TurnError) as caught:
        project_terminal_result(observations, thread_id=THREAD, output_required=True)

    assert caught.value.reason is TurnFailure.INVALID_COMPLETION
