import json
import os
import select
import sys
from pathlib import Path
from typing import cast


def read_message() -> dict[str, object]:
    raw = sys.stdin.readline()
    if not raw:
        raise SystemExit(0)
    message: object = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("invalid replay input")
    return cast("dict[str, object]", message)


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def pause(notification: bytes) -> None:
    descriptor = os.open(
        os.environ["KORA_CLAUDE_REPLAY_FIFO"], os.O_RDONLY | os.O_NONBLOCK
    )
    try:
        os.write(int(sys.argv[2]), notification)
        select.select([descriptor], [], [])
        if os.read(descriptor, 1) != b"x":
            raise ValueError("invalid pause release")
    finally:
        os.close(descriptor)


def control(
    message: dict[str, object], version: str, ignore: bool = False
) -> str | None:
    request = message.get("request")
    if message.get("type") != "control_request" or not isinstance(request, dict):
        return None
    subtype = cast("dict[str, object]", request).get("subtype")
    if subtype not in {"initialize", "interrupt"}:
        raise ValueError("unexpected replay control")
    if subtype == "initialize" and version == "ignore_initialize":
        os.write(int(sys.argv[2]), b"initializing\n")
        return "initialize"
    if subtype == "initialize" and version == "oversized_initialize":
        pause(b"initializing\n")
        send({"type": "system", "subtype": "oversized", "value": "x" * 1_100_000})
        return "initialize"
    if subtype == "interrupt":
        os.write(int(sys.argv[2]), b"interrupt\n")
    if ignore and subtype == "interrupt":
        return "interrupt"
    send(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": message["request_id"],
                "response": {"claude_code_version": version}
                if subtype == "initialize"
                else {},
            },
        }
    )
    return str(subtype)


def play(recording: Path, version: str) -> None:
    while True:
        message = read_message()
        if control(message, version) is not None:
            continue
        if message.get("type") != "user":
            raise ValueError("expected replay prompt")
        recording.with_suffix(".prompt").write_text("received")
        break
    interrupted = False
    with recording.open() as handle:
        for line in handle:
            value: object = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("invalid replay frame")
            frame = cast("dict[str, object]", value)
            directive = frame.get("kora_replay")
            if directive in {"await_interrupt", "ignore_interrupt"}:
                os.write(int(sys.argv[2]), b"waiting\n")
                while not interrupted or directive == "ignore_interrupt":
                    interrupted |= (
                        control(
                            read_message(), version, directive == "ignore_interrupt"
                        )
                        == "interrupt"
                    )
                    if interrupted:
                        recording.with_suffix(".interrupted").write_text("received")
                continue
            if directive == "stderr":
                count = frame["bytes"]
                if type(count) is not int or count < 0:
                    raise ValueError("invalid stderr size")
                sys.stderr.write("x" * count)
                sys.stderr.flush()
                continue
            if directive == "pause":
                pause(b"pause\n")
                continue
            if directive == "linger":
                for raw in sys.stdin:
                    message = json.loads(raw)
                    control(message, version)
                continue
            if "kora_replay" in frame:
                raise ValueError("unknown replay directive")
            send(frame)
            if frame.get("type") != "control_request":
                continue
            while True:
                reply = read_message()
                handled = control(reply, version)
                if handled is not None:
                    interrupted |= handled == "interrupt"
                    continue
                response = reply.get("response")
                if not isinstance(response, dict):
                    raise ValueError("expected hook response")
                fields = cast("dict[str, object]", response)
                if (
                    fields.get("request_id") != frame.get("request_id")
                    or fields.get("subtype") != "success"
                ):
                    raise ValueError("unexpected hook response")
                with recording.with_suffix(".decisions").open("a") as decisions:
                    decisions.write(json.dumps(fields.get("response")) + "\n")
                os.write(int(sys.argv[2]), b"decided\n")
                break


if __name__ == "__main__":
    play(Path(os.environ["KORA_CLAUDE_REPLAY_RECORDING"]), sys.argv[1])
