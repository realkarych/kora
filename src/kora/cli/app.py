from importlib.metadata import version

import typer

from kora.observability import CATALOG, EventSpec, FieldKind, Level

PROGRAM_NAME: str = "kora"
CLI_COMPONENT: str = CATALOG.register_component("cli")

PROCESS_STARTED: EventSpec = CATALOG.register(
    EventSpec(
        name="process.started",
        component=CLI_COMPONENT,
        level=Level.DEBUG,
        message="process started",
        fields={},
    )
)
PROCESS_STOPPED: EventSpec = CATALOG.register(
    EventSpec(
        name="process.stopped",
        component=CLI_COMPONENT,
        level=Level.DEBUG,
        message="process stopped",
        fields={"exit_code": FieldKind.INTEGER},
    )
)
PROCESS_FAILED: EventSpec = CATALOG.register(
    EventSpec(
        name="process.failed",
        component=CLI_COMPONENT,
        level=Level.CRITICAL,
        message="the process stopped on an unhandled error",
        fields={"exception": FieldKind.EXCEPTION, "exit_code": FieldKind.INTEGER},
    )
)


def kora_version() -> str:
    return version(PROGRAM_NAME)


def show_version(value: bool) -> None:
    if value:
        typer.echo(f"{PROGRAM_NAME} {kora_version()}")
        raise typer.Exit()


def build_app() -> typer.Typer:
    app = typer.Typer(
        name=PROGRAM_NAME,
        add_completion=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
    )

    @app.callback(invoke_without_command=True)
    def root(
        show: bool = typer.Option(
            False,
            "--version",
            callback=show_version,
            is_eager=True,
            help="Print the kora version and exit.",
        ),
    ) -> None:
        return None

    return app
