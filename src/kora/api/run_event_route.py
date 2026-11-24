import asyncio
import re
from dataclasses import dataclass

from anyio import create_task_group
from anyio.lowlevel import checkpoint
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from kora.api.errors import (
    DependencyRetryContext,
    definitive_error,
    dependency_unavailable,
)
from kora.api.exchange import COMPONENT, current_exchange
from kora.api.responses import error_response
from kora.api.run_event_query import (
    RunEventPosition,
    RunEventQueryRejected,
    decode_event_position,
    parse_event_query,
)
from kora.db.connection import Database, DatabaseSnapshotRejected, require_snapshot_time
from kora.db.namespace_reads import granted_namespace_id
from kora.db.run_event_reads import (
    RunEventReadRejected,
    RunEventRow,
    decode_run_event_page,
    read_run_event_page,
    run_event_scope,
)
from kora.models.errors import (
    ApiError,
    ApiErrorCode,
    ApiErrorEnvelope,
    ReadProjectionDetails,
)
from kora.models.run_events import PAGE_BYTES, ULID_PATTERN, RunEvent, RunEventPage
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.time import Clock

FINISHED = CATALOG.register(
    EventSpec(
        "api.run_events.finished",
        COMPONENT,
        Level.DEBUG,
        "run event metadata read finished",
        {
            "code": FieldKind.STRING,
            "rows": FieldKind.INTEGER,
            "bytes": FieldKind.INTEGER,
            "duration_ms": FieldKind.INTEGER,
        },
    )
)


def public_event(row: RunEventRow) -> RunEvent:
    return RunEvent.model_validate(
        {
            "id": "event_" + row.id,
            "kind": row.kind,
            "at": row.at,
            "actor": row.actor,
            "run_id": "run_" + row.run_id,
            "node_instance_id": None
            if row.node_instance_id is None
            else "node_" + row.node_instance_id,
            "attempt_id": None if row.attempt_id is None else "att_" + row.attempt_id,
        }
    )


@dataclass(frozen=True, slots=True)
class RunEventRoute:
    database: Database
    clock: Clock

    async def __call__(self, ns: str, run: str, request: Request) -> Response:
        exchange = current_exchange()
        started = asyncio.get_running_loop().time()
        rows = size = 0
        response: Response | None = None
        try:
            async with create_task_group() as group:

                async def disconnect() -> None:
                    while (await request.receive())["type"] != "http.disconnect":
                        await checkpoint()
                    group.cancel_scope.cancel()

                group.start_soon(disconnect)
                response, rows, size = await self.page(ns, run, request, started + 5)
                group.cancel_scope.cancel()
            return self.unavailable() if response is None else response
        finally:
            exchange.logger.emit(
                FINISHED,
                code=exchange.code,
                rows=rows,
                bytes=size,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
            )

    def unavailable(self) -> Response:
        return error_response(
            dependency_unavailable(
                DependencyRetryContext.READ, request_id=current_exchange().request_id
            ),
            method="GET",
        )

    async def page(
        self, ns: str, run: str, request: Request, deadline: float
    ) -> tuple[Response, int, int]:
        exchange = current_exchange()
        principal = exchange.principal
        if principal is None:
            raise RuntimeError("an admitted personal principal is required")

        def refuse(code: ApiErrorCode) -> tuple[Response, int, int]:
            return (
                error_response(
                    definitive_error(code, request_id=exchange.request_id), method="GET"
                ),
                0,
                0,
            )

        try:
            async with self.database.read_snapshot(deadline=deadline) as connection:
                namespace = await granted_namespace_id(
                    connection,
                    ns,
                    tuple(grant.namespace_id for grant in principal.namespaces),
                )
                if namespace is None:
                    return refuse(ApiErrorCode.FORBIDDEN)
                if re.fullmatch(f"run_{ULID_PATTERN}", run) is None:
                    return refuse(ApiErrorCode.VALIDATION)
                run_id = run[4:]
                if not await run_event_scope(connection, namespace, run_id):
                    return refuse(ApiErrorCode.NOT_FOUND)
                query = parse_event_query(request.scope.get("query_string", b""))
                position = decode_event_position(query, namespace, run_id)
                observed_at = self.clock.now()
                native_rows = await read_run_event_page(
                    connection,
                    namespace,
                    run_id,
                    None if position is None else position.ceiling,
                    None if position is None else position.after,
                    query.limit,
                )
            rows = decode_run_event_page(native_rows)
            items = tuple(public_event(row) for row in rows[: query.limit])
            continuation = None
            if len(rows) > query.limit:
                continuation = RunEventPosition(
                    namespace=namespace,
                    run=run_id,
                    limit=query.limit,
                    ceiling=rows[0].id if position is None else position.ceiling,
                    after=rows[query.limit - 1].id,
                ).encode()
            content = (
                RunEventPage(
                    items=items, next_cursor=continuation, observed_at=observed_at
                )
                .model_dump_json()
                .encode()
            )
            require_snapshot_time(deadline)
            if len(content) > PAGE_BYTES:
                envelope = ApiErrorEnvelope(
                    error=ApiError(
                        code=ApiErrorCode.CONFLICT,
                        message="Reduce the page limit and retry.",
                        retryable=False,
                        request_id=exchange.request_id,
                        details=ReadProjectionDetails(
                            reason="read_projection_too_large", run_id=run
                        ),
                    )
                )
                return error_response(envelope, method="GET"), 0, 0
        except RunEventQueryRejected:
            return refuse(ApiErrorCode.VALIDATION)
        except (RunEventReadRejected, ValidationError):
            return refuse(ApiErrorCode.INTERNAL)
        except DatabaseSnapshotRejected:
            return self.unavailable(), 0, 0
        return (
            Response(content, media_type="application/json"),
            len(items),
            len(content),
        )
