from enum import StrEnum

from pydantic import ValidationError

from kora.ir.structured_json import (
    MAX_JSON_BYTES,
    StructuredOutputValidationError,
    decode_json,
)
from kora.plugins.harness.codex.app_server_turn import TurnCompletion
from kora.protocols.harness import OutputCandidate


class OutputFailure(StrEnum):
    INVALID_JSON = "output_invalid_json"
    IDENTITY_MISMATCH = "output_identity_mismatch"
    TEXT_LIMIT = "output_text_limit"


class OutputRefused(Exception):
    def __init__(self, reason: OutputFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


def output_candidate(
    completion: TurnCompletion, *, thread_id: str
) -> OutputCandidate | None:
    message = completion.final_message
    if completion.status != "completed" or message is None:
        return None
    if message.thread_id != thread_id or message.turn_id != completion.turn_id:
        raise OutputRefused(OutputFailure.IDENTITY_MISMATCH)
    if len(message.text) > MAX_JSON_BYTES:
        raise OutputRefused(OutputFailure.TEXT_LIMIT)
    try:
        payload = message.text.encode("utf-8")
    except UnicodeEncodeError:
        raise OutputRefused(OutputFailure.INVALID_JSON) from None
    if len(payload) > MAX_JSON_BYTES:
        raise OutputRefused(OutputFailure.TEXT_LIMIT)
    try:
        value = decode_json(payload)
        return OutputCandidate(source="result_text", value=value)
    except (StructuredOutputValidationError, ValidationError):
        raise OutputRefused(OutputFailure.INVALID_JSON) from None
