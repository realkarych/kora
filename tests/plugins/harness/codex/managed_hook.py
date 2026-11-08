import json
import os
import signal
import socket
import sys
from pathlib import Path

from kora.plugins.harness.codex.pre_tool_use import (
    HookInputFailure,
    HookInputRefused,
    decode_pre_tool_use,
)
from tests.plugins.harness.codex.selected_launch import (
    HOOK_TIMEOUT_SOCKET,
    SELECTED_DENIAL,
    SELECTED_HOOK_CASES,
    SELECTED_HOOK_TIMEOUT_CASES,
    SELECTED_PATCH_CASES,
    selected_command,
)

CONTROL = Path("/var/lib/kora-hook-probe/control")
MAX_INPUT_BYTES = 16_384
PATCH = (
    "*** Begin Patch\n"
    "*** Add File: kora-probe.marker\n"
    "+direct patch marker\n"
    "*** End Patch\n"
)
INPUT_REFUSAL = "kora managed hook probe refused input"
CAP_REFUSAL = "kora managed hook probe refused oversized input"
PATCH_DENIAL = "kora managed hook probe denied the direct patch"


def deny(reason: str) -> int:
    sys.stderr.write(reason + "\n")
    return 2


def hold_for_native_timeout() -> None:
    with socket.socket(socket.AF_UNIX) as barrier:
        barrier.connect(str(CONTROL / HOOK_TIMEOUT_SOCKET))
        barrier.sendall(b"1")
        signal.pause()


def main() -> int:
    decision = (CONTROL / "decision").read_text(encoding="utf-8")
    executor = decision in {"executor-allow", "executor-deny", "executor-stall"}
    selected = decision in SELECTED_HOOK_CASES or decision in {
        "selected-command-metadata",
        "selected-allow",
        "selected-deny",
    }
    patch = (
        decision in {"patch-allow", "patch-deny"} or decision in SELECTED_PATCH_CASES
    )
    nested_input = decision == "shell_controls" or executor or (selected and not patch)
    expected_command = "touch kora-probe.marker"
    if decision in {"interactive", "decoder-refuse"} or nested_input or patch:
        with (CONTROL / "invocations").open("a", encoding="utf-8") as invocations:
            invocations.write("1")
    if decision == "interactive":
        expected_command = (
            "printf 'kora-stdin-ready\\n'; IFS= read -r token && "
            'test "$token" = kora-stdin-token && '
            "printf 'kora-stdin-written\\n' > kora-probe.marker && "
            "cat kora-probe.marker"
        )
    if decision == "interrupt":
        expected_command = (
            "exec /usr/local/bin/python3 -I /opt/kora-probe/interrupt_child.py"
        )
    if nested_input:
        expected_command = (
            'shell_path=$(readlink -f -- "$0") && '
            'mode=posix-sh && if [ -n "${BASH_VERSION-}" ]; then '
            "mode=bash-non-login; if shopt -q login_shell; then mode=bash-login; fi; "
            "fi && "
            """printf '%s\\n' "$shell_path" "$mode" > kora-probe.marker"""
        )
    if executor:
        origin = '"${KORA_PROBE_EXECUTOR_ORIGIN-}"'
        expected_command += f" && printf '%s\\n' {origin} >> kora-probe.marker"

    if selected:
        expected_command = selected_command(decision)

    allowance = 1 if decision == "decoder-refuse" else MAX_INPUT_BYTES
    try:
        call = decode_pre_tool_use(
            sys.stdin.buffer.read(allowance + 1), max_bytes=allowance
        )
    except HookInputRefused as error:
        if error.reason is HookInputFailure.INPUT_TOO_LARGE:
            return deny(CAP_REFUSAL)
        return deny(INPUT_REFUSAL)
    expected_call_id = "kora-hook-probe-call"
    if decision == "interrupt" and call.tool_use_id == "kora-interrupt-primer":
        expected_command = ":"
        expected_call_id = "kora-interrupt-primer"
    if (
        call.tool != ("apply_patch" if patch else "Bash")
        or call.tool_use_id != expected_call_id
        or call.command != (PATCH if patch else expected_command)
    ):
        return deny(INPUT_REFUSAL)

    receipt_input = (
        {"tool_input": {"command": call.command}}
        if nested_input
        else {"command": call.command}
    )
    receipt_identity = (
        {"session_id": call.session_id, "turn_id": call.turn_id}
        if decision in SELECTED_HOOK_CASES
        else {}
    )
    with (CONTROL / "receipts.jsonl").open("a", encoding="utf-8") as receipts:
        receipts.write(
            json.dumps(
                {
                    "event": "PreToolUse",
                    "tool": call.tool,
                    "call": call.tool_use_id,
                    **receipt_input,
                    **receipt_identity,
                    "pid": os.getpid(),
                }
            )
            + "\n"
        )
    if decision in {
        "allow",
        "interactive",
        "shell_controls",
        "executor-allow",
        "selected-command-metadata",
        "selected-allow",
        "selected-patch-allow",
        "patch-allow",
        "interrupt",
    }:
        return 0
    if decision in {"stall", "executor-stall"}:
        signal.pause()
    if decision == "deny":
        return deny("kora managed hook probe denied the marker command")
    if decision == "executor-deny":
        return deny("kora managed hook probe denied the executor command")
    if decision in {"selected-deny", "selected-hook-deny"}:
        return deny(SELECTED_DENIAL)
    if decision == "selected-hook-no-reason":
        return 2
    if decision in {"selected-hook-exit-one", "selected-patch-exit-one"}:
        return 1
    if decision in SELECTED_HOOK_TIMEOUT_CASES:
        hold_for_native_timeout()
        return deny(INPUT_REFUSAL)
    if decision in {"patch-deny", "selected-patch-deny"}:
        return deny(PATCH_DENIAL)
    return deny(INPUT_REFUSAL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(deny(INPUT_REFUSAL)) from None
