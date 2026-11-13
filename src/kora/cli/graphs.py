import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import typer
from pydantic import ValidationError

from kora.cli.app import CLI_COMPONENT
from kora.client import ApiError, ClientError, KoraClient
from kora.client.documents import GraphDocumentError, read_graph_deployment
from kora.client.settings import (
    CLI_CREDENTIAL_VARIABLE,
    ClientConfigurationError,
    read_client_settings,
    read_namespace,
)
from kora.client.transport import IDEMPOTENCY_KEY, DeployedGraphVersion, TriggeredRun
from kora.ir.json import JsonObject
from kora.models.graphs import OpaqueObject
from kora.models.runs import SkippedRun, TriggerRequest
from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    ProcessDiagnostics,
)

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_CONFIGURATION = 78
CONFIGURATION_PREFIX = "kora: invalid client configuration"
DOCUMENT_PREFIX = "kora: invalid graph document"
Result = TypeVar("Result")
DEPLOY_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "cli.deploy.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora deploy finished",
        {"outcome": FieldKind.STRING, "code": FieldKind.STRING},
    )
)

TRIGGER_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "cli.trigger.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora trigger finished",
        {"outcome": FieldKind.STRING, "code": FieldKind.STRING},
    )
)


def _stderr(text: str) -> None:
    typer.echo(text, err=True)


async def _deploy(
    url: str, token: str, path: Path, diagnostics: ProcessDiagnostics
) -> DeployedGraphVersion:
    deployment = read_graph_deployment(path)
    async with KoraClient(url, token, diagnostics=diagnostics) as client:
        return await client.deploy(deployment.namespace, deployment.request)


def _summary(deployed: DeployedGraphVersion) -> str:
    response = deployed.response
    action = "reused" if response.reused else "published"
    return (
        f"{action} graph version {response.graph_version_id} "
        f"(number {response.number}, deployment {response.deployment_digest})"
    )


def _report(
    diagnostics: ProcessDiagnostics,
    finished: EventSpec,
    call: Callable[[], Coroutine[None, None, Result]],
    as_json: bool,
    summary: Callable[[Result], str],
    document: Callable[[Result], JsonObject],
) -> None:
    logger = diagnostics.logger(CLI_COMPONENT)
    try:
        result = asyncio.run(call())
    except ClientConfigurationError as error:
        _stderr(f"{CONFIGURATION_PREFIX}: {error.variable}: {error.problem.value}")
        raise typer.Exit(EXIT_CONFIGURATION) from None
    except GraphDocumentError as error:
        _stderr(f"{DOCUMENT_PREFIX}: {error.problem.value}")
        raise typer.Exit(EXIT_USAGE) from None
    except ApiError as error:
        logger.emit(finished, outcome="api_error", code=error.code)
        typer.echo(error.content.decode("utf-8"))
        raise typer.Exit(EXIT_FAILURE) from None
    except ClientError as error:
        logger.emit(finished, outcome="client_error", code=error.code)
        if as_json:
            typer.echo(json.dumps(error.envelope()))
        else:
            _stderr(f"kora: {error.code}: {error}")
        raise typer.Exit(EXIT_FAILURE) from None
    logger.emit(finished, outcome="ok", code="none")
    typer.echo(json.dumps(document(result)) if as_json else summary(result))


def deploy_command(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    path: Path,
    as_json: bool,
) -> None:
    async def call() -> DeployedGraphVersion:
        settings = read_client_settings(environment, CLI_CREDENTIAL_VARIABLE)
        return await _deploy(settings.url, settings.token, path, diagnostics)

    _report(
        diagnostics,
        DEPLOY_FINISHED,
        call,
        as_json,
        _summary,
        lambda deployed: deployed.document,
    )


@dataclass(frozen=True, slots=True)
class TriggerOptions:
    graph: str
    namespace: str | None
    graph_version: str | None
    idempotency_key: str | None
    allow_unenforced_secrets: bool


def _trigger_summary(triggered: TriggeredRun) -> str:
    response = triggered.response
    if isinstance(response, SkippedRun):
        return "skipped: an earlier run of the graph is still outstanding"
    return f"accepted run {response.run_id} (graph version {response.graph_version_id})"


def trigger_command(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    options: TriggerOptions,
    as_json: bool,
) -> None:
    try:
        request = TriggerRequest(
            params=OpaqueObject(),
            graph_version=options.graph_version,
            allow_unenforced_secrets=options.allow_unenforced_secrets,
        )
    except ValidationError:
        _stderr("kora: invalid --graph-version")
        raise typer.Exit(EXIT_USAGE) from None
    key = options.idempotency_key
    if key is not None and IDEMPOTENCY_KEY.fullmatch(key) is None:
        _stderr("kora: invalid --idempotency-key")
        raise typer.Exit(EXIT_USAGE)

    async def call() -> TriggeredRun:
        settings = read_client_settings(environment, CLI_CREDENTIAL_VARIABLE)
        namespace = read_namespace(environment, options.namespace)
        async with KoraClient(
            settings.url, settings.token, diagnostics=diagnostics
        ) as client:
            return await client.trigger(namespace, options.graph, request, key)

    _report(
        diagnostics,
        TRIGGER_FINISHED,
        call,
        as_json,
        _trigger_summary,
        lambda triggered: triggered.document,
    )
