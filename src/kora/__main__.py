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
    build_app,
)
from kora.observability import (
    CATALOG,
    ComponentLogger,
    LoggingConfigurationError,
    configure_process,
    read_logging_settings,
)

EXIT_SUCCESS: int = 0
EXIT_FAILURE: int = 1
EXIT_USAGE: int = 2
EXIT_SOFTWARE: int = 70
EXIT_CONFIGURATION: int = 78
EXIT_INTERRUPT: int = 130
STDERR_DESCRIPTOR: int = 2
PROGRAM: str = "kora"
CONFIGURATION_PREFIX: str = "kora: invalid logging configuration"
MISSING_COMMAND: str = "a command or --version is required"

ExceptHook = Callable[
    [type[BaseException], BaseException, TracebackType | None], object
]


def write_stderr(text: str) -> None:
    data = text.encode("utf-8", errors="replace")
    view = memoryview(data)
    while view:
        written = os.write(STDERR_DESCRIPTOR, view)
        view = view[written:]


def write_usage(usage: str, message: str) -> None:
    lines = [usage] if usage else []
    lines.append(f"Try '{PROGRAM} --help' for help.")
    lines.append("")
    lines.append(f"Error: {message}")
    write_stderr("\n".join(lines) + "\n")


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_instance_id() -> str:
    return uuid.uuid4().hex


def run_application(app: typer.Typer, argv: Sequence[str]) -> int:
    command = typer.main.get_command(app)
    try:
        with command.make_context(PROGRAM, list(argv)) as context:
            command.invoke(context)
            if context.invoked_subcommand is None:
                write_usage(context.get_usage(), MISSING_COMMAND)
                return EXIT_USAGE
    except typer.Exit as exit_request:
        return exit_request.exit_code
    except typer.Abort:
        return EXIT_FAILURE
    except typer.TyperException as error:
        get_usage = getattr(getattr(error, "ctx", None), "get_usage", None)
        usage = str(get_usage()) if callable(get_usage) else ""
        write_usage(usage, error.format_message())
        return error.exit_code
    return EXIT_SUCCESS


class FailureHooks:
    def __init__(self, log: ComponentLogger) -> None:
        self._log = log
        self._previous_excepthook: ExceptHook | None = None
        self._previous_threading_hook: (
            Callable[[threading.ExceptHookArgs], object] | None
        ) = None

    def __enter__(self) -> "FailureHooks":
        self._previous_excepthook = sys.excepthook
        self._previous_threading_hook = threading.excepthook
        sys.excepthook = self._handle
        threading.excepthook = self._handle_thread
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._previous_excepthook is not None:
            sys.excepthook = self._previous_excepthook
        if self._previous_threading_hook is not None:
            threading.excepthook = self._previous_threading_hook

    def _handle(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        self._log.emit(PROCESS_FAILED, exception=exc_value, exit_code=EXIT_SOFTWARE)

    def _handle_thread(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            self._log.emit(
                PROCESS_FAILED, exception=args.exc_value, exit_code=EXIT_SOFTWARE
            )


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
        with FailureHooks(log):
            try:
                exit_code = run_application(application or build_app(), arguments)
            except KeyboardInterrupt:
                exit_code = EXIT_INTERRUPT
            except Exception as error:
                log.emit(PROCESS_FAILED, exception=error, exit_code=EXIT_SOFTWARE)
                exit_code = EXIT_SOFTWARE
    finally:
        log.emit(PROCESS_STOPPED, exit_code=exit_code)
        diagnostics.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
