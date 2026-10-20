import asyncio
import os
import sys
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType

import typer
import typer.main

from kora.cli import (
    CLI_COMPONENT,
    PROCESS_FAILED,
    PROCESS_STARTED,
    PROCESS_STOPPED,
    THREAD_FAILED,
    build_app,
)
from kora.db import DB_COMPONENT
from kora.observability import (
    CATALOG,
    ComponentLogger,
    LoggingConfigurationError,
    configure_process,
    read_logging_settings,
)
from kora.observability.policy import escape_controls, truncate

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


def compose_application(environment: Mapping[str, str]) -> typer.Typer:
    app = build_app()
    app.add_typer(build_database_app(environment), name=DATABASE_GROUP)
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
                    application or compose_application(environment), arguments
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
