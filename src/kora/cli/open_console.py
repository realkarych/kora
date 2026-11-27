import asyncio
import os
import sys
import time
from collections.abc import Mapping
from typing import Literal

import typer
from pydantic import ValidationError

from kora.cli.app import CLI_COMPONENT
from kora.cli.graphs import EXIT_CONFIGURATION, EXIT_FAILURE, EXIT_USAGE
from kora.client import ApiError, ClientError
from kora.client.settings import (
    CLI_CREDENTIAL_VARIABLE,
    ClientConfigurationError,
    read_client_settings,
    read_namespace,
)
from kora.models.console import LoginSelection
from kora.observability import CATALOG, EventSpec, FieldKind, Level, ProcessDiagnostics

OPENERS = {"linux": "/usr/bin/xdg-open", "darwin": "/usr/bin/open"}
DESKTOP_ENV = frozenset(
    (
        "DBUS_SESSION_BUS_ADDRESS DESKTOP_SESSION DISPLAY HOME PATH "
        "WAYLAND_DISPLAY XDG_CONFIG_DIRS XDG_CONFIG_HOME XDG_CURRENT_DESKTOP "
        "XDG_DATA_DIRS XDG_DATA_HOME XDG_RUNTIME_DIR XDG_SESSION_DESKTOP"
    ).split()
)
FINISHED = CATALOG.register(
    EventSpec(
        "cli.open.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora open finished",
        {"outcome": FieldKind.STRING},
    )
)


def launch_browser(
    url: str, opener: str, environment: Mapping[str, str]
) -> bool | None:
    variables = {name: environment[name] for name in DESKTOP_ENV & environment.keys()}
    actions: list[tuple[Literal[0], int, str, int, int] | tuple[Literal[1], int]] = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
        (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
    ]
    try:
        with os.scandir("/dev/fd") as descriptors:
            for index, entry in enumerate(descriptors):
                if index == 1024:
                    return False
                descriptor = int(entry.name)
                if descriptor > 2 and os.get_inheritable(descriptor):
                    actions.append((os.POSIX_SPAWN_CLOSE, descriptor))
        pid = os.posix_spawn(
            opener, (opener, url), variables, file_actions=actions, setpgroup=0
        )
    except OSError:
        return False
    deadline = time.monotonic() + 10
    while True:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            return os.waitstatus_to_exitcode(status) == 0
        if (remaining := deadline - time.monotonic()) <= 0:
            return None
        time.sleep(min(0.025, remaining))


def open_console_command(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    run: str,
    namespace: str | None,
) -> None:
    logger = diagnostics.logger(CLI_COMPONENT)

    def fail(outcome: str, message: str, code: int) -> typer.Exit:
        logger.emit(FINISHED, outcome=outcome)
        typer.echo(f"kora: {message}", err=True)
        return typer.Exit(code)

    async def request_link() -> str:
        settings = read_client_settings(environment, CLI_CREDENTIAL_VARIABLE)
        selection = LoginSelection(
            namespace=read_namespace(environment, namespace), run_id=run
        )
        async with settings.client(diagnostics) as client:
            return await client.issue_console_login_link(selection)

    try:
        link = asyncio.run(request_link())
    except ClientConfigurationError as error:
        raise fail(
            "configuration",
            f"invalid client configuration: {error.variable}.",
            EXIT_CONFIGURATION,
        ) from None
    except ValidationError:
        raise fail("selection", "invalid run selection.", EXIT_USAGE) from None
    except (ApiError, ClientError):
        raise fail(
            "link_refused", "could not obtain a browser sign-in link.", EXIT_FAILURE
        ) from None
    opened = launch_browser(link, OPENERS[sys.platform], environment)
    if opened is None:
        raise fail(
            "opener_unconfirmed",
            "browser handoff is unconfirmed. Check for the opened tab.",
            EXIT_FAILURE,
        )
    if not opened:
        raise fail("opener_refused", "could not open the browser.", EXIT_FAILURE)
    logger.emit(FINISHED, outcome="opened")
