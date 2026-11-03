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
    expected_command = "touch kora-probe.marker"
    if decision == "interactive":
        with (CONTROL / "invocations").open("a", encoding="utf-8") as invocations:
            invocations.write("1")
        expected_command = (
            "printf 'kora-stdin-ready\\n'; IFS= read -r token && "
            'test "$token" = kora-stdin-token && '
            "printf 'kora-stdin-written\\n' > kora-probe.marker && "
            "cat kora-probe.marker"
        )

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return 2
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        return 2
    event = cast(dict[str, object], payload)
    command_input = event.get("tool_input")
    if not isinstance(command_input, dict):
        return 2
    command = cast(dict[str, object], command_input).get("command")
    if (
        event.get("hook_event_name") != "PreToolUse"
        or event.get("tool_name") != "Bash"
        or event.get("tool_use_id") != "kora-hook-probe-call"
        or command != expected_command
    ):
        return 2

    with (CONTROL / "receipts.jsonl").open("a", encoding="utf-8") as receipts:
        receipts.write(
            json.dumps(
                {
                    "event": event["hook_event_name"],
                    "tool": event["tool_name"],
                    "call": event["tool_use_id"],
                    "command": command,
                    "pid": os.getpid(),
                }
            )
            + "\n"
        )
    if decision in {"allow", "interactive"}:
        return 0
    if decision == "stall":
        signal.pause()
    if decision == "deny":
        sys.stderr.write("kora managed hook probe denied the marker command\n")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(2) from None
