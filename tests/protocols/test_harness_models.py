import json
from types import MappingProxyType
from typing import Final

import pytest
from pydantic import BaseModel, ValidationError

from kora.ir.errors import safe_field_errors
from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES, JsonValue
from kora.protocols.harness import (
    EVENT_ADAPTER,
    MAX_FILE_NAME_BYTES,
    MAX_MCP_SOURCES,
    MAX_PROMPT_BYTES,
    MAX_SAMPLE_ITEMS,
    MAX_SESSION_FILES,
    MAX_TEXT_BYTES,
    MAX_USAGE_ROWS,
    ErrorData,
    OutputCandidate,
    PermissionDeniedData,
    ProviderLimitedData,
    ProviderRetryingData,
    ResultData,
    SessionResult,
    SessionSpec,
    SessionStartedData,
    ToolCallData,
    ToolResultData,
    TurnData,
    UsageData,
)
from kora.protocols.vcs import WorkspaceRef
from tests.protocols.harness_documents import (
    DENIAL,
    EVENT_DATA,
    REFUSED_MARKER,
    UNCONFIRMED,
    WORKSPACE_REF,
    event,
    nested_list,
    session_file,
    session_result,
    session_spec,
    usage_row,
)

DATA_MODELS: Final[tuple[type[BaseModel], ...]] = (
    SessionStartedData,
    TurnData,
    ToolCallData,
    ToolResultData,
    PermissionDeniedData,
    UsageData,
    ProviderLimitedData,
    ProviderRetryingData,
    ResultData,
    ErrorData,
)
KNOWN: Final[frozenset[str]] = frozenset(
    {"i", "at", "kind", "data", "items", "total", "source", "value"}
    | {name for model in DATA_MODELS for name in model.model_fields}
    | set(SessionResult.model_fields)
)
OVERSIZED_TEXT: Final[str] = REFUSED_MARKER + "x" * MAX_TEXT_BYTES
CACHE_SPLIT_KNOWN: Final[list[str]] = ["output_tokens", "cache_creation_tokens"]
REFUSED_EVENT_KEYS: Final[list[dict[str, object]]] = [
    event("turn", **{REFUSED_MARKER: True}),
    {**event("turn"), REFUSED_MARKER: True},
    event(
        "usage",
        known_token_fields=CACHE_SPLIT_KNOWN,
        cache_creation_tokens=2,
        cache_creation_by_ttl={"5m": 1, REFUSED_MARKER: 1},
    ),
]
REFUSED_EVENTS: Final[list[dict[str, object]]] = [
    {**event("turn"), "data": EVENT_DATA["tool_call"]},
    {**event("usage"), "data": EVENT_DATA["provider_limited"]},
    {**event("turn"), "kind": REFUSED_MARKER},
    {key: value for key, value in event("turn").items() if key != "kind"},
    {**event("turn"), "kind": [REFUSED_MARKER]},
    event("turn", text=OVERSIZED_TEXT),
    event("tool_result", content=OVERSIZED_TEXT),
    event("error", text=OVERSIZED_TEXT),
    event("turn", text="\ud800" + REFUSED_MARKER),
    event("permission_denied", pattern=f"namespace.allow.{REFUSED_MARKER}"),
    event("permission_denied", pattern="layer.deny.0"),
    event("permission_denied", code=REFUSED_MARKER),
    event("permission_denied", source="harness", pattern=None, code="not_allowed"),
    event("permission_denied", source="policy", pattern=None, code="harness_rule"),
    event("tool_call", input={"command": REFUSED_MARKER}),
    event("permission_denied", code="allowed"),
    event("permission_denied", code="not_allowed"),
    event("permission_denied", pattern="namespace.allow.0"),
    event("permission_denied", pattern="node.deny.007"),
    event("permission_denied", pattern="node.deny.10000"),
    event("permission_denied", source="harness", code="harness_rule"),
    event("usage", reasoning_tokens=2, output_tokens=1),
    event(
        "usage",
        known_token_fields=CACHE_SPLIT_KNOWN,
        cache_creation_tokens=3,
        cache_creation_by_ttl={"5m": 3},
    ),
    event(
        "usage",
        known_token_fields=CACHE_SPLIT_KNOWN,
        cache_creation_tokens=3,
        cache_creation_by_ttl={"5m": 1, "1h": 1},
    ),
    event("usage", cache_creation_tokens=2, cache_creation_by_ttl={"5m": 1, "1h": 1}),
    event("session_started", mcp_sources=["files"] * (MAX_MCP_SOURCES + 1)),
    event("provider_limited", vendor_group=f"{REFUSED_MARKER} group"),
    event("provider_limited", retry_after=REFUSED_MARKER),
    event("provider_limited", retry_after=1),
    event("provider_retrying", retry_after="2026-10-23T00:00:00"),
    event("result", subtype=REFUSED_MARKER.upper()),
    event("error", code=f"{REFUSED_MARKER} code"),
    event("turn", truncated=1),
]
REFUSED_RESULT_KEYS: Final[list[dict[str, object]]] = [
    session_result(denials={"items": [{**DENIAL, REFUSED_MARKER: True}], "total": 1}),
    session_result(error_summary={"source": "harness", REFUSED_MARKER: True}),
    session_result(**{REFUSED_MARKER: True}),
]
REFUSED_RESULTS: Final[list[dict[str, object]]] = [
    session_result(usage=[usage_row()] * (MAX_USAGE_ROWS + 1)),
    session_result(usage=[usage_row(), usage_row(observation_sequence=2)]),
    session_result(denials={"items": [DENIAL] * (MAX_SAMPLE_ITEMS + 1), "total": 2000}),
    session_result(
        unconfirmed_calls={
            "items": [UNCONFIRMED] * (MAX_SAMPLE_ITEMS + 1),
            "total": 2000,
        }
    ),
    session_result(denials={"items": [DENIAL, DENIAL], "total": 1}),
    session_result(unconfirmed_calls={"items": [UNCONFIRMED], "total": 0}),
    session_result(
        unconfirmed_calls={
            "items": [{**UNCONFIRMED, "tool": REFUSED_MARKER + " tool"}],
            "total": 1,
        }
    ),
    session_result(
        output={"source": "result_text", "value": {"a": [OVERSIZED_TEXT] * 32}}
    ),
    session_result(output={"source": REFUSED_MARKER, "value": None}),
    session_result(api_error_status=600),
    session_result(error_class=REFUSED_MARKER),
    session_result(stop_reason=f"{REFUSED_MARKER} stop"),
]


