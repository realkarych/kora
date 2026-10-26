import asyncio
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import typer
import typer.main
from pydantic import TypeAdapter, ValidationError

from kora.cli import (
    CLI_COMPONENT,
    PROCESS_FAILED,
    PROCESS_STARTED,
    PROCESS_STOPPED,
    THREAD_FAILED,
    build_app,
)
from kora.cli.plugins import (
    LISTING_FINISHED,
    LISTING_RETAINED,
    DiscoveryComplete,
    FailureView,
    ListingError,
    LoadedRecord,
    PluginListing,
    ProviderHeader,
    ProviderView,
    UnavailableRecord,
    build_plugins_app,
)
from kora.db import DB_COMPONENT
from kora.observability import (
    CATALOG,
    ComponentLogger,
    LoggingConfigurationError,
    ProcessDiagnostics,
    configure_process,
    read_logging_settings,
)
from kora.observability.policy import escape_controls, truncate
from kora.protocols.loading import (
    LOADING_COMPONENT,
    PROVIDER_LOADED,
    PROVIDER_UNAVAILABLE,
)
from kora.protocols.metadata import parse_config_schema
from kora.protocols.time import Clock, SystemClock

if TYPE_CHECKING:
    from kora.runner.channels import BoundedChannel
    from kora.runner.filesystem import DirectoryAnchor
    from kora.runner.processes import GatedProcess

EXIT_SUCCESS: int = 0
EXIT_FAILURE: int = 1
EXIT_USAGE: int = 2
EXIT_SOFTWARE: int = 70
EXIT_CONFIGURATION: int = 78
EXIT_INTERRUPT: int = 130
EXIT_STATUS_MASK: int = 0xFF
STDERR_DESCRIPTOR: int = 2
STDOUT_DESCRIPTOR: int = 1
PROGRAM: str = "kora"
CONFIGURATION_PREFIX: str = "kora: invalid logging configuration"
DATABASE_CONFIGURATION_PREFIX: str = "kora: invalid database configuration"
DATABASE_GROUP: str = DB_COMPONENT
MISSING_COMMAND: str = "a command or --version is required"


def write_stderr(text: str) -> None:
    data = text.encode("utf-8", errors="replace")
    view = memoryview(data)
    try:
        while view:
            written = os.write(STDERR_DESCRIPTOR, view)
            view = view[written:]
    except OSError:
        return


def write_usage(usage: str, message: str) -> None:
    lines = [usage] if usage else []
    lines.append(f"Try '{PROGRAM} --help' for help.")
    lines.append("")
    lines.append(f"Error: {truncate(escape_controls(message))}")
    write_stderr("\n".join(lines) + "\n")


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_instance_id() -> str:
    return uuid.uuid4().hex


def build_database_app(environment: Mapping[str, str]) -> typer.Typer:
    database_app = typer.Typer(
        name=DATABASE_GROUP,
        help="Metadata database operations.",
        add_completion=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
    )

    @database_app.callback(invoke_without_command=True)
    def database_root(context: typer.Context) -> None:
        if context.invoked_subcommand is None:
            write_usage(context.get_usage(), MISSING_COMMAND)
            raise typer.Exit(EXIT_USAGE)

    @database_app.command(help="Apply every pending migration.")
    def upgrade() -> None:
        from kora.db.connection import (
            DatabaseConfigurationError,
            read_database_settings,
        )
        from kora.db.migrations import upgrade_database

        try:
            settings = read_database_settings(environment)
        except DatabaseConfigurationError as error:
            write_stderr(
                f"{DATABASE_CONFIGURATION_PREFIX}: {error.variable}: {error.reason}\n"
            )
            raise typer.Exit(EXIT_CONFIGURATION) from None
        report = asyncio.run(upgrade_database(settings))
        typer.echo(report.to_revision)

    return database_app


