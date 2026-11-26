import asyncio
import errno
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Annotated, TypeVar, cast

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
from kora.cli.conformance import CONFORMANCE_FINISHED, CommandResult
from kora.cli.doctor import (
    DOCTOR_CHECK_FINISHED,
    DOCTOR_FINISHED,
    CheckName,
    CheckResult,
    DoctorReport,
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
    from kora.api.graph_deployment_worker import DeploymentPreparer
    from kora.db.connection import Database, DatabaseSettings
    from kora.ir.json import JsonObject
    from kora.protocols.authentication import Authenticator
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
CLIENT_CONFIGURATION_PREFIX: str = "kora: invalid client configuration"
MCP_EXTRA_REQUIRED: str = "kora: the stdio MCP server requires the kora[mcp] extra"
MCP_DISTRIBUTIONS: frozenset[str] = frozenset({"mcp", "mcp_types"})
API_HARNESS_VARIABLE: str = "KORA_API_HARNESS"
API_WORKER_UNCERTAINTY_VARIABLE: str = "KORA_API_WORKER_CLOCK_UNCERTAINTY_US"
POSTGRES_SERVING_OVERRIDES: frozenset[str] = frozenset(
    {
        "PGDATABASE",
        "PGGSSLIB",
        "PGHOST",
        "PGKRBSRVNAME",
        "PGPASSFILE",
        "PGPASSWORD",
        "PGPORT",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSSLCERT",
        "PGSSLCRL",
        "PGSSLKEY",
        "PGSSLMAXPROTOCOLVERSION",
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMODE",
        "PGSSLNEGOTIATION",
        "PGSSLROOTCERT",
        "PGTARGETSESSIONATTRS",
        "PGUSER",
        "SSLKEYLOGFILE",
    }
)


class McpServerName(StrEnum):
    AUTHORING = "authoring"
    OPERATING = "operating"


DOCTOR_DATABASE_DEADLINE_SECONDS: float = 5.0
Outcome = TypeVar("Outcome")


@dataclass(frozen=True)
class DoctorSettings:
    database_deadline_seconds: float = DOCTOR_DATABASE_DEADLINE_SECONDS
    home: Callable[[], Path] = Path.home


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


async def run_interruptible(
    work: Coroutine[object, object, Outcome],
) -> tuple[Outcome | None, bool]:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(work)
    interrupted = asyncio.Event()

    def interrupt(number: int, frame: FrameType | None) -> None:
        interrupted.set()
        loop.call_soon_threadsafe(task.cancel)

    previous = {
        value: signal.signal(value, interrupt)
        for value in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        try:
            return await task, interrupted.is_set()
        except asyncio.CancelledError:
            return None, True
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


def conformance(
    diagnostics: ProcessDiagnostics,
    protocol: str,
    name: str,
    config: str | None,
    fixtures: str | None,
    fixture_data: str | None,
) -> None:
    from kora.protocols.lifetime import OriginalOutcome

    async def invoke() -> CommandResult:
        try:
            from kora.conformance.command import ConformanceCommand, cleanup_command
            from kora.conformance.result import ChildInput, InputFiles
        except ImportError:
            return CommandResult(outcome=OriginalOutcome.FAILED, reason="refused")
        if protocol == "cleanup":
            if any(value is not None for value in (config, fixtures, fixture_data)):
                return CommandResult(outcome=OriginalOutcome.FAILED, reason="refused")
            return await cleanup_command(name)
        try:
            request = ChildInput.model_validate(
                {
                    "protocol": protocol,
                    "name": name,
                    "fixtures": fixtures,
                    "files": InputFiles(
                        configuration=None
                        if config is None
                        else os.path.abspath(config),
                        fixture_data=None
                        if fixture_data is None
                        else os.path.abspath(fixture_data),
                    ),
                }
            )
        except ValidationError:
            return CommandResult(outcome=OriginalOutcome.FAILED, reason="refused")
        return await ConformanceCommand(diagnostics).run(request)

    async def interrupted() -> CommandResult:
        completed, _ = await run_interruptible(invoke())
        if completed is None:
            return CommandResult(outcome=OriginalOutcome.CANCELLED, reason="cancelled")
        return completed

    result = asyncio.run(interrupted())
    diagnostics.logger(CLI_COMPONENT).emit(
        CONFORMANCE_FINISHED,
        **result.model_dump(exclude={"cleanup"}, exclude_none=True, mode="json"),
    )
    typer.echo(result.model_dump_json())
    raise typer.Exit(result.exit_code)


def serve_mcp(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    server: McpServerName,
    url: str | None,
) -> None:
    from kora.client.settings import (
        MCP_CREDENTIAL_VARIABLE,
        ClientConfigurationError,
        read_client_settings,
    )

    try:
        from kora.mcp.stdio import serve_stdio
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] not in MCP_DISTRIBUTIONS:
            raise
        write_stderr(f"{MCP_EXTRA_REQUIRED}\n")
        raise typer.Exit(EXIT_CONFIGURATION) from None
    try:
        settings = read_client_settings(environment, MCP_CREDENTIAL_VARIABLE, url)
    except ClientConfigurationError as error:
        write_stderr(
            f"{CLIENT_CONFIGURATION_PREFIX}: {error.variable}: {error.problem.value}\n"
        )
        raise typer.Exit(EXIT_CONFIGURATION) from None
    asyncio.run(serve_stdio(server.value, settings, diagnostics))


def installed_versions(expected: Mapping[str, str]) -> bool:
    try:
        return all(version(name) == required for name, required in expected.items())
    except PackageNotFoundError:
        return False


def postgres_serving_candidate(
    settings: "DatabaseSettings", environment: Mapping[str, str]
) -> bool:
    query = settings.url.query
    certificate = query.get("sslrootcert")
    port = settings.url.port
    return (
        installed_versions({"asyncpg": "0.32.0", "SQLAlchemy": "2.1.5"})
        and settings.url.host == "127.0.0.1"
        and port is not None
        and 1 <= port <= 65535
        and query.keys() <= {"sslmode", "sslrootcert", "target_session_attrs"}
        and query.get("sslmode") == "verify-full"
        and query.get("target_session_attrs", "any") == "any"
        and isinstance(certificate, str)
        and Path(certificate).is_absolute()
        and Path(certificate).is_file()
        and POSTGRES_SERVING_OVERRIDES.isdisjoint(environment)
    )


def serving_deployment_preparer(clock: Clock) -> "DeploymentPreparer":
    from kora.api.graph_deployment_worker import DeploymentPreparer
    from kora.api.serving import ServingFailure, ServingRefusal
    from kora.engine.graph_output_policy import OutputPublicationBinding
    from kora.engine.graph_parameter_evaluation import ParameterPublicationBinding
    from kora.ir.data import detach_ir_data
    from kora.ir.schema_catalogue import builtin_schema_definition
    from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
    from kora.ir.structured_output import structured_output_revision_descriptor
    from kora.plugins.artifact_type.structured_output import (
        StructuredOutputArtifactType,
    )
    from kora.plugins.artifact_type.structured_output_bindings import (
        effective_behavior_bindings,
    )
    from kora.protocols.artifact_type import BuiltinArtifactTypeBinding

    try:
        definition = builtin_schema_definition("StructuredOutput", "1")
    except (OSError, ValueError) as error:
        raise ServingFailure(ServingRefusal.DOMAIN_CONFIGURATION) from error
    descriptor = structured_output_revision_descriptor()
    if definition is None or definition.file_digest != descriptor.get("schema_sha256"):
        raise ServingFailure(ServingRefusal.DOMAIN_CONFIGURATION)
    provider = StructuredOutputArtifactType(
        {},
        BuiltinArtifactTypeBinding(
            "StructuredOutput",
            cast("JsonObject", detach_ir_data(definition.schema)),
            None,
            descriptor,
        ),
    )
    return DeploymentPreparer(
        clock,
        binding=OutputPublicationBinding(
            {
                "policy": SchemaDomain.OUTPUT.value,
                "parameters": domain_limits(SchemaDomain.OUTPUT),
            },
            effective_behavior_bindings(provider),
        ),
        parameter_binding=ParameterPublicationBinding(
            {
                "profile": SchemaDomain.PARAMETERS.value,
                "limits": domain_limits(SchemaDomain.PARAMETERS),
            }
        ),
    )


def api_dependency_refusal() -> int:
    write_stderr("kora: API startup refused (dependency_unavailable)\n")
    return EXIT_CONFIGURATION


def serve_api(
    environment: Mapping[str, str], diagnostics: ProcessDiagnostics, port: int
) -> int:
    harness = environment.get(API_HARNESS_VARIABLE)
    cel_requirement = {"common-expression-language": "0.10.0"}
    if not installed_versions({"uvicorn": "0.54.0"}) or (
        harness is None and not installed_versions(cel_requirement)
    ):
        return api_dependency_refusal()

    from kora.api.exchange import COMPONENT
    from kora.api.installed_scheduler import FIRST_CLAIM, SCHEDULER_VARIABLE
    from kora.api.serving import ServingRefusal, refuse
    from kora.db.connection import (
        DatabaseConfigurationError,
        Dialect,
        read_database_settings,
    )

    log = diagnostics.logger(COMPONENT)
    try:
        settings = read_database_settings(environment)
    except DatabaseConfigurationError:
        return refuse(log, ServingRefusal.DATABASE_CONFIGURATION, EXIT_CONFIGURATION)
    scheduler = environment.get(SCHEDULER_VARIABLE)
    if scheduler is not None and (
        scheduler != FIRST_CLAIM
        or settings.dialect is not Dialect.SQLITE
        or harness != "claude"
    ):
        return refuse(log, ServingRefusal.SCHEDULER_CONFIGURATION, EXIT_CONFIGURATION)
    if settings.dialect is Dialect.POSTGRESQL:
        if harness is not None and not installed_versions(cel_requirement):
            return api_dependency_refusal()
        from kora.api._linux_serving_barrier import (
            LinuxServingBarrierError,
            LinuxServingBarrierRefusal,
            install_linux_serving_barrier,
        )

        if not postgres_serving_candidate(settings, environment):
            return refuse(
                log, ServingRefusal.POSTGRESQL_PROFILE_UNAVAILABLE, EXIT_CONFIGURATION
            )
        try:
            install_linux_serving_barrier()
        except LinuxServingBarrierError as error:
            reason = (
                ServingRefusal.POSTGRESQL_PROFILE_UNAVAILABLE
                if error.reason is LinuxServingBarrierRefusal.UNSUPPORTED_PROFILE
                else ServingRefusal.POSTGRESQL_BARRIER_UNAVAILABLE
            )
            return refuse(log, reason, EXIT_CONFIGURATION)
        return refuse(
            log, ServingRefusal.POSTGRESQL_ACTIVATION_UNAVAILABLE, EXIT_CONFIGURATION
        )

    return serve_sqlite_api(
        settings,
        harness,
        environment.get(API_WORKER_UNCERTAINTY_VARIABLE),
        scheduler is not None,
        port,
        diagnostics,
        log,
    )


def serve_sqlite_api(
    settings: "DatabaseSettings",
    harness: str | None,
    worker_uncertainty: str | None,
    scheduler_enabled: bool,
    port: int,
    diagnostics: ProcessDiagnostics,
    log: ComponentLogger,
) -> int:
    from kora.api.application import create_application
    from kora.api.exchange import ApiExchange
    from kora.api.graph_services import GraphServices, deployment_selection
    from kora.api.installed_scheduler import installed_root_admission
    from kora.api.installed_worker import installed_worker_profile
    from kora.api.run_event_route import RunEventRoute
    from kora.api.serving import (
        ServingFailure,
        ServingRefusal,
        ServingScheduler,
        refuse,
        serve_sqlite,
    )
    from kora.api.worker_claims_route import WorkerClaimsRoute
    from kora.api.worker_registration_route import WorkerRegistrationRoute
    from kora.db.graph_value_json import GraphValueRejected
    from kora.db.identifiers import IdentifierGenerator
    from kora.engine.admission_initialization import AdmissionInitialization
    from kora.engine.worker_claim_poll import WorkerClaimPoll
    from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration
    from kora.protocols.errors import KoraProtocolError
    from kora.protocols.loading import load_provider
    from kora.protocols.metadata import ProtocolName
    from kora.scheduler.loop import first_claim_scheduler

    clock = SystemClock()
    worker = None
    if worker_uncertainty is not None:
        worker = installed_worker_profile(worker_uncertainty)
        if worker is None or harness != "claude":
            reason = (
                ServingRefusal.SCHEDULER_CONFIGURATION
                if scheduler_enabled
                else ServingRefusal.WORKER_CONFIGURATION
            )
            return refuse(log, reason, EXIT_CONFIGURATION)
    if scheduler_enabled and worker is None:
        return refuse(log, ServingRefusal.SCHEDULER_CONFIGURATION, EXIT_CONFIGURATION)
    preparer = None
    selection = None
    capabilities = LocalRuntimeConfiguration(state_root=None).capabilities()
    if harness is not None:
        if harness != "claude":
            return refuse(log, ServingRefusal.DOMAIN_CONFIGURATION, EXIT_CONFIGURATION)
        try:
            load_provider(ProtocolName.HARNESS, harness, diagnostics)
            preparer = serving_deployment_preparer(clock)
        except (
            GraphValueRejected,
            KoraProtocolError,
            OSError,
            ServingFailure,
            UnicodeDecodeError,
        ):
            return refuse(log, ServingRefusal.DOMAIN_CONFIGURATION, EXIT_CONFIGURATION)
        selection = deployment_selection(harness, capabilities)

    def application(database: "Database") -> ApiExchange:
        provider = load_provider(
            ProtocolName.AUTHENTICATION, "static_token", diagnostics
        )
        factory = cast(
            "Callable[[JsonObject, Database], Authenticator]", provider.implementation
        )
        identifiers = IdentifierGenerator(clock.now)
        deployment = (
            None
            if preparer is None or selection is None
            else GraphServices(database, preparer, selection, identifiers)
        )
        registration = claims = initialization = None
        if worker is not None:
            runtimes = {"local": capabilities}
            registration = WorkerRegistrationRoute(
                database, runtimes, clock, diagnostics
            )
            claims = WorkerClaimsRoute(
                WorkerClaimPoll(
                    database,
                    runtimes,
                    clock,
                    worker.limits,
                    worker.uncertainty,
                )
            )
            initialization = AdmissionInitialization(database, clock, identifiers.new)
        return create_application(
            factory({}, database),
            diagnostics,
            clock,
            new_instance_id,
            deployment,
            run_events=RunEventRoute(database, clock),
            worker_registration=registration,
            worker_claims=claims,
            admission_initialization=initialization,
            single_claim=worker is not None,
        )

    def scheduler(database: "Database") -> ServingScheduler:
        if worker is None or selection is None:
            raise ServingFailure(ServingRefusal.SCHEDULER_CONFIGURATION)
        admission = installed_root_admission(
            database,
            worker.limits,
            clock.now,
            IdentifierGenerator(clock.now),
            selection,
            capabilities,
        )
        return first_claim_scheduler(admission, read_seconds=worker.limits.read_seconds)

    return serve_sqlite(
        settings,
        port,
        log,
        application,
        preparer=preparer,
        worker_startup=worker is not None,
        make_scheduler=scheduler if scheduler_enabled else None,
    )


def cel_check() -> CheckResult:
    from kora.cel_eval.binding import BindingUnavailable, load_binding

    try:
        binding = load_binding()
    except BindingUnavailable as refused:
        return CheckResult.failed(CheckName.CEL, refused.refusal.value)
    return CheckResult.passed(CheckName.CEL, binding.version)


async def database_check(
    environment: Mapping[str, str], settings: DoctorSettings
) -> CheckResult:
    from kora.db.probe import DatabaseProbeFailure, probe_database

    probe = await probe_database(environment, settings.database_deadline_seconds)
    if isinstance(probe, DatabaseProbeFailure):
        return CheckResult.failed(CheckName.DATABASE, probe.value)
    return CheckResult.passed(CheckName.DATABASE, probe.value)


def installation_refused(reason: str) -> tuple[CheckResult, CheckResult]:
    return (
        CheckResult.failed(CheckName.INSTALLATION, reason),
        CheckResult.failed(CheckName.HARNESS, "installation_refused"),
    )


def installation_checks(
    environment: Mapping[str, str],
    settings: DoctorSettings,
    diagnostics: ProcessDiagnostics,
    state_root: str | None,
) -> tuple[CheckResult, ...]:
    from kora.plugins.runtime.local.harness_probe import (
        HarnessPassed,
        probe_harnesses,
        probe_installation,
    )
    from kora.plugins.runtime.local.state_probe import (
        StateRootFailure,
        probe_state_root,
    )

    probe = probe_state_root(state_root, environment, settings.home)
    if isinstance(probe, StateRootFailure):
        return (
            CheckResult.failed(CheckName.STATE_ROOT, probe.value),
            *installation_refused(StateRootFailure.UNSAFE.value),
        )
    try:
        checks = [
            CheckResult.passed(CheckName.STATE_ROOT, str(probe.path))
            if probe.failure is None
            else CheckResult.failed(CheckName.STATE_ROOT, probe.failure.value)
        ]
        installation = probe_installation(probe.anchor, probe.path, diagnostics)
        if isinstance(installation, str):
            return (*checks, *installation_refused(installation))
        checks.append(CheckResult.passed(CheckName.INSTALLATION, installation.detail))
        if not installation.configuration.harness:
            checks.append(CheckResult.failed(CheckName.HARNESS, "not_configured"))
        checks.extend(
            CheckResult.passed(CheckName.HARNESS, harness.version, harness.name)
            if isinstance(harness, HarnessPassed)
            else CheckResult.failed(CheckName.HARNESS, harness.reason, harness.name)
            for harness in probe_harnesses(diagnostics, installation.configuration)
        )
        return tuple(checks)
    finally:
        probe.anchor.close()


async def doctor_checks(
    environment: Mapping[str, str],
    settings: DoctorSettings,
    log: ComponentLogger,
    diagnostics: ProcessDiagnostics,
    state_root: str | None,
) -> DoctorReport:
    from kora.cli.app import kora_version

    checks: list[CheckResult] = []

    def record(check: CheckResult) -> None:
        checks.append(check)
        log.emit(
            DOCTOR_CHECK_FINISHED,
            check=check.name.value,
            status=check.status.value,
            **({} if check.reason is None else {"reason": check.reason}),
            **({} if check.subject is None else {"harness": check.subject}),
        )

    record(CheckResult.passed(CheckName.VERSION, kora_version()))
    record(cel_check())
    record(await database_check(environment, settings))
    for check in await asyncio.to_thread(
        installation_checks, environment, settings, diagnostics, state_root
    ):
        record(check)
    return DoctorReport(tuple(checks))


def doctor(
    diagnostics: ProcessDiagnostics,
    environment: Mapping[str, str],
    settings: DoctorSettings,
    as_json: bool,
    state_root: str | None = None,
) -> None:
    log = diagnostics.logger(CLI_COMPONENT)
    try:
        saved_stdout = os.dup(STDOUT_DESCRIPTOR)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        saved_stdout = None
    original_stdout = sys.stdout
    redirected = False
    try:
        os.dup2(STDERR_DESCRIPTOR, STDOUT_DESCRIPTOR)
        redirected = True
        with redirect_stdout(sys.stderr):
            try:
                report, interrupted = asyncio.run(
                    run_interruptible(
                        doctor_checks(
                            environment, settings, log, diagnostics, state_root
                        )
                    )
                )
            finally:
                if sys.stdout is not None:
                    sys.stdout.flush()
                if original_stdout is not None:
                    original_stdout.flush()
    finally:
        if saved_stdout is not None:
            try:
                if redirected:
                    os.dup2(saved_stdout, STDOUT_DESCRIPTOR)
            finally:
                os.close(saved_stdout)
        elif redirected:
            os.close(STDOUT_DESCRIPTOR)
    if report is None or interrupted:
        raise typer.Exit(EXIT_INTERRUPT)
    log.emit(
        DOCTOR_FINISHED,
        passed=report.passed,
        failed=report.failed,
        exit_code=report.exit_code,
    )
    typer.echo(report.render(as_json=as_json))
    raise typer.Exit(report.exit_code)


def compose_application(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    doctor_settings: DoctorSettings | None = None,
) -> typer.Typer:
    app = build_app()
    app.add_typer(build_database_app(environment), name=DATABASE_GROUP)
    app.add_typer(
        build_plugins_app(InstalledPluginDiscovery(diagnostics)), name="plugins"
    )

    @app.command(name="api", help="Serve the configured local SQLite API.")
    def serve_api_command(
        port: Annotated[int, typer.Option("--port", min=0, max=65535)] = 8000,
    ) -> None:
        raise typer.Exit(serve_api(environment, diagnostics, port))

    @app.command(name="deploy", help="Deploy a compiled graph without command files.")
    def deploy(
        path: Path,
        as_json: bool = typer.Option(False, "--json", help="Print the API response."),
    ) -> None:
        from kora.cli.graphs import deploy_command

        deploy_command(environment, diagnostics, path, as_json)

    @app.command(
        name="trigger", help="Start a deployed graph with validated parameters."
    )
    def trigger(
        graph: str,
        namespace: str | None = typer.Option(None, "--namespace"),
        graph_version: str | None = typer.Option(None, "--graph-version"),
        idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
        param: Annotated[
            list[str] | None,
            typer.Option(
                "--param", help="A literal KEY=JSON value; repeat for each key."
            ),
        ] = None,
        allow_unenforced_secrets: bool = typer.Option(
            False, "--allow-unenforced-secrets"
        ),
        as_json: bool = typer.Option(False, "--json", help="Print the API response."),
    ) -> None:
        from kora.cli.graphs import TriggerOptions, trigger_command

        trigger_command(
            environment,
            diagnostics,
            TriggerOptions(
                graph,
                namespace,
                graph_version,
                idempotency_key,
                allow_unenforced_secrets,
                tuple(param or ()),
            ),
            as_json,
        )

    @app.command(name="events", help="Read a finite page of saved run events.")
    def events(
        run: str,
        namespace: str | None = typer.Option(None, "--namespace"),
        limit: str | None = typer.Option(None, "--limit"),
        cursor: str | None = typer.Option(None, "--cursor"),
        follow: bool = typer.Option(
            False, "--follow", help="Unavailable in this profile."
        ),
        as_json: bool = typer.Option(False, "--json", help="Print the API response."),
    ) -> None:
        from kora.cli.events import events_command

        events_command(
            environment, diagnostics, run, namespace, limit, cursor, follow, as_json
        )

    @app.command(name="mcp", help="Serve the kora MCP tools over stdio.")
    def serve_mcp_command(
        server: Annotated[McpServerName, typer.Option("--server")],
        url: str | None = typer.Option(None, "--url"),
    ) -> None:
        serve_mcp(environment, diagnostics, server, url)

    @app.command(
        name="conformance", help="Check an installed provider or resume local cleanup."
    )
    def check_conformance(
        protocol: str,
        name: str,
        config: str | None = typer.Option(None, "--config"),
        fixtures: str | None = typer.Option(None, "--fixtures"),
        fixture_data: str | None = typer.Option(None, "--fixture-data"),
    ) -> None:
        conformance(diagnostics, protocol, name, config, fixtures, fixture_data)

    @app.command(name="doctor", help="Check the local installation prerequisites.")
    def check_installation(
        as_json: bool = typer.Option(False, "--json"),
        state_root: str | None = typer.Option(None, "--state-root"),
    ) -> None:
        doctor(
            diagnostics,
            environment,
            doctor_settings if doctor_settings is not None else DoctorSettings(),
            as_json,
            None if state_root is None else str(Path(state_root).absolute()),
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
    doctor_settings: DoctorSettings | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    for component in (
        "api",
        "artifact_type.structured_output",
        "cel",
        "client",
        "conformance.lifetime",
        "engine",
        "mcp",
        "scheduler",
    ):
        CATALOG.register_component(component)
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
                    application
                    or compose_application(environment, diagnostics, doctor_settings),
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