ACCEPTED_DENIALS: Final[list[dict[str, object]]] = [
    event("permission_denied", pattern=None),
    event("permission_denied", pattern=None, code="protected_path"),
    event("permission_denied", pattern=None, code="not_allowed"),
    event("permission_denied", source="harness", pattern=None, code="harness_rule"),
]
REFUSED_FILE_NAMES: Final[list[str]] = [
    ".",
    "..",
    "a/b",
    "a..b",
    "a\x85b",
    "a\u2028b",
    "a\u202eb",
    "x" * (MAX_FILE_NAME_BYTES + 1),
    "\u00e9" * (MAX_FILE_NAME_BYTES // 2 + 1),
    "\ud800",
    " a.json",
    "a.json ",
    " ",
]
REFUSED_SPECS: Final[list[dict[str, object]]] = [
    session_spec(prompt="x" * (MAX_PROMPT_BYTES + 1)),
    session_spec(prompt="\u00e9" * (MAX_PROMPT_BYTES // 2 + 1)),
    session_spec(prompt="\ud800"),
    session_spec(files=[session_file("a.json")] * (MAX_SESSION_FILES + 1)),
    *(session_spec(files=[session_file(name)]) for name in REFUSED_FILE_NAMES),
    session_spec(files=[session_file("a.json"), session_file("a.json")]),
    session_spec(files=[session_file("a.json"), session_file("A.JSON")]),
    session_spec(files=[session_file("caf\u00e9"), session_file("cafe\u0301")]),
]
REFUSED_ROOTS: Final[list[str]] = ["/workspace/\x85", "/workspace/\u2028", "/a\u202eb"]


MODES: Final[tuple[str, ...]] = ("python", "json")


def event_in(mode: str, document: dict[str, object]) -> object:
    if mode == "json":
        return EVENT_ADAPTER.validate_json(json.dumps(document))
    return EVENT_ADAPTER.validate_python(document)


def result_in(mode: str, document: dict[str, object]) -> object:
    if mode == "json":
        return SessionResult.model_validate_json(json.dumps(document))
    return SessionResult.model_validate(document)


def safe_rendering(error: ValidationError) -> str:
    return repr(safe_field_errors(error, KNOWN, "Invalid harness value."))


@pytest.mark.parametrize("kind", sorted(EVENT_DATA))
def test_every_event_kind_round_trips(kind: str) -> None:
    document = event(kind)

    parsed = EVENT_ADAPTER.validate_python(document)
    restored = EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(parsed))

    assert restored == parsed
    assert EVENT_ADAPTER.dump_python(parsed, mode="json") == document


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("document", REFUSED_EVENTS)
def test_session_event_data_is_closed(mode: str, document: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        event_in(mode, document)

    assert REFUSED_MARKER not in str(raised.value)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("document", REFUSED_EVENT_KEYS)
def test_unknown_event_keys_stay_out_of_safe_errors(
    mode: str, document: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as raised:
        event_in(mode, document)

    assert REFUSED_MARKER not in safe_rendering(raised.value)


@pytest.mark.parametrize("document", ACCEPTED_DENIALS)
def test_denials_follow_the_decision_mapping(document: dict[str, object]) -> None:
    parsed = EVENT_ADAPTER.validate_python(document)

    assert EVENT_ADAPTER.dump_python(parsed, mode="json") == document


def test_usage_events_own_their_cache_split() -> None:
    document = event(
        "usage",
        known_token_fields=CACHE_SPLIT_KNOWN,
        cache_creation_tokens=3,
        cache_creation_by_ttl={"5m": 1, "1h": 2},
    )

    parsed = EVENT_ADAPTER.validate_python(document)

    assert isinstance(parsed.data, UsageData)
    assert isinstance(parsed.data.cache_creation_by_ttl, MappingProxyType)
    assert EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(parsed)) == parsed


@pytest.mark.parametrize("document", REFUSED_SPECS)
def test_session_spec_refuses_values_outside_its_bounds(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SessionSpec.model_validate(document)


@pytest.mark.parametrize("root", REFUSED_ROOTS)
def test_workspace_paths_are_printable(root: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceRef.model_validate({**WORKSPACE_REF, "root": root})


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("document", REFUSED_RESULTS)
def test_session_result_shape(mode: str, document: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        result_in(mode, document)

    assert REFUSED_MARKER not in str(raised.value)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("document", REFUSED_RESULT_KEYS)
def test_unknown_result_keys_stay_out_of_safe_errors(
    mode: str, document: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as raised:
        result_in(mode, document)

    assert REFUSED_MARKER not in safe_rendering(raised.value)


def test_session_result_accepts_its_bounds() -> None:
    document = session_result(
        usage=[
            usage_row(model=f"model-{index}", observation_sequence=index)
            for index in range(MAX_USAGE_ROWS)
        ],
        denials={"items": [DENIAL] * MAX_SAMPLE_ITEMS, "total": MAX_SAMPLE_ITEMS},
        unconfirmed_calls={"items": [UNCONFIRMED] * MAX_SAMPLE_ITEMS, "total": 5000},
    )

    parsed = SessionResult.model_validate(document)

    assert len(parsed.usage) == MAX_USAGE_ROWS
    assert parsed.unconfirmed_calls.total == 5000


def test_session_file_names_may_hold_an_inner_space() -> None:
    document = session_spec(files=[session_file("a b.json")])

    parsed = SessionSpec.model_validate(document)

    assert parsed.files[0].name == "a b.json"


def test_known_token_fields_dump_in_a_fixed_order() -> None:
    known = ["reasoning_tokens", "input_tokens", "output_tokens", "cached_input_tokens"]
    document = event("usage", known_token_fields=known)

    dumped = EVENT_ADAPTER.dump_python(
        EVENT_ADAPTER.validate_python(document), mode="json"
    )

    assert dumped["data"]["known_token_fields"] == sorted(known)


def test_cache_split_dumps_in_a_fixed_order() -> None:
    document = event(
        "usage",
        known_token_fields=CACHE_SPLIT_KNOWN,
        cache_creation_tokens=3,
        cache_creation_by_ttl={"1h": 2, "5m": 1},
    )

    dumped = EVENT_ADAPTER.dump_json(EVENT_ADAPTER.validate_python(document))

    assert b'"cache_creation_by_ttl":{"5m":1,"1h":2}' in dumped


@pytest.mark.parametrize(
    "value", ["x" * (MAX_CANONICAL_BYTES - 2), nested_list(DEFAULT_MAX_DEPTH)]
)
def test_output_values_reach_their_bound(value: JsonValue) -> None:
    document = {"source": "structured_output", "value": value}

    parsed = OutputCandidate.model_validate(document)

    assert parsed.value == value


@pytest.mark.parametrize(
    "value", ["x" * (MAX_CANONICAL_BYTES - 1), nested_list(DEFAULT_MAX_DEPTH + 1)]
)
def test_output_values_past_their_bound_are_refused(value: JsonValue) -> None:
    document = {"source": "structured_output", "value": value}

    with pytest.raises(ValidationError):
        OutputCandidate.model_validate(document)
