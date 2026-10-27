import asyncio
import os
import socket
import sys
from pathlib import Path

from kora.conformance.bridge import ConformanceSelection
from kora.conformance.child import inputs, report, run_pytest
from kora.conformance.executor import Executor
from kora.conformance.result import ChildResult
from kora.conformance.session import Session
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.filesystem import DirectoryAnchor
from kora_tooling_fixture.configured_storage import configured


def main(result_path: str, event_path: str, lifetime_path: str) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.connect(result_path)
        channel = BoundedChannel(endpoint, SystemClock())
        request, configuration, fixture_data = asyncio.run(inputs(channel))
        channel.close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as events:
        events.connect(event_path)

    async def failed_report(result: ChildResult) -> None:
        try:
            await report(channel, result)
        except ChannelError:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as feedback:
                feedback.connect(os.environ["KORA_FIXTURE_FEEDBACK"])
                feedback.sendall(b"r")
                assert feedback.recv(1) == b"a"
            raise
        raise AssertionError("closed report channel unexpectedly accepted a result")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as lifetime:
        lifetime.connect(lifetime_path)
        session = Session(
            request.protocol,
            ConformanceSelection(configured(), configuration, fixture_data),
            Executor(request.protocol),
            lifetime,
            DirectoryAnchor.open_path(Path.cwd().parent),
            DirectoryAnchor.open_path(Path.cwd()),
            failed_report,
        )
        run_pytest(request.fixtures, session)
    raise AssertionError("storage failure returned to pytest")


if __name__ == "__main__":
    main(*sys.argv[1:])
