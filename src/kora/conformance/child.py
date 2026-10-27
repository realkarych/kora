import asyncio
import socket
import sys
from pathlib import Path

import pytest

from kora.conformance.bridge import ConformanceSelection
from kora.conformance.capture import Pipe
from kora.conformance.common import CONFORMANCE_COMPONENT, INSTANCE_ID, utc_now
from kora.conformance.executor import Executor
from kora.conformance.result import ChildInput, ChildResult, accepted_report
from kora.conformance.session import CURRENT_SESSION, Session
from kora.observability import Level, LoggingSettings, configure_process
from kora.protocols.loading import LOADING_COMPONENT, load_provider
from kora.protocols.time import SystemClock
from kora.runner.channels import MAX_RECORD_BYTES, BoundedChannel
from kora.runner.filesystem import DirectoryAnchor


async def inputs(channel: BoundedChannel) -> tuple[ChildInput, bytes, bytes]:
    deadline = SystemClock().monotonic_ns() + 30_000_000_000
    request = ChildInput.model_validate_json(await channel.read_record(deadline, 4096))
    configuration = await channel.read_record(deadline, MAX_RECORD_BYTES)
    fixture_data = await channel.read_record(deadline, MAX_RECORD_BYTES)
    return request, configuration, fixture_data


async def report(channel: BoundedChannel, result: ChildResult) -> None:
    await channel.write_record(
        result.model_dump_json().encode(), SystemClock().monotonic_ns() + 30_000_000_000
    )


def run_pytest(fixtures: str, session: Session) -> ChildResult:
    root = Path(__file__).parent
    token = CURRENT_SESSION.set(session)
    pipe = Pipe.open()
    pipe.reader.start()
    try:
        status = pytest.main(
            [
                "-c",
                "/dev/null",
                "--noconftest",
                "--import-mode=importlib",
                f"--rootdir={root}",
                "--capture=no",
                "-p",
                "no:logging",
                "-p",
                "no:warnings",
                "-p",
                "no:cacheprovider",
                "-p",
                "kora.conformance.command_plugin",
                "-p",
                fixtures,
                f"--junitxml=/dev/fd/{pipe.write_end}",
                "-o",
                "junit_family=xunit2",
                "-o",
                "junit_logging=no",
                "-o",
                "junit_log_passing_tests=false",
                str(root / "command_case.py"),
            ]
        )
    finally:
        CURRENT_SESSION.reset(token)
        pipe.release()
    return ChildResult(
        phase="complete",
        expected=len(session.executor.cases),
        completed=session.executor.completed,
        pytest_status=int(status),
        wrapper_closed=session.closed,
        report_accepted=accepted_report(pipe.reader.data, pipe.reader.discarded),
    )


def main(result_path: str, event_path: str, lifetime_path: str) -> int:
    result = ChildResult(phase="bootstrap")
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as events,
    ):
        endpoint.connect(result_path)
        channel = BoundedChannel(endpoint, SystemClock())
        startup = DirectoryAnchor.open_path(Path.cwd())
        root = DirectoryAnchor.open_path(Path.cwd().parent)
        try:
            request, configuration, fixture_data = asyncio.run(inputs(channel))
            events.connect(event_path)
            diagnostics = configure_process(
                CONFORMANCE_COMPONENT,
                LoggingSettings(component_levels={LOADING_COMPONENT: Level.DEBUG}),
                INSTANCE_ID,
                utc_now,
                descriptor=events.fileno(),
            )
            try:
                metadata = load_provider(request.protocol, request.name, diagnostics)
                selection = ConformanceSelection(metadata, configuration, fixture_data)
                executor = Executor(request.protocol)
            finally:
                drained = diagnostics.shutdown()
                events.shutdown(socket.SHUT_WR)
            if not drained or diagnostics.writer_is_alive():
                raise RuntimeError("conformance_bootstrap_diagnostics_unsettled")
            session = Session(
                request.protocol,
                selection,
                executor,
                lifetime_path,
                root,
                startup,
                lambda value: report(channel, value),
            )
            result = ChildResult(phase="pytest", expected=len(executor.cases))
            result = run_pytest(request.fixtures, session)
        finally:
            startup.close()
            root.close()
            try:
                asyncio.run(report(channel, result))
            finally:
                channel.close()
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
