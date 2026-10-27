from typing import Final

from kora.ir.json import JsonValue

TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
)

AT: Final[str] = "2026-10-23T00:00:00Z"
DIGEST: Final[str] = "sha256:" + "0" * 64
REFUSED_MARKER: Final[str] = "refused-harness-marker"
COVERAGE: Final[dict[str, object]] = {
    "version": 1,
    "observation_id": "observation-1",
    "observation_sequence": 1,
    "completeness": "complete",
    "observed_at": AT,
    "attribution_gap": False,
    "observation_gap": False,
}
WORKSPACE_REF: Final[dict[str, object]] = {
    "attempt_id": "attempt-1",
    "root": "/workspace/attempt",
    "repo": "example/repo",
    "commit": "c0ffee",
    "base_commit": "c0ffee",
    "branch": None,
}
WORKSPACE_TARGET: Final[dict[str, object]] = {
    "root": "/workspace/attempt",
    "host_path": None,
}
SESSION_FILE: Final[dict[str, object]] = {
    "name": "mcp.json",
    "digest": DIGEST,
    "placement": "mcp_config",
    "storage_ref": None,
}
SESSION_SPEC: Final[dict[str, object]] = {
    "harness": "claude",
    "adapter_version": "1.0.0",
    "options": {"model": "model-1"},
    "files": [SESSION_FILE],
    "prompt": "change the parser",
    "resume_handle": None,
    "price_table_id": "prices-1",
    "price_table": {},
}
DENIAL: Final[dict[str, object]] = {
    "tool": "Bash",
    "tool_use_id": "call-1",
    "source": "policy",
    "pattern": "namespace.deny.0",
    "code": "denied",
}
UNCONFIRMED: Final[dict[str, object]] = {"tool": "Bash", "tool_use_id": "call-2"}


def nested_list(depth: int) -> JsonValue:
    value: JsonValue = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def usage_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(TOKEN_FIELDS, 0)
    row.update(
        model="model-1",
        known_token_fields=["output_tokens"],
        cache_creation_by_ttl=None,
        reported_usd=None,
        observation_id="observation-1",
        observation_sequence=1,
        observed_at=AT,
        completeness="complete",
        basis="price_table",
        billing_class="api_key",
        delta_of_session_totals=False,
        source={},
    )
    row.update(changes)
    return row


def session_result(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "harness": "claude",
        "session_id": "session-1",
        "installed_version": "2.1.0",
        "billing_class": "api_key",
        "stop_reason": "end_turn",
        "terminal_reason": None,
        "api_error_status": None,
        "error_class": None,
        "error_summary": None,
        "output": {"source": "structured_output", "value": {"verdict": "pass"}},
        "output_diagnostic": None,
        "usage": [usage_row()],
        "usage_coverage": COVERAGE,
        "turns": 3,
        "denials": {"items": [DENIAL], "total": 1},
        "denials_complete": True,
        "unconfirmed_calls": {"items": [UNCONFIRMED], "total": 1},
        "policy_bypass": False,
    }
    result.update(changes)
    return result


EVENT_DATA: Final[dict[str, dict[str, object]]] = {
    "session_started": {
        "session_id": "session-1",
        "harness_version": "2.1.0",
        "billing_class": "api_key",
        "plugins_digest": DIGEST,
        "mcp_sources": ["files"],
    },
    "turn": {
        "message_id": "message-1",
        "agent_id": None,
        "text": "working on it",
        "truncated": False,
    },
    "tool_call": {
        "tool": "Bash",
        "tool_use_id": "call-1",
        "agent_id": None,
        "output_channel": False,
    },
    "tool_result": {
        "tool_use_id": "call-1",
        "is_error": False,
        "content": "ok",
        "truncated": False,
    },
    "permission_denied": DENIAL,
    "usage": {
        **{
            key: value
            for key, value in usage_row().items()
            if key not in ("completeness", "delta_of_session_totals", "source")
        },
        "message_id": "message-1",
        "agent_id": None,
        "coverage": COVERAGE,
    },
    "provider_limited": {
        "retry_after": AT,
        "scope": "rate",
        "vendor_group": None,
        "model": "model-1",
        "binding": "binding-1",
    },
    "provider_retrying": {"model": "model-1", "retry_number": 1, "retry_after": None},
    "result": {"subtype": "success", "is_error": False, "terminal_reason": None},
    "error": {"code": "api_error", "text": "overloaded", "truncated": False},
}


def event(kind: str, **changes: object) -> dict[str, object]:
    return {"i": 1, "at": AT, "kind": kind, "data": {**EVENT_DATA[kind], **changes}}


def session_spec(**changes: object) -> dict[str, object]:
    return {**SESSION_SPEC, **changes}


def session_file(name: str) -> dict[str, object]:
    return {**SESSION_FILE, "name": name}
