from dataclasses import dataclass
from typing import Literal

from kora.ir.node import ErrorClass
from kora.plugins.harness.codex.app_server_errors import (
    NativeErrorCode,
    NativeErrorInfo,
)
from kora.plugins.harness.codex.app_server_messages import CompletedMessage
from kora.plugins.harness.codex.app_server_output import (
    OutputFailure,
    OutputRefused,
    final_message_candidate,
)
from kora.plugins.harness.codex.app_server_session import TurnObservations
from kora.plugins.harness.codex.app_server_turn import TurnError, TurnFailure
from kora.protocols.harness import OutputCandidate, ResultData

OutputDiagnostic = Literal[
    "output_missing", OutputFailure.INVALID_JSON, OutputFailure.TEXT_LIMIT
]


@dataclass(frozen=True, repr=False)
class TerminalResult:
    data: ResultData
    failure_class: ErrorClass | None
    http_status: int | None
    output: OutputCandidate | None
    output_diagnostic: OutputDiagnostic | None


def _failure_class(error: NativeErrorInfo | None) -> ErrorClass:
    if error is not None:
        if error.code in (
            NativeErrorCode.USAGE_LIMIT_EXCEEDED,
            NativeErrorCode.RATE_LIMIT_EXCEEDED,
        ):
            return ErrorClass.PROVIDER_LIMITED
        if error.code is NativeErrorCode.UNAUTHORIZED:
            return ErrorClass.CONFIG_ERROR
    return ErrorClass.HARNESS_ERROR


def _output(
    message: CompletedMessage | None, *, thread_id: str, turn_id: str
) -> tuple[OutputCandidate | None, OutputDiagnostic | None]:
    if message is None:
        return None, "output_missing"
    try:
        return final_message_candidate(
            message, thread_id=thread_id, turn_id=turn_id
        ), None
    except OutputRefused as error:
        if error.reason in (OutputFailure.INVALID_JSON, OutputFailure.TEXT_LIMIT):
            return None, error.reason
        raise


def project_terminal_result(
    observations: TurnObservations, *, thread_id: str, output_required: bool
) -> TerminalResult | None:
    status = observations.terminal_status
    if status is None:
        return None
    turn_id = observations.turn_id
    error = observations.terminal_error
    if not turn_id or (status == "completed" and error is not None):
        raise TurnError(TurnFailure.INVALID_COMPLETION)

    failed = status == "failed"
    terminal_reason = "codex_error_missing" if failed else None
    http_status = None
    if error is not None:
        terminal_reason = f"codex_error_{error.code.name.lower()}"
        native_status = error.http_status_code
        if native_status is not None and 100 <= native_status <= 599:
            http_status = native_status

    candidate, output_diagnostic = (
        _output(observations.final_message, thread_id=thread_id, turn_id=turn_id)
        if status == "completed" and output_required
        else (None, None)
    )
    return TerminalResult(
        data=ResultData(
            subtype=f"codex_{status}",
            is_error=failed,
            terminal_reason=terminal_reason,
        ),
        failure_class=_failure_class(error) if failed else None,
        http_status=http_status,
        output=candidate,
        output_diagnostic=output_diagnostic,
    )
