from dataclasses import dataclass, field
from hashlib import sha256
from typing import cast

from kora.ir.agent_session_validation import (
    MAX_TRANSCRIPT_BYTES,
    TRUNCATION_MARKER,
    decode,
    refuse,
    validate,
)
from kora.ir.agent_session_validation import (
    AgentSessionValidationError as AgentSessionValidationError,
)
from kora.ir.json import JsonObject
from kora.ir.structured_json import encode_snapshot
from kora.ir.structured_output import same_descriptor


@dataclass(frozen=True, slots=True, init=False)
class AgentSession:
    _canonical: bytes = field(repr=False)

    def __init__(self, value: object) -> None:
        canonical = encode_snapshot(validate(value))
        object.__setattr__(self, "_canonical", canonical)

    @property
    def value(self) -> JsonObject:
        return checked_value(self)

    def canonical_bytes(self) -> bytes:
        checked_value(self)
        return self._canonical


def checked_value(value: AgentSession) -> JsonObject:
    if type(value) is not AgentSession:
        refuse()
    raw: object = getattr(value, "_canonical", None)
    if type(raw) is not bytes:
        refuse()
    result = validate(decode(raw))
    if encode_snapshot(result) != raw:
        refuse()
    return result


def parse_agent_session(value: object) -> AgentSession:
    if type(value) is AgentSession:
        return AgentSession(checked_value(value))
    return AgentSession(value)


def parse_agent_session_json(payload: bytes) -> AgentSession:
    return AgentSession(decode(payload))


def canonical_agent_session(value: AgentSession) -> bytes:
    return AgentSession.canonical_bytes(value)


def agent_session_digest(value: AgentSession) -> str:
    return sha256(canonical_agent_session(value)).hexdigest()


def summarize_agent_session(value: AgentSession) -> JsonObject:
    metadata = cast("JsonObject", checked_value(value)["metadata"])
    result: JsonObject = {key: metadata[key] for key in SUMMARY_FIELDS}
    result["usage"] = [
        {key: row[key] for key in SUMMARY_USAGE_FIELDS}
        for row in cast("list[JsonObject]", metadata["usage"])
    ]
    coverage = cast("JsonObject", metadata["usage_coverage"])
    result["usage_coverage"] = {key: coverage[key] for key in SUMMARY_COVERAGE_FIELDS}
    return result


def verify_agent_session_transcript(value: AgentSession, payload: bytes) -> None:
    if type(payload) is not bytes or len(payload) > MAX_TRANSCRIPT_BYTES:
        refuse("AS006")
    entry = cast("JsonObject", checked_value(value)["transcript"])
    if (
        entry["capture"] != "captured"
        or len(payload) != entry["retained_size"]
        or sha256(payload).hexdigest() != entry["retained_sha256"]
        or entry["truncated"]
        and not payload.endswith(TRUNCATION_MARKER)
    ):
        refuse("AS006")


SUMMARY_FIELDS: tuple[str, ...] = (
    "harness",
    "installed_version",
    "session_id",
    "billing_class",
    "turns",
    "duration_ms",
    "stop_reason",
    "provenance",
)
SUMMARY_USAGE_FIELDS: tuple[str, ...] = (
    "model",
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
    "known_token_fields",
    "cache_creation_by_ttl",
    "completeness",
    "basis",
    "billing_class",
    "delta_of_session_totals",
    "usd_estimate",
)
SUMMARY_COVERAGE_FIELDS: tuple[str, ...] = (
    "completeness",
    "attribution_gap",
    "observation_gap",
)

_DESCRIPTOR: JsonObject = {
    "name": "AgentSession",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:AgentSession",
    "parent": None,
    "representation": "kora.agent-session-transcript-primary.v1",
    "behavior": {
        "validation": "kora.agent-session-validation.v1",
        "digest_fields": "kora.agent-session-logical.v1",
        "summary": "kora.agent-session-summary.v1",
    },
    "canonicalization": "kora.structured-json.v1",
    "validation": {
        "max_raw_logical_json_bytes": 4194304,
        "max_canonical_logical_json_bytes": 4194304,
        "max_json_depth": 16,
    },
    "digest_selection": [""],
    "logical_identity": "kora.agent-session-logical.v1",
    "compatibility": {
        "execution_semantics": "kora.execution.v1",
        "checker_view": "kora.agent-session-view.v1",
    },
    "schema_sha256": "d7a63554c3c50b9237a118f788ffd4a0f0af262bc192c2301db568f7c902e55a",
    "cel_policy_sha256": (
        "8522225273b2696446b698c3caaab6b9a382a3f8a842c3277da18a1199d7ff30"
    ),
}


def require_agent_session_revision(descriptor: object) -> None:
    if not same_descriptor(descriptor, _DESCRIPTOR):
        refuse("AS005")
