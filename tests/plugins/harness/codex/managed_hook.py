import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

CONTROL = Path("/var/lib/kora-hook-probe/control")
MAX_INPUT_BYTES = 16_384


def main() -> int:
    decision = (CONTROL / "decision").read_text(encoding="utf-8")
    executor = decision in {"executor-allow", "executor-deny", "executor-stall"}
    nested_input = decision == "shell_controls" or executor
    expected_command = "touch kora-probe.marker"
    if decision == "interactive" or nested_input:
        with (CONTROL / "invocations").open("a", encoding="utf-8") as invocations:
            invocations.write("1")
    if decision == "interactive":
        expected_command = (
            "printf 'kora-stdin-ready\\n'; IFS= read -r token && "
            'test "$token" = kora-stdin-token && '
            "printf 'kora-stdin-written\\n' > kora-probe.marker && "
            "cat kora-probe.marker"
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

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return 2
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        return 2
    event = cast(dict[str, object], payload)
    raw_input = event.get("tool_input")
    if not isinstance(raw_input, dict):
        return 2
    command_input = cast(dict[str, object], raw_input)
    command = command_input.get("command")
    if (
        event.get("hook_event_name") != "PreToolUse"
        or event.get("tool_name") != "Bash"
        or event.get("tool_use_id") != "kora-hook-probe-call"
        or command != expected_command
        or (nested_input and command_input != {"command": expected_command})
    ):
        return 2

    receipt_input = (
        {"tool_input": command_input} if nested_input else {"command": command}
    )
    with (CONTROL / "receipts.jsonl").open("a", encoding="utf-8") as receipts:
        receipts.write(
            json.dumps(
                {
                    "event": event["hook_event_name"],
                    "tool": event["tool_name"],
                    "call": event["tool_use_id"],
                    **receipt_input,
                    "pid": os.getpid(),
                }
            )
            + "\n"
        )
    if decision in {"allow", "interactive", "shell_controls", "executor-allow"}:
        return 0
    if decision in {"stall", "executor-stall"}:
        signal.pause()
    if decision == "deny":
        sys.stderr.write("kora managed hook probe denied the marker command\n")
    if decision == "executor-deny":
        sys.stderr.write("kora managed hook probe denied the executor command\n")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(2) from None
