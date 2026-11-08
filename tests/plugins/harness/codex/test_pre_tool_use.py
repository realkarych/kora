import json
import traceback
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.plugins.harness.codex.policy_input import policy_request
from kora.plugins.harness.codex.pre_tool_use import (
    HookInputFailure,
    HookInputRefused,
    decode_pre_tool_use,
)
from kora.protocols.policy import PolicyValueRefused
from tests.plugins.policy.policy_support import (
    layers,
    policy,
    reason,
    update_patch,
    workspace,
)

LIMIT = 16_384
CALL_ID = "codex-call-1"
PRIVATE_COMMAND = "private-command-canary"
PRIVATE_ID = "private-id-canary"
PRIVATE_METADATA = "private-metadata-canary"
PRIVATE_SESSION = " private-session-café "
PRIVATE_TURN = " private-turn-résumé "


def native_payload(
    *, tool: str = "Bash", command: str = "git status", cwd: str = "/reported"
) -> dict[str, object]:
    return {
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "model": "fixture-model",
        "permission_mode": "bypassPermissions",
        "session_id": "session-id",
        "tool_input": {"command": command},
        "tool_name": tool,
        "tool_use_id": CALL_ID,
        "transcript_path": None,
        "turn_id": "turn-id",
    }


def encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("Bash", "  printf 'café\\n'  "),
        ("apply_patch", update_patch("résumé.txt") + "\n"),
        ("Bash", ""),
    ],
    ids=["bash", "patch", "empty-command"],
)
def test_codex_pre_tool_use_decodes_pinned_inputs(tool: str, command: str) -> None:
    payload = native_payload(tool=tool, command=command)
    payload["transcript_path"] = "/reported/session.jsonl"
    payload["session_id"] = PRIVATE_SESSION
    payload["turn_id"] = PRIVATE_TURN
    raw = encode(payload)

    parsed = decode_pre_tool_use(raw, max_bytes=len(raw))

    assert parsed.tool == tool
    assert parsed.tool_use_id == CALL_ID
    assert parsed.command == command
    assert parsed.session_id == PRIVATE_SESSION
    assert parsed.turn_id == PRIVATE_TURN
    assert CALL_ID not in repr(parsed)
    assert PRIVATE_SESSION not in repr(parsed)
    assert PRIVATE_TURN not in repr(parsed)
    assert "reported" not in repr(parsed)
    if command:
        assert command not in repr(parsed)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"hook_event_name": "PostToolUse"}, HookInputFailure.INVALID_INPUT),
        ({"cwd": []}, HookInputFailure.INVALID_INPUT),
        ({"permission_mode": "unknown"}, HookInputFailure.INVALID_INPUT),
        ({"tool_input": []}, HookInputFailure.INVALID_INPUT),
        ({"tool_input": {}}, HookInputFailure.INVALID_INPUT),
        ({"tool_input": {"command": 1}}, HookInputFailure.INVALID_INPUT),
        (
            {"tool_input": {"command": "git status", "workdir": "/outside"}},
            HookInputFailure.INVALID_INPUT,
        ),
        ({"tool_input": {"command": "\ud800"}}, HookInputFailure.INVALID_INPUT),
        ({"cwd": "\ud800"}, HookInputFailure.INVALID_INPUT),
        ({"tool_use_id": 7}, HookInputFailure.INVALID_INPUT),
        ({"transcript_path": {}}, HookInputFailure.INVALID_INPUT),
        ({"agent_id": "agent"}, HookInputFailure.INVALID_INPUT),
        ({"agent_id": None}, HookInputFailure.INVALID_INPUT),
        ({"agent_type": None}, HookInputFailure.INVALID_INPUT),
        ({"unknown": True}, HookInputFailure.INVALID_INPUT),
        ({"tool_name": "Write"}, HookInputFailure.UNSUPPORTED_TOOL),
    ],
    ids=[
        "wrong-event",
        "cwd-type",
        "unknown-mode",
        "input-type",
        "missing-command",
        "command-type",
        "extra-tool-input",
        "command-surrogate",
        "discarded-surrogate",
        "call-id-type",
        "transcript-type",
        "subagent-id",
        "null-agent-id",
        "null-agent-type",
        "unknown-field",
        "unsupported-tool",
    ],
)
def test_codex_pre_tool_use_refuses_invalid_input(
    overrides: dict[str, object], expected: HookInputFailure
) -> None:
    raw = encode({**native_payload(), **overrides})

    with pytest.raises(HookInputRefused) as caught:
        decode_pre_tool_use(raw, max_bytes=LIMIT)

    assert caught.value.reason is expected


