import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from kora.cli import CLI_COMPONENT
from kora.conformance.command import ConformanceCommand
from kora.conformance.io_helper import InstalledIOHelperLauncher, read_journal
from kora.observability import LoggingSettings, configure_process
from kora.protocols.lifetime import CleanupOutcome, OriginalOutcome


def utc_now() -> datetime:
    return datetime.now(UTC)


async def retain(home: Path, mode: str) -> None:
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "retention-probe", utc_now
    )
    command = ConformanceCommand(
        diagnostics,
        launcher=InstalledIOHelperLauncher(home=home, state_home=home / "state"),
    )
    try:
        await command.prepare_channels()
        if mode == "post_unlink":
            command.unlink_listeners()
        elif mode == "substituted":
            startup = command.startup
            assert startup is not None
            os.unlink("r", dir_fd=startup.descriptor)
            descriptor = os.open(
                "r",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=startup.descriptor,
            )
            os.close(descriptor)
            try:
                command.unlink_listeners()
            except ValueError:
                command.owner.retain_unverified_resources()
            else:
                raise AssertionError("socket substitution was accepted")
            closing = command.owner.request_close(None)
            assert not closing.done()
            command.owner.finalize_command_outcome(OriginalOutcome.FAILED)
            report = await command.owner.close(None)
            assert report.state is CleanupOutcome.UNKNOWN
            invocation = command.owner.mutations.directory
            assert invocation is not None
            assert read_journal(invocation).original_outcome is OriginalOutcome.FAILED
        os.write(1, command.owner.invocation_id.encode("ascii"))
        await asyncio.Event().wait()
    finally:
        diagnostics.shutdown()


if __name__ == "__main__":
    asyncio.run(retain(Path(sys.argv[1]), sys.argv[2]))
