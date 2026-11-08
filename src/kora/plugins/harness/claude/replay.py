import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final, cast

from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.observability.redaction import SecretRegistry
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.plugins.harness.claude.sdk import bundled_binary, run_replayed_client
from kora.plugins.harness.claude.session import (
    COMPONENT,
    SessionFailure,
    close_recorded_processes,
    failure_for,
    interpreter,
    launch_rejected,
    launcher_text,
    remove_file,
    require_bound,
    require_directory,
    require_environment,
    require_environment_value,
    require_launch_directory,
    require_prompt,
    require_tools,
    session_failed,
    write_exclusive,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import EventSink
from kora.protocols.time import Clock

RECORDING_ENV: Final[str] = "KORA_CLAUDE_REPLAY_RECORDING"
MAX_RECORDING_BYTES: Final[int] = 1_048_576
MAX_RECORDING_LINES: Final[int] = 64
REPLAY_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "harness.claude.replay_finished",
        COMPONENT,
        Level.INFO,
        "a replayed Claude session finished",
        {"outcome": FieldKind.STRING},
    )
)
REPLAY_FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "harness.claude.replay_failed",
        COMPONENT,
        Level.WARNING,
        "a replayed Claude session failed",
        {"reason": FieldKind.STRING},
    )
)


@dataclass(frozen=True)
class PreparedReplay:
    launcher: Path
    secret: Path
    player: Path


def player_text(executable: str) -> str:
    lines = (
        "import json",
        "import os",
        "import sys",
        "name = 'KORA_CLAUDE_REPLAY_RECORDING'",
        "path = os.environ[name]",
        "with open(path, encoding='utf-8') as handle:",
        "    stored = []",
        "    for line in handle:",
        "        if line.strip() == '':",
        "            continue",
        "        if line.endswith('\\n'):",
        "            stored.append(line)",
        "        else:",
        "            stored.append(line + '\\n')",
        "for raw in sys.stdin:",
        "    failed = False",
        "    try:",
        "        message = json.loads(raw)",
        "    except json.JSONDecodeError:",
        "        failed = True",
        "        message = None",
        "    if failed or not isinstance(message, dict):",
        "        raise SystemExit(1) from None",
        "    kind = message.get('type')",
        "    if kind != 'control_request':",
        "        if kind == 'user':",
        "            sys.stdout.write(''.join(stored))",
        "            sys.stdout.flush()",
        "            raise SystemExit(0)",
        "        continue",
        "    request = message.get('request')",
        "    subtype = None",
        "    if isinstance(request, dict):",
        "        subtype = request.get('subtype')",
        "    request_id = message.get('request_id')",
        "    if subtype == 'initialize':",
        "        response = {",
        "            'subtype': 'success',",
        "            'request_id': request_id,",
        "            'response': {},",
        "        }",
        "    else:",
        "        response = {",
        "            'subtype': 'error',",
        "            'request_id': request_id,",
        "            'error': 'unsupported',",
        "        }",
        "    outer = {'type': 'control_response', 'response': response}",
        "    encoded = json.dumps(outer, separators=(',', ':'))",
        "    sys.stdout.write(encoded + '\\n')",
        "    sys.stdout.flush()",
        "raise SystemExit(0)",
    )
    return f"#!{executable} -I\n" + "\n".join(lines) + "\n"


def decoded_object(line: str) -> dict[object, object]:
    failed = False
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError:
        failed = True
        parsed = None
    if failed or not isinstance(parsed, dict):
        session_failed()
    return cast("dict[object, object]", parsed)


def accept_recording(data: bytes) -> None:
    failed = False
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        failed = True
        text = ""
    if failed:
        session_failed()
    lines = [line for line in text.splitlines() if line != ""]
    if not 1 <= len(lines) <= MAX_RECORDING_LINES:
        session_failed()
    for line in lines:
        kind = decoded_object(line).get("type")
        if not isinstance(kind, str):
            session_failed()