@pytest.mark.parametrize("field", tuple(native_payload()))
def test_codex_pre_tool_use_refuses_each_missing_field(field: str) -> None:
    payload = native_payload()
    del payload[field]
    raw = encode(payload)

    with pytest.raises(HookInputRefused) as caught:
        decode_pre_tool_use(raw, max_bytes=LIMIT)

    assert caught.value.reason is HookInputFailure.INVALID_INPUT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"{", HookInputFailure.INVALID_INPUT),
        (
            encode(native_payload()).replace(b"fixture-model", b"\xff"),
            HookInputFailure.INVALID_INPUT,
        ),
        (
            b'{"cwd":"duplicate",' + encode(native_payload())[1:],
            HookInputFailure.INVALID_INPUT,
        ),
        (b" " * (LIMIT + 1), HookInputFailure.INPUT_TOO_LARGE),
    ],
    ids=["malformed", "invalid-utf8", "duplicate-key", "over-cap"],
)
def test_codex_pre_tool_use_refuses_bad_wire_bytes(
    raw: bytes, expected: HookInputFailure
) -> None:
    with pytest.raises(HookInputRefused) as caught:
        decode_pre_tool_use(raw, max_bytes=LIMIT)

    assert caught.value.reason is expected


def test_codex_hook_input_failure_is_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = native_payload(command=PRIVATE_COMMAND)
    payload["tool_use_id"] = PRIVATE_ID
    payload["session_id"] = PRIVATE_SESSION
    payload["turn_id"] = PRIVATE_TURN
    payload["transcript_path"] = {"invalid": PRIVATE_METADATA}
    raw = encode(payload)

    with pytest.raises(HookInputRefused) as caught:
        decode_pre_tool_use(raw, max_bytes=LIMIT)
    captured = capsys.readouterr()
    rendered = "".join(traceback.format_exception(caught.value))

    assert caught.value.args == ("hook_input_invalid",)
    assert caught.value.__context__ is None
    assert captured.out == captured.err == ""
    assert PRIVATE_COMMAND not in rendered
    assert PRIVATE_ID not in rendered
    assert PRIVATE_METADATA not in rendered
    assert PRIVATE_SESSION not in rendered
    assert PRIVATE_TURN not in rendered


def test_codex_hook_request_uses_trusted_workspace(
    kora_capture: Capture, tmp_path: Path
) -> None:
    trusted = tmp_path / "trusted"
    reported = tmp_path / "reported"
    trusted.mkdir()
    reported.mkdir()
    (trusted / "protected.txt").write_text("old\n", encoding="utf-8")
    (trusted / "alias").symlink_to("protected.txt")
    (trusted / "ordinary.txt").write_text("old\n", encoding="utf-8")
    (reported / "alias").write_text("old\n", encoding="utf-8")
    protected = ("protected.txt",)
    payload = native_payload(
        tool="apply_patch", command=update_patch("alias"), cwd=str(reported)
    )
    payload["session_id"] = PRIVATE_SESSION
    payload["turn_id"] = PRIVATE_TURN

    parsed = decode_pre_tool_use(encode(payload), max_bytes=LIMIT)
    request = policy_request(
        parsed, workspace=workspace(trusted), node_path="graph.agent"
    )
    comparison = policy_request(
        parsed, workspace=workspace(reported), node_path="graph.agent"
    )
    trusted_reason = reason(kora_capture, request, protected=protected)
    reported_reason = reason(kora_capture, comparison, protected=protected)

    assert request.workspace.root == str(trusted)
    assert request.node_path == "graph.agent"
    assert request.agent_id is None
    assert request.harness == "codex"
    assert request.tool_use_id == CALL_ID
    assert request.arguments == {"command": update_patch("alias")}
    assert PRIVATE_SESSION not in repr(request)
    assert PRIVATE_TURN not in repr(request)
    assert trusted_reason == "protected_path"
    assert reported_reason == "allowed"


@pytest.mark.parametrize(
    ("tool", "command", "expected"),
    [
        ("Bash", "git status", "allowed"),
        ("Bash", "rm target", "namespace.deny.0"),
        ("apply_patch", update_patch("ordinary.txt"), "allowed"),
        ("Bash", "apply_patch <<'PATCH'\nexample\nPATCH", "argument_invalid"),
    ],
    ids=["allowed-shell", "denied-shell", "ordinary-patch", "shell-routed-patch"],
)
def test_codex_hook_request_reaches_real_policy(
    kora_capture: Capture, tmp_path: Path, tool: str, command: str, expected: str
) -> None:
    (tmp_path / "ordinary.txt").write_text("old\n", encoding="utf-8")
    raw = encode(native_payload(tool=tool, command=command))
    chosen = layers(namespace=policy(deny=("Bash(rm *)",)))

    parsed = decode_pre_tool_use(raw, max_bytes=LIMIT)
    request = policy_request(
        parsed, workspace=workspace(tmp_path), node_path="graph.agent"
    )
    verdict = reason(kora_capture, request, chosen, protected=("protected.txt",))

    assert request.arguments == {"command": command}
    assert verdict == expected


def test_codex_hook_request_retains_identifier_validation(tmp_path: Path) -> None:
    payload = native_payload()
    payload["tool_use_id"] = f"{PRIVATE_ID} invalid"
    parsed = decode_pre_tool_use(encode(payload), max_bytes=LIMIT)
    trusted = workspace(tmp_path)

    with pytest.raises(PolicyValueRefused) as caught:
        policy_request(parsed, workspace=trusted, node_path="graph.agent")

    assert str(caught.value) == "the policy value tool_use_id is invalid"
