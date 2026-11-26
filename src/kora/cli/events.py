from collections.abc import Mapping

from kora.cli.app import CLI_COMPONENT
from kora.cli.graphs import report
from kora.client.settings import (
    CLI_CREDENTIAL_VARIABLE,
    read_client_settings,
    read_namespace,
)
from kora.client.transport import ListedRunEvents
from kora.models.run_events import SystemEventActor
from kora.observability import CATALOG, EventSpec, FieldKind, Level, ProcessDiagnostics
from kora.protocols.identity import instant_wire

EVENTS_FINISHED = CATALOG.register(
    EventSpec(
        "cli.events.finished",
        CLI_COMPONENT,
        Level.DEBUG,
        "kora events finished",
        {"outcome": FieldKind.STRING, "code": FieldKind.STRING},
    )
)


def _summary(result: ListedRunEvents) -> str:
    lines: list[str] = []
    for event in result.response.items:
        actor = event.actor
        author = actor.component if isinstance(actor, SystemEventActor) else actor.owner
        kind = event.kind.encode("unicode_escape").decode("ascii")
        lines.append(f"{instant_wire(event.at)} {event.id} {kind} {author}")
    if result.response.next_cursor is not None:
        lines.append(f"Next cursor {result.response.next_cursor}")
    return "\n".join(lines) if lines else "No events."


def events_command(
    environment: Mapping[str, str],
    diagnostics: ProcessDiagnostics,
    run_id: str,
    namespace: str | None,
    limit: str | None,
    cursor: str | None,
    follow: bool,
    as_json: bool,
) -> None:
    async def call() -> ListedRunEvents:
        settings = read_client_settings(environment, CLI_CREDENTIAL_VARIABLE)
        resolved_namespace = read_namespace(environment, namespace)
        async with settings.client(diagnostics) as client:
            return await client.list_run_events(
                resolved_namespace,
                run_id,
                limit=limit,
                cursor=cursor,
                follow=follow,
            )

    report(
        diagnostics,
        EVENTS_FINISHED,
        call,
        as_json,
        _summary,
        lambda result: result.document,
    )