class InstalledPluginDiscovery:
    def __init__(
        self, diagnostics: ProcessDiagnostics, clock: Clock | None = None
    ) -> None:
        self._log = diagnostics.logger(LOADING_COMPONENT)
        self._lifecycle = diagnostics.logger(CLI_COMPONENT)
        self._clock = clock if clock is not None else SystemClock()
        self._process: GatedProcess | None = None

    @property
    def process(self) -> "GatedProcess | None":
        return self._process

    async def discover(self) -> PluginListing:
        from kora.runner.filesystem import (
            DirectoryAnchor,
            FilesystemError,
            create_directory,
        )

        if self._process is not None:
            raise ListingError("previous_discovery_unsettled")
        root_path = Path(tempfile.mkdtemp(prefix="kora-list-", dir="/tmp")).resolve()
        try:
            with ExitStack() as resources:
                root = DirectoryAnchor.open_path(root_path)
                resources.callback(root.close)
                cwd = create_directory(root, "cwd")
                resources.callback(cwd.close)
                listeners: list[socket.socket] = []
                identities: list[os.stat_result] = []
                for name in ("r", "e"):
                    endpoint = self._listener(root_path / name)
                    resources.callback(endpoint.close)
                    listeners.append(endpoint)
                    identities.append(
                        os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
                    )
                return await self._invoke(root_path, root, cwd, listeners, identities)
        except (OSError, FilesystemError) as error:
            raise ListingError(f"state_retained:{root_path}") from error

    async def _invoke(
        self,
        root_path: Path,
        root: "DirectoryAnchor",
        cwd: "DirectoryAnchor",
        listeners: list[socket.socket],
        identities: list[os.stat_result],
    ) -> PluginListing:
        from kora.protocols.lifetime import (
            EnvironmentVariable,
            ProcessDomain,
            ProcessLaunch,
            ProcessOutput,
        )
        from kora.runner.channels import BoundedChannel, ChannelError, wait_future
        from kora.runner.processes import ProcessError, start_gated

        loop = asyncio.get_running_loop()
        accepts = tuple(
            asyncio.create_task(loop.sock_accept(listener)) for listener in listeners
        )
        channels: list[BoundedChannel] = []
        readers: list[asyncio.Task[object]] = []
        combined: asyncio.Future[tuple[PluginListing, int]] | None = None
        deadline = self._clock.monotonic_ns() + 30_000_000_000
        environment = {
            "HOME": str(root_path / "cwd"),
            "TMPDIR": str(root_path / "cwd"),
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        launch = ProcessLaunch(
            executable=os.path.abspath(sys.executable),
            argv=(
                os.path.abspath(sys.executable),
                "-I",
                "-m",
                "kora.cli.plugin_child",
                str(root_path / "r"),
                str(root_path / "e"),
            ),
            environment=tuple(
                EnvironmentVariable(name=name, value=value)
                for name, value in environment.items()
            ),
            working_directory_resource_id=uuid.uuid4().hex,
            stdin=None,
            stdout=ProcessOutput.CAPTURE,
            stderr=ProcessOutput.CAPTURE,
            domain=ProcessDomain.INVOCATION,
        )
        owner = start_gated(
            launch, uuid.uuid4().hex, cwd.descriptor, deadline, self._clock
        )
        self._process = owner
        completed = False
        try:
            await owner.prepare()
            await owner.acknowledge()
            endpoints: list[socket.socket] = []
            for accepted in accepts:
                await wait_future(accepted, deadline, self._clock)
                endpoint, _ = accepted.result()
                endpoints.append(endpoint)
            channels.append(BoundedChannel(endpoints[0], self._clock))
            result_task = asyncio.create_task(self.read_result(channels[0], deadline))
            event_task = asyncio.create_task(self.read_events(endpoints[1], deadline))
            readers.extend((result_task, event_task))
            combined = asyncio.gather(result_task, event_task)
            await wait_future(combined, deadline, self._clock)
            listing = result_task.result()
            if event_task.result() != len(listing.providers) + len(listing.failures):
                raise ListingError("diagnostic_count")
            completed = True
        except (
            ChannelError,
            ProcessError,
            ValidationError,
            OSError,
            ValueError,
        ) as error:
            raise ListingError("invalid_discovery") from error
        finally:
            original = None if completed else sys.exception()
            close_started = self._clock.monotonic_ns()
            cleanup = asyncio.create_task(
                self._finish(
                    owner,
                    accepts,
                    readers,
                    combined,
                    channels,
                    listeners,
                    root_path,
                    root,
                    cwd,
                    identities,
                    close_started,
                )
            )
            cancelled = False
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
            except ListingError as cleanup_error:
                self._lifecycle.emit(
                    LISTING_RETAINED,
                    reason=cleanup_error.reason.partition(":")[0],
                    reference=root_path.name,
                )
                if original is not None:
                    raise original from cleanup_error
                if cancelled:
                    raise asyncio.CancelledError from cleanup_error
                raise
            finally:
                self._lifecycle.emit(
                    LISTING_FINISHED,
                    state="closed" if owner.closed else "unknown",
                    discarded_bytes=owner.capture_bytes,
                )
                if owner.closed:
                    self._process = None
            if cancelled:
                raise asyncio.CancelledError
        if owner.returncode != 0:
            raise ListingError("child_exit")
        return listing

    async def _finish(
        self,
        owner: "GatedProcess",
        accepts: tuple[asyncio.Task[tuple[socket.socket, object]], ...],
        readers: list[asyncio.Task[object]],
        combined: asyncio.Future[tuple[PluginListing, int]] | None,
        channels: list["BoundedChannel"],
        listeners: list[socket.socket],
        root_path: Path,
        root: "DirectoryAnchor",
        cwd: "DirectoryAnchor",
        identities: list[os.stat_result],
        close_started: int,
    ) -> None:
        from kora.runner.channels import ChannelError
        from kora.runner.filesystem import FilesystemError
        from kora.runner.processes import ProcessError

        for accepted in accepts:
            accepted.cancel()
        for reader in readers:
            reader.cancel()
        if combined is not None:
            combined.cancel()
            await asyncio.gather(combined, return_exceptions=True)
        await asyncio.gather(*accepts, *readers, return_exceptions=True)
        for channel in channels:
            channel.close()
        for accepted in accepts:
            if not accepted.cancelled() and accepted.exception() is None:
                accepted.result()[0].close()
        for listener in listeners:
            listener.close()
        try:
            await self._close(owner, close_started)
        except (ChannelError, ProcessError, OSError) as error:
            evidence = json.dumps(
                {
                    "process": None
                    if owner.registration is None
                    else owner.registration.model_dump(mode="json"),
                    "state": "closure_unknown",
                }
            ).encode()
            try:
                descriptor = os.open(
                    "closure.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root.descriptor,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(evidence)
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(root.descriptor)
            except OSError as storage_error:
                raise ListingError(
                    f"closure_unknown_evidence_failed:{root_path}"
                ) from storage_error
            raise ListingError(f"closure_unknown:{root_path}") from error
        try:
            self._remove_root(root_path, root, cwd, identities)
        except (OSError, FilesystemError) as error:
            raise ListingError(f"state_retained:{root_path}") from error

    @staticmethod
    def _listener(path: Path) -> socket.socket:
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint.bind(str(path))
            os.chmod(path, 0o600)
            endpoint.listen(1)
            endpoint.setblocking(False)
            return endpoint
        except OSError:
            endpoint.close()
            raise

    @staticmethod
    def _remove_root(
        path: Path,
        root: "DirectoryAnchor",
        cwd: "DirectoryAnchor",
        identities: list[os.stat_result],
    ) -> None:
        root.verify()
        cwd.verify()
        for name, expected in zip(("r", "e"), identities, strict=True):
            metadata = os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (metadata.st_dev, metadata.st_ino)
                != (expected.st_dev, expected.st_ino)
            ):
                raise OSError("socket_identity")
        actual_cwd = os.stat("cwd", dir_fd=root.descriptor, follow_symlinks=False)
        if (actual_cwd.st_dev, actual_cwd.st_ino) != (
            cwd.identity.device,
            cwd.identity.inode,
        ):
            raise OSError("directory_identity")
        actual_root = path.lstat()
        if (actual_root.st_dev, actual_root.st_ino) != (
            root.identity.device,
            root.identity.inode,
        ):
            raise OSError("root_identity")
        for name in ("r", "e"):
            os.unlink(name, dir_fd=root.descriptor)
        os.rmdir("cwd", dir_fd=root.descriptor)
        path.rmdir()

    async def read_result(
        self, channel: "BoundedChannel", deadline: int
    ) -> PluginListing:
        providers: list[ProviderView] = []
        failures: list[FailureView] = []
        adapter = TypeAdapter(ProviderHeader | FailureView | DiscoveryComplete)
        while True:
            record = adapter.validate_json(await channel.read_record(deadline, 4096))
            if isinstance(record, ProviderHeader):
                schema = parse_config_schema(
                    TypeAdapter(object).validate_json(
                        await channel.read_record(deadline, 1048576)
                    )
                )
                providers.append(
                    ProviderView.model_validate(
                        {**record.model_dump(exclude={"kind"}), "config_schema": schema}
                    )
                )
            elif isinstance(record, FailureView):
                failures.append(record)
            else:
                if (
                    record.providers != len(providers)
                    or record.failures != len(failures)
                    or not record.diagnostics_ok
                ):
                    raise ListingError("incomplete_discovery")
                await channel.expect_eof(deadline)
                return PluginListing(tuple(providers), tuple(failures))

    async def read_events(self, endpoint: socket.socket, deadline: int) -> int:
        from kora.runner.channels import wait_future

        count = 0
        pending = b""
        adapter = TypeAdapter(LoadedRecord | UnavailableRecord)
        while True:
            received = asyncio.create_task(
                asyncio.get_running_loop().sock_recv(endpoint, 16384)
            )
            try:
                await wait_future(received, deadline, self._clock)
                chunk = received.result()
            finally:
                received.cancel()
                await asyncio.gather(received, return_exceptions=True)
            if not chunk:
                if pending:
                    raise ListingError("truncated_diagnostic")
                return count
            pending += chunk
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                if len(line) > 16384:
                    raise ListingError("diagnostic_capacity")
                record = adapter.validate_json(line)
                spec = (
                    PROVIDER_LOADED
                    if isinstance(record, LoadedRecord)
                    else PROVIDER_UNAVAILABLE
                )
                fields = record.model_dump(
                    mode="json", include=set(spec.fields), exclude_none=True
                )
                self._log.emit(spec, **fields)
                count += 1
                pending = remainder
            if len(pending) > 16384:
                raise ListingError("diagnostic_capacity")

    async def _close(self, owner: "GatedProcess", started: int) -> None:
        from kora.runner.channels import ChannelError
        from kora.runner.processes import ProcessError

        owner.close_gate()
        for instant, requested in (
            (started + 5_000_000_000, None),
            (started + 7_000_000_000, signal.SIGTERM),
            (started + 12_000_000_000, signal.SIGKILL),
        ):
            if requested is not None:
                owner.signal(requested, instant)
            try:
                await owner.settle(instant)
            except (ChannelError, ProcessError):
                if requested is signal.SIGKILL:
                    raise
            else:
                break
        await owner.drain(started + 14_000_000_000)
        owner.release(started + 14_000_000_000)


def compose_application(
    environment: Mapping[str, str], diagnostics: ProcessDiagnostics
) -> typer.Typer:
    app = build_app()
    app.add_typer(build_database_app(environment), name=DATABASE_GROUP)
    app.add_typer(
        build_plugins_app(InstalledPluginDiscovery(diagnostics)), name="plugins"
    )
    return app


def run_application(app: typer.Typer, argv: Sequence[str]) -> int:
    command = typer.main.get_command(app)
    try:
        with command.make_context(PROGRAM, list(argv)) as context:
            command.invoke(context)
            if context.invoked_subcommand is None:
                write_usage(context.get_usage(), MISSING_COMMAND)
                return EXIT_USAGE
    except typer.Exit as exit_request:
        return exit_request.exit_code & EXIT_STATUS_MASK
    except typer.Abort:
        return EXIT_FAILURE
    except typer.TyperException as error:
        get_usage = getattr(getattr(error, "ctx", None), "get_usage", None)
        usage = str(get_usage()) if callable(get_usage) else ""
        write_usage(usage, error.format_message())
        return error.exit_code
    return EXIT_SUCCESS


class ThreadFailureHook:
    def __init__(self, log: ComponentLogger) -> None:
        self._log = log
        self._previous: Callable[[threading.ExceptHookArgs], object] | None = None

    def __enter__(self) -> "ThreadFailureHook":
        self._previous = threading.excepthook
        threading.excepthook = self._handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._previous is not None:
            threading.excepthook = self._previous

    def _handle(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            self._log.emit(THREAD_FAILED, exception=args.exc_value)


def exit_code_of(request: SystemExit) -> int:
    code = request.code
    if code is None:
        return EXIT_SUCCESS
    if isinstance(code, int):
        return code & EXIT_STATUS_MASK
    write_stderr(f"{truncate(escape_controls(str(code)))}\n")
    return EXIT_FAILURE


def silence_broken_stdout() -> int:
    devnull = os.open(os.devnull, os.O_WRONLY)
    if devnull != STDOUT_DESCRIPTOR:
        os.dup2(devnull, STDOUT_DESCRIPTOR)
        os.close(devnull)
    return EXIT_FAILURE


def flush_stdout(exit_code: int) -> int:
    stream = sys.stdout
    if stream is None:
        return exit_code
    try:
        stream.flush()
    except ValueError:
        return exit_code
    except OSError:
        silence_broken_stdout()
        return exit_code if exit_code != EXIT_SUCCESS else EXIT_FAILURE
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    application: typer.Typer | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    try:
        settings = read_logging_settings(environment, CATALOG.components())
    except LoggingConfigurationError as error:
        write_stderr(f"{CONFIGURATION_PREFIX}: {error.variable}: {error.reason}\n")
        return EXIT_CONFIGURATION
    diagnostics = configure_process(CLI_COMPONENT, settings, new_instance_id(), utc_now)
    log = diagnostics.logger(CLI_COMPONENT, name="kora.cli")
    log.emit(PROCESS_STARTED)
    exit_code = EXIT_SOFTWARE
    try:
        with ThreadFailureHook(log):
            try:
                exit_code = run_application(
                    application or compose_application(environment, diagnostics),
                    arguments,
                )
            except KeyboardInterrupt:
                exit_code = EXIT_INTERRUPT
            except SystemExit as request:
                exit_code = exit_code_of(request)
            except BrokenPipeError:
                exit_code = silence_broken_stdout()
            except BaseException as error:
                log.emit(PROCESS_FAILED, exception=error, exit_code=EXIT_SOFTWARE)
                exit_code = EXIT_SOFTWARE
    finally:
        exit_code = flush_stdout(exit_code)
        log.emit(PROCESS_STOPPED, exit_code=exit_code)
        diagnostics.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
