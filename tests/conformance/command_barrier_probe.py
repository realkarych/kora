import asyncio
import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import (
        ProbeClock,
    )
else:
    from kora_tooling_fixture.clock import ProbeClock

from kora.cli import CLI_COMPONENT
from kora.conformance.command import ConformanceCommand
from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.result import ChildInput, InputFiles
from kora.observability import LoggingSettings, configure_process
from kora.protocols.lifetime import CleanupOutcome, OriginalOutcome
from kora.protocols.metadata import ProtocolName


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run(home: Path, mode: str, installed_root: Path) -> None:
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "command-barrier", utc_now
    )
    clock = ProbeClock()
    command = ConformanceCommand(
        diagnostics,
        clock=clock,
        launcher=InstalledIOHelperLauncher(home=home, state_home=home / "state"),
    )
    with TemporaryDirectory(prefix="kora-barrier-", dir="/tmp") as temporary:
        folder = Path(temporary).resolve()
        config = folder / "config.json"
        data = folder / "data.json"
        config.write_text(json.dumps({"value": "private-barrier-value"}))
        data.write_text(
            json.dumps({"mode": mode, "feedback": str(folder / "feedback")})
        )
        feedback_path = (
            installed_root / f"{os.getpid()}.sock"
            if mode == "bootstrap"
            else folder / "feedback"
        )
        request = ChildInput(
            protocol=ProtocolName.SECRETS,
            name="blocked" if mode == "bootstrap" else "configured",
            fixtures="kora_tooling_fixture.fixtures_command",
            files=InputFiles(configuration=str(config), fixture_data=str(data)),
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(feedback_path))
            listener.listen(1)
            listener.setblocking(False)
            operation = asyncio.create_task(command.run(request))
            try:
                async with asyncio.timeout(20):
                    loop = asyncio.get_running_loop()
                    endpoint, _ = await loop.sock_accept(listener)
                    with endpoint:
                        feedback = await loop.sock_recv(endpoint, 32)
                        if mode == "bootstrap":
                            assert feedback == b"bootstrap"
                            assert command.result.phase == "bootstrap"
                            clock.advance(31_000_000_000)
                            await command.owner.closure_started.wait()
                            clock.advance(5_000_000_000)
                        elif mode == "idle":
                            assert len(feedback) == 32
                            while command.result.phase != "ready":
                                turn: asyncio.Future[None] = loop.create_future()
                                loop.call_soon(turn.set_result, None)
                                await turn
                            clock.advance(31_000_000_000)
                        else:
                            assert feedback == b"teardown"
                            await command.owner.closure_started.wait()
                            assert not command.owner.admission_open
                            assert not command.owner.invocation_cleanup.result.done()
                            assert not operation.done()
                        if mode != "bootstrap":
                            await loop.sock_sendall(endpoint, b"a")
                    result = await operation
                    assert result.outcome is (
                        OriginalOutcome.COMPLETE
                        if mode == "idle"
                        else OriginalOutcome.FAILED
                    )
                    assert (
                        result.completed
                        == result.expected
                        == (0 if mode == "bootstrap" else 10)
                    )
                    assert result.cleanup is not None
                    assert result.cleanup.state is CleanupOutcome.CLOSED
                    assert command.process is not None
                    assert command.process.closed
                    assert command.owner.inventory().helpers.retained_helpers == 0
                    os.write(1, result.model_dump_json().encode())
            finally:
                operation.cancel()
                clock.use_system_time()
                await asyncio.gather(operation, return_exceptions=True)
                diagnostics.shutdown()
                feedback_path.unlink()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