def require_recording(path: Path) -> None:
    if not path.is_absolute():
        launch_rejected()
    try:
        info = path.lstat()
    except OSError:
        launch_rejected()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        launch_rejected()
    if info.st_size < 1 or info.st_size > MAX_RECORDING_BYTES:
        launch_rejected()
    try:
        data = path.read_bytes()
    except OSError:
        launch_rejected()
    if len(data) != info.st_size:
        session_failed()
    accept_recording(data)


def prepare_replay(
    *,
    cwd: Path,
    launch_dir: Path,
    recording: Path,
    environment: Mapping[str, str],
    prompt: str,
    tools: tuple[str, ...],
    bound: timedelta,
) -> tuple[PreparedReplay, int]:
    require_prompt(prompt)
    require_tools(tools)
    span = require_bound(bound)
    if not cwd.is_absolute():
        launch_rejected()
    require_directory(cwd)
    require_launch_directory(launch_dir)
    require_recording(recording)
    if RECORDING_ENV in environment:
        launch_rejected()
    prepared = require_environment(environment)
    require_environment_value(str(recording))
    prepared[RECORDING_ENV] = str(recording)
    executable = interpreter()
    bundled_binary()
    secret = launch_dir / "environment.json"
    player = launch_dir / "player"
    try:
        os.mkdir(launch_dir, 0o700)
        os.chmod(launch_dir, 0o700)
        write_exclusive(
            secret,
            json.dumps(prepared, sort_keys=True).encode("utf-8"),
            0o600,
        )
        write_exclusive(player, player_text(executable).encode("utf-8"), 0o700)
        write_exclusive(launch_dir / "binary", str(player).encode("utf-8"), 0o600)
        launcher = launch_dir / "launch"
        write_exclusive(launcher, launcher_text(executable).encode("utf-8"), 0o700)
    except KoraProtocolError:
        remove_file(secret)
        raise
    return PreparedReplay(launcher, secret, player), span


def deadline_check(clock: Clock, deadline_ns: int) -> Callable[[], None]:
    def check() -> None:
        if clock.monotonic_ns() >= deadline_ns:
            session_failed()

    return check


async def run_replayed_session(
    context: ProtocolContext,
    sink: EventSink,
    registry: SecretRegistry,
    *,
    cwd: Path,
    launch_dir: Path,
    recording: Path,
    environment: Mapping[str, str],
    prompt: str,
    tools: tuple[str, ...],
    bound: timedelta,
    output_format: bool,
) -> None:
    secret: Path | None = None
    failure: SessionFailure | None = None
    completed = False
    try:
        try:
            prepared, span = prepare_replay(
                cwd=cwd,
                launch_dir=launch_dir,
                recording=recording,
                environment=environment,
                prompt=prompt,
                tools=tools,
                bound=bound,
            )
        except KoraProtocolError as error:
            failure = failure_for(error)
            raise
        secret = prepared.secret
        deadline_ns = context.clock.monotonic_ns() + span
        events = ClaudeEvents(
            sink,
            registry,
            output_format=output_format,
            check_deadline=deadline_check(context.clock, deadline_ns),
        )
        try:
            await run_replayed_client(
                events,
                cwd=cwd,
                launcher=prepared.launcher,
                launch_dir=launch_dir,
                prompt=prompt,
                tools=tools,
                clock=context.clock,
                deadline_ns=deadline_ns,
            )
        except KoraProtocolError:
            failure = SessionFailure.SESSION_FAILED
            raise
        else:
            completed = True
        finally:
            try:
                closed = await close_recorded_processes(
                    launch_dir,
                    str(prepared.player),
                    context.clock,
                )
            finally:
                remove_file(secret)
                secret = None
            if not closed:
                failure = SessionFailure.CHILDREN_REMAINING
                completed = False
        if failure is not None or not completed:
            session_failed()
    except KoraProtocolError:
        context.logger.emit(
            REPLAY_FAILED,
            reason=(failure or SessionFailure.SESSION_FAILED).value,
        )
        raise
    context.logger.emit(REPLAY_FINISHED, outcome="completed")
