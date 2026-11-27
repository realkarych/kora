import json
from pathlib import Path

import typer

from kora.cli.app import CLI_COMPONENT
from kora.client.errors import (
    COMPILE_ARGUMENT_CODE,
    INTERNAL_FAILURE_MESSAGE,
    LOCAL_VALIDATION_CODE,
    CompileArgument,
    compile_argument_findings,
    local_validation_error,
    validation_findings,
)
from kora.dsl import CompileFindings, CompileIncomplete, compile_graph
from kora.ir.diagnostic_collection import DiagnosticCollectionError
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, ProcessDiagnostics

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SOFTWARE = 70
COMPILE_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "cli.compile.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora compile finished",
        {"outcome": FieldKind.STRING, "code": FieldKind.STRING},
    )
)


def _echo_error(document: JsonObject) -> None:
    typer.echo(json.dumps(document).encode("utf-8"), err=True)


def report_compile_argument(
    diagnostics: ProcessDiagnostics, reason: CompileArgument
) -> None:
    diagnostics.logger(CLI_COMPONENT).emit(
        COMPILE_FINISHED, outcome="usage_error", code=COMPILE_ARGUMENT_CODE
    )
    _echo_error(compile_argument_findings(reason))


def compile_command(
    diagnostics: ProcessDiagnostics, path: str | None, output_format: str | None
) -> None:
    if not path or output_format != "json":
        report_compile_argument(diagnostics, "path" if not path else "format")
        raise typer.Exit(EXIT_USAGE)
    logger = diagnostics.logger(CLI_COMPONENT)
    try:
        result = compile_graph(Path(path))
        if isinstance(result, CompileFindings):
            refusal = validation_findings(result.diagnostics)
            logger.emit(COMPILE_FINISHED, outcome="invalid", code="findings")
            _echo_error(refusal)
            raise typer.Exit(EXIT_FAILURE)
        artifact = result.text.encode("utf-8")
    except DiagnosticCollectionError:
        logger.emit(
            COMPILE_FINISHED, outcome="client_error", code=LOCAL_VALIDATION_CODE
        )
        _echo_error(local_validation_error("diagnostic_count_limit"))
        raise typer.Exit(EXIT_FAILURE) from None
    except CompileIncomplete:
        logger.emit(COMPILE_FINISHED, outcome="internal_error", code="internal_error")
        typer.echo(INTERNAL_FAILURE_MESSAGE.encode("utf-8"), err=True)
        raise typer.Exit(EXIT_SOFTWARE) from None
    logger.emit(COMPILE_FINISHED, outcome="ok", code="none")
    typer.echo(artifact)
