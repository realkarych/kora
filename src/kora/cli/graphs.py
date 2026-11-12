import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import typer

from kora.cli.app import CLI_COMPONENT
from kora.client import ApiError, ClientError, KoraClient
from kora.client.documents import GraphDocumentError, read_graph_deployment
from kora.client.settings import (
    CLI_CREDENTIAL_VARIABLE,
    ClientConfigurationError,
    read_client_settings,
)
from kora.client.transport import DeployedGraphVersion
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
DEPLOY_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "cli.deploy.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora deploy finished",
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


def deploy_command(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    path: Path,
    as_json: bool,
) -> None:
    logger = diagnostics.logger(CLI_COMPONENT)
    try:
        settings = read_client_settings(environment, CLI_CREDENTIAL_VARIABLE)
        deployed = asyncio.run(_deploy(settings.url, settings.token, path, diagnostics))
    except ClientConfigurationError as error:
        _stderr(f"{CONFIGURATION_PREFIX}: {error.variable}: {error.problem.value}")
        raise typer.Exit(EXIT_CONFIGURATION) from None
    except GraphDocumentError as error:
        _stderr(f"{DOCUMENT_PREFIX}: {error.problem.value}")
        raise typer.Exit(EXIT_USAGE) from None
    except ApiError as error:
        logger.emit(DEPLOY_FINISHED, outcome="api_error", code=error.code)
        typer.echo(error.content.decode("utf-8"))
        raise typer.Exit(EXIT_FAILURE) from None
    except ClientError as error:
        logger.emit(DEPLOY_FINISHED, outcome="client_error", code=error.code)
        if as_json:
            typer.echo(json.dumps(error.envelope()))
        else:
            _stderr(f"kora: {error.code}: {error}")
        raise typer.Exit(EXIT_FAILURE) from None
    logger.emit(DEPLOY_FINISHED, outcome="ok", code="none")
    typer.echo(json.dumps(deployed.document) if as_json else _summary(deployed))
