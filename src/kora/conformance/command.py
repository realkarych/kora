import asyncio
import os
import socket
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from kora.cli.conformance import CommandResult
from kora.cli.plugins import LoadedRecord, UnavailableRecord
from kora.conformance.io_helper import (
    CreateListenersMutation,
    HelperError,
    IOHelperLauncher,
)
from kora.conformance.lifetime import LifetimeError, ParentLifetime, resume_close
from kora.conformance.result import ChildInput, ChildResult
from kora.conformance.transport import LifetimeEndpoint
from kora.observability import ProcessDiagnostics
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    EnvironmentVariable,
    LifetimeLimits,
    OriginalOutcome,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.loading import (
    LOADING_COMPONENT,
    PROVIDER_LOADED,
    PROVIDER_UNAVAILABLE,
)
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel, ChannelError, wait_future
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora.runner.processes import GatedProcess, ProcessError


class ConformanceCommand:
    def __init__(
        self,
        diagnostics: ProcessDiagnostics,
        *,
        clock: Clock | None = None,
        launcher: IOHelperLauncher | None = None,
    ) -> None:
        self.diagnostics = diagnostics
        self.owner = ParentLifetime(
            clock=clock, launcher=launcher, await_command_outcome=True
        )
        self.startup: DirectoryAnchor | None = None
        self.process: GatedProcess | None = None
        self.listeners: list[socket.socket] = []
        self.identities: list[os.stat_result] = []
        self.accepts: list[asyncio.Task[tuple[socket.socket, object]]] = []
        self.tasks: list[asyncio.Task[object]] = []
        self.resources = ExitStack()
        self.channel: BoundedChannel | None = None
        self.result = ChildResult(phase="bootstrap")
        self.outcome = OriginalOutcome.COMPLETE

    async def prepare_channels(self) -> str:
        await self.owner.ready()
        tree = await self.owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        acquired = await self.owner.acquire(
            tree.token, new_operation_id(), TemporaryTreeRequest()
        )
        await self.owner.acknowledge(
            tree.token, acquired.operation_id, acquired.revision
        )
        self.startup = self.owner.records[tree.token.resource_id].directory
        if self.startup is None:
            raise ValueError("missing_startup_tree")
        try:
            result = await self.owner.mutations.perform(
                CreateListenersMutation(directory=self.startup.identity),
                self.startup,
                self.owner.deadline(),
            )
            helper = next(
                helper
                for helper in self.owner.mutations.helpers
                if helper.result is result
            )
            descriptors = helper.take_descriptors()
            with ExitStack() as received:
                for descriptor in descriptors:
                    received.callback(os.close, descriptor)
                if len(descriptors) != 3:
                    raise ValueError("listener_count")
                for descriptor, name in zip(descriptors, ("r", "e", "l"), strict=True):
                    if not stat.S_ISSOCK(os.fstat(descriptor).st_mode):
                        raise ValueError("listener_type")
                    listener = self.resources.enter_context(
                        socket.socket(fileno=os.dup(descriptor))
                    )
                    listener.setblocking(False)
                    self.listeners.append(listener)
                    self.identities.append(
                        os.stat(
                            name, dir_fd=self.startup.descriptor, follow_symlinks=False
                        )
                    )
            self.owner.mutations.prune()
        finally:
            caller = asyncio.current_task()
            for helper, origin in self.owner.mutations.origins.items():
                if origin.task is caller:
                    for descriptor in helper.take_descriptors():
                        os.close(descriptor)
        return tree.token.resource_id

    def unlink_listeners(self) -> None:
        for listener in self.listeners:
            listener.close()
        if self.startup is None or not self.identities:
            return
        self.startup.verify()
        for name, expected in zip(("r", "e", "l"), self.identities, strict=False):
            actual = os.stat(
                name, dir_fd=self.startup.descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISSOCK(actual.st_mode)
                or actual.st_uid != os.getuid()
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise ValueError("listener_identity")
        while self.identities:
            name = ("r", "e", "l")[len(self.identities) - 1]
            os.unlink(name, dir_fd=self.startup.descriptor)
            self.identities.pop()

    async def _launch(self, tree: str, deadline: int) -> str:
        snapshot = self.owner.mutations.snapshot
        if snapshot is None:
            raise ValueError("missing_startup_snapshot")
        directory = Path(
            "/",
            *(part.name for part in snapshot.ancestors),
            self.owner.invocation_id,
            tree,
        )
        environment = {
            "HOME": str(directory),
            "TMPDIR": str(directory),
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        child = await self.owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.PROCESS_DOMAIN,
                parent_resource_id=tree,
            )
        )
        acquired = await self.owner.acquire(
            child.token,
            new_operation_id(),
            ProcessLaunch(
                executable=os.path.abspath(sys.executable),
                argv=(
                    os.path.abspath(sys.executable),
                    "-I",
                    "-W",
                    "error",
                    "-m",
                    "kora.conformance.child",
                    "r",
                    "e",
                    "l",
                ),
                environment=tuple(
                    EnvironmentVariable(name=key, value=value)
                    for key, value in environment.items()
                ),
                working_directory_resource_id=tree,
                stdin=None,
                stdout=ProcessOutput.CAPTURE,
                stderr=ProcessOutput.CAPTURE,
                domain=ProcessDomain.INVOCATION,
            ),
        )
        await self.owner.acknowledge(
            child.token, acquired.operation_id, acquired.revision
        )
        self.process = self.owner.records[child.token.resource_id].process
        if self.process is None:
            raise ValueError("missing_child")
        self.accepts = [
            asyncio.create_task(asyncio.get_running_loop().sock_accept(listener))
            for listener in self.listeners
        ]
        exited = asyncio.create_task(self.process.wait_exit(deadline))
        self.tasks.append(exited)
        try:
            for accepted in self.accepts:
                await asyncio.wait(
                    (accepted, exited), return_when=asyncio.FIRST_COMPLETED
                )
                if not accepted.done():
                    exited.result()
                    raise ValueError("child_bootstrap_exit")
                accepted.result()
        finally:
            exited.cancel()
            await asyncio.gather(exited, return_exceptions=True)
        return child.token.resource_id

    async def _read_events(self, endpoint: socket.socket, deadline: int) -> None:
        adapter = TypeAdapter(LoadedRecord | UnavailableRecord)
        data = bytearray()
        while True:
            reading = asyncio.create_task(
                asyncio.get_running_loop().sock_recv(endpoint, 16384)
            )
            try:
                await wait_future(reading, deadline, self.owner.clock)
                chunk = reading.result()
            finally:
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 16384:
                raise ValueError("bootstrap_diagnostic_capacity")
        if not data:
            return
        record = adapter.validate_json(bytes(data))
        spec = (
            PROVIDER_LOADED
            if isinstance(record, LoadedRecord)
            else PROVIDER_UNAVAILABLE
        )
        self.diagnostics.logger(LOADING_COMPONENT).emit(
            spec,
            **record.model_dump(
                mode="json", include=set(spec.fields), exclude_none=True
            ),
        )

    async def _read_result(self) -> ChildResult:
        if self.channel is None:
            raise ValueError("missing_result_channel")
        await self.channel.wait_readable()
        return ChildResult.model_validate_json(
            await self.channel.read_record(self.owner.deadline(), 4096)
        )

    async def _start(
        self, request: ChildInput, deadline: int
    ) -> asyncio.Task[ClosureReport]:
        tree = await self.prepare_channels()
        child = await self._launch(tree, deadline)
        self.unlink_listeners()
        result_socket, event_socket, lifetime_socket = (
            accepted.result()[0] for accepted in self.accepts
        )
        self.channel = BoundedChannel(result_socket, self.owner.clock)
        serving = asyncio.create_task(
            LifetimeEndpoint(self.owner, child, lifetime_socket).run()
        )
        events = asyncio.create_task(self._read_events(event_socket, deadline))
        self.tasks.extend((serving, events))
        await self.channel.write_record(request.model_dump_json().encode(), deadline)
        await self.channel.write_record(b"{}", deadline)
        await self.channel.write_record(b"{}", deadline)
        self.result = ChildResult.model_validate_json(
            await self.channel.read_record(deadline, 4096)
        )
        await wait_future(events, deadline, self.owner.clock)
        if self.result.phase != "ready" or self.result.expected == 0:
            raise ValueError("bootstrap_refused")
        return serving

    async def _execute(self, request: ChildInput) -> None:
        deadline = self.owner.deadline()
        startup = asyncio.create_task(self._start(request, deadline))
        self.tasks.append(startup)
        try:
            await wait_future(startup, deadline, self.owner.clock)
            serving = startup.result()
        finally:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        reading = asyncio.create_task(self._read_result())
        self.tasks.append(reading)
        await asyncio.wait(
            (reading, serving, self.owner.invocation_cleanup.result),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not reading.done():
            raise ValueError("completion_unavailable")
        self.result = reading.result()
        if not self.result.accepted or self.process is None:
            raise ValueError("checks_refused")
        self.owner.request_close(None)
        final = self.owner.invocation_cleanup.final
        try:
            if not self.process.closed:
                await self.process.settle(final)
                await self.process.drain(final)
        except ProcessError:
            if not self.process.closed:
                raise
        if self.process.release(final) != 0:
            raise ValueError("child_exit")

    def _classify(self, outcome: OriginalOutcome) -> None:
        if self.outcome is OriginalOutcome.COMPLETE:
            self.outcome = outcome
        self.owner.set_original_outcome(outcome)

    async def _finish(self) -> ClosureReport:
        try:
            self.unlink_listeners()
        except (OSError, FilesystemError, ValueError):
            self.owner.retain_unverified_resources()
            self._classify(OriginalOutcome.FAILED)
        for task in (*self.accepts, *self.tasks):
            task.cancel()
        owned: list[asyncio.Task[object]] = [*self.accepts, *self.tasks]
        outcomes = await asyncio.gather(*owned, return_exceptions=True)
        if self.channel is not None:
            self.channel.close()
        for accepted in self.accepts:
            if not accepted.cancelled() and accepted.exception() is None:
                accepted.result()[0].close()
        self.resources.close()
        failures: list[BaseException] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                self._classify(OriginalOutcome.FAILED)
                if not isinstance(
                    outcome,
                    (ChannelError, LifetimeError, ProcessError, OSError, ValueError),
                ):
                    failures.append(outcome)
        if not self.owner.invocation_cleanup.result.done():
            self.owner.finalize_command_outcome(self.outcome)
        report = await self.owner.close(None)
        if failures:
            raise BaseExceptionGroup("command_reader_failures", failures)
        return report

    async def run(self, request: ChildInput) -> CommandResult:
        try:
            try:
                await self._execute(request)
            except asyncio.CancelledError:
                self._classify(OriginalOutcome.CANCELLED)
            except (
                ChannelError,
                LifetimeError,
                HelperError,
                ProcessError,
                FilesystemError,
                OSError,
                ValueError,
            ):
                self._classify(OriginalOutcome.FAILED)
        finally:
            if sys.exception() is not None:
                self._classify(OriginalOutcome.FAILED)
            cleanup = asyncio.create_task(self._finish())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    self._classify(OriginalOutcome.CANCELLED)
            closure = cleanup.result()
        result = CommandResult(
            outcome=closure.original_outcome,
            reason="cleanup_unknown"
            if closure.state is not CleanupOutcome.CLOSED
            else "cancelled"
            if closure.original_outcome is OriginalOutcome.CANCELLED
            else "complete"
            if closure.original_outcome is OriginalOutcome.COMPLETE
            else "refused",
            invocation_id=closure.invocation_id,
            completed=self.result.completed,
            expected=self.result.expected,
            cleanup=closure,
        )
        return result


async def cleanup_command(invocation_id: str) -> CommandResult:
    try:
        reference = CleanupReference(invocation_id=invocation_id)
    except ValidationError:
        return CommandResult(outcome=OriginalOutcome.FAILED, reason="refused")
    cleanup = asyncio.create_task(resume_close(reference, LifetimeLimits()))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
        except LifetimeError:
            break
    try:
        report = cleanup.result()
    except LifetimeError:
        return CommandResult(
            outcome=OriginalOutcome.CANCELLED
            if interrupted
            else OriginalOutcome.FAILED,
            reason="cancelled" if interrupted else "refused",
        )
    return CommandResult(
        outcome=OriginalOutcome.CANCELLED
        if interrupted
        else OriginalOutcome.COMPLETE
        if report.state is CleanupOutcome.CLOSED
        else OriginalOutcome.FAILED,
        reason="cleanup_complete"
        if report.state is CleanupOutcome.CLOSED
        else "cleanup_unknown",
        invocation_id=report.invocation_id,
        cleanup=report,
    )
