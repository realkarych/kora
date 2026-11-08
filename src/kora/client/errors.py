from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from kora.ir.json import JsonObject


class Outcome(StrEnum):
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


class Failure(StrEnum):
    CONNECTION = "connection"
    TLS = "tls"
    POOL = "pool"
    DESTINATION_POLICY = "destination_policy"
    PROXY_POLICY = "proxy_policy"
    DEADLINE = "deadline"
    STATUS_ENVELOPE = "status_envelope"
    JSON = "json"
    SCHEMA = "schema"
    RESPONSE_LIMIT = "response_limit"
    STREAM = "stream"


class ClientDetails(TypedDict):
    reason: Failure
    outcome: Outcome
    recovery_hint: str


class ClientErrorRecord(TypedDict):
    origin: Literal["client"]
    code: str
    message: str
    location: None
    retryable: Literal[False]
    details: ClientDetails
    request_id: None


class ClientErrorEnvelope(TypedDict):
    error: ClientErrorRecord


MESSAGES: dict[str, str] = {
    "CLT001": "The API connection could not be established.",
    "CLT002": "The API request deadline expired.",
    "CLT003": "The API response was invalid.",
    "CLT004": "The API response stream ended unexpectedly.",
}
CODES: dict[Failure, str] = {
    Failure.CONNECTION: "CLT001",
    Failure.TLS: "CLT001",
    Failure.POOL: "CLT001",
    Failure.DESTINATION_POLICY: "CLT001",
    Failure.PROXY_POLICY: "CLT001",
    Failure.DEADLINE: "CLT002",
    Failure.STATUS_ENVELOPE: "CLT003",
    Failure.JSON: "CLT003",
    Failure.SCHEMA: "CLT003",
    Failure.RESPONSE_LIMIT: "CLT003",
    Failure.STREAM: "CLT004",
}
UNKNOWN_HINT: str = (
    "Inspect authoritative state and any remaining local output "
    "before retrying explicitly."
)
CONNECTION_HINT: str = (
    "Check connection settings without weakening credential transport policy."
)
CONTRACT_HINT: str = (
    "Check the request and client/API contract before trying again explicitly."
)


@dataclass(frozen=True)
class Correlation:
    request_id: str | None = None
    replayed_request_id: str | None = None


NO_CORRELATION = Correlation()


class ClientError(Exception):
    def __init__(
        self,
        reason: Failure,
        outcome: Outcome = Outcome.UNKNOWN,
        correlation: Correlation = NO_CORRELATION,
    ) -> None:
        self.reason = reason
        self.outcome = outcome
        self.correlation = correlation
        self.code = CODES[reason]
        super().__init__(MESSAGES[self.code])

    def envelope(self) -> ClientErrorEnvelope:
        hint = UNKNOWN_HINT
        if self.outcome is Outcome.NOT_SENT:
            hint = CONNECTION_HINT if self.code == "CLT001" else CONTRACT_HINT
        return {
            "error": {
                "origin": "client",
                "code": self.code,
                "message": MESSAGES[self.code],
                "location": None,
                "retryable": False,
                "details": {
                    "reason": self.reason,
                    "outcome": self.outcome,
                    "recovery_hint": hint,
                },
                "request_id": None,
            }
        }


class ApiErrorFields(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    code: str = Field(pattern=r"^(?:API|IR|DSL|RT|POL)[0-9]{3}$")
    message: str = Field(min_length=1, max_length=1024)
    location: None
    retryable: bool
    details: dict[str, object]
    request_id: str = Field(min_length=1, max_length=256)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        body: JsonObject,
        content: bytes,
        correlation: Correlation,
    ) -> None:
        self.status = status
        self.code = code
        self.body = body
        self.content = content
        self.correlation = correlation
        super().__init__(f"The API returned an error ({status}).")
