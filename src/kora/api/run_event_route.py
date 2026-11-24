import asyncio
import re
from dataclasses import dataclass
from datetime import datetime

from anyio import CancelScope
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from kora.api.errors import MESSAGES
from kora.api.exchange import COMPONENT, current_exchange
from kora.api.graph_deployment_limits import MAX_REQUEST_BYTES
from kora.api.responses import error_response
from kora.api.run_event_query import (
    RunEventPosition,
    decode_event_position,
    parse_event_query,
)
from kora.db.connection import (
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    complete,
)
from kora.db.namespace_reads import granted_namespace_id
from kora.db.run_event_page_v1 import ACTOR_BYTES
from kora.db.run_event_reads import (
    NativeRunEvent,
    RunEventReadRejected,
    read_run_event_page,
    run_event_scope,
)
from kora.db.run_failure_summary import ENCODINGS
from kora.db.types import normalise_timestamp
from kora.ir.vcs_ref_json import decode_strict_json
from kora.models.errors import (
    ApiError,
    ApiErrorCode,
    ApiErrorEnvelope,
    ConflictDetails,
    EmptyDetails,
    ReadProjectionDetails,
)
from kora.models.run_events import PAGE_BYTES, ULID_PATTERN, RunEvent, RunEventPage
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.time import Clock

READ_BUDGET = ConflictDetails(reason="read_budget_exceeded")
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


def _text(value: object, encoding: str) -> str:
    if not isinstance(value, bytes):
        raise RunEventReadRejected()
    try:
        return value.decode(encoding)
    except UnicodeError:
        raise RunEventReadRejected() from None


def _timestamp(value: object, encoding: str) -> datetime:
    if isinstance(value, datetime):
        return normalise_timestamp(value)
    raw = _text(value, encoding)
    try:
        moment = normalise_timestamp(datetime.fromisoformat(raw))
    except ValueError:
        raise RunEventReadRejected() from None
    if moment.isoformat(timespec="microseconds") != raw:
        raise RunEventReadRejected()
    return moment


def _public_identity(value: object, prefix: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunEventReadRejected()
    return prefix + value


def decode_run_event(row: NativeRunEvent, run: str) -> RunEvent:
    identity, kind, at, actor, node, attempt, encoding_number, owned = row
    if owned not in (True, 1) or type(encoding_number) is not int:
        raise RunEventReadRejected()
    if not 1 <= encoding_number <= len(ENCODINGS):
        raise RunEventReadRejected()
    encoding = ENCODINGS[encoding_number - 1]
    event_kind = _text(kind, encoding)
    if "\0" in event_kind:
        raise RunEventReadRejected()
    return RunEvent.model_validate(
        {
            "id": _public_identity(identity, "event_"),
            "kind": event_kind,
            "at": _timestamp(at, encoding),
            "actor": decode_strict_json(
                _text(actor, encoding).encode(), ACTOR_BYTES, 1
            ),
            "run_id": run,
            "node_instance_id": _public_identity(node, "node_"),
            "attempt_id": _public_identity(attempt, "att_"),
        }
    )


def refusal(
    code: ApiErrorCode, details: ConflictDetails | ReadProjectionDetails | None = None
) -> Response:
    error = ApiError(
        code=code,
        message=MESSAGES[code]
        if details is None
        else "Reduce the page limit and retry.",
        retryable=code is ApiErrorCode.UNAVAILABLE,
        details=details or EmptyDetails(),
        request_id=current_exchange().request_id,
    )
    return error_response(ApiErrorEnvelope(error=error), method="GET")


async def watch_request(request: Request, scoped: asyncio.Event) -> ApiErrorCode:
    remaining = MAX_REQUEST_BYTES
    while (message := await request.receive())["type"] != "http.disconnect":
        remaining -= len(message.get("body", b""))
        if remaining < 0:
            await scoped.wait()
            return ApiErrorCode.VALIDATION
        await asyncio.sleep(0)
    return ApiErrorCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class RunEventRoute:
    database: Database
    clock: Clock

    async def __call__(self, ns: str, run: str, request: Request) -> Response:
        exchange = current_exchange()
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + 5
        rows = size = 0
        scoped = asyncio.Event()
        page = asyncio.create_task(self.page(ns, run, request, deadline, scoped))
        ingress = asyncio.create_task(watch_request(request, scoped))
        try:
            try:
                await asyncio.wait((page, ingress), return_when=asyncio.FIRST_COMPLETED)
                if ingress.done():
                    return refusal(ingress.result())
                response, rows = page.result()
            finally:
                for task in (page, ingress):
                    task.cancel()
                with CancelScope(shield=True):
                    await complete(
                        asyncio.gather(page, ingress, return_exceptions=True)
                    )
            if response.status_code == 200:
                if loop.time() >= deadline:
                    rows = 0
                    return refusal(ApiErrorCode.CONFLICT, READ_BUDGET)
                size = len(response.body)
            return response
        finally:
            exchange.logger.emit(
                FINISHED,
                code=exchange.code,
                rows=rows,
                bytes=size,
                duration_ms=int((loop.time() - started) * 1000),
            )

    async def page(
        self,
        ns: str,
        run: str,
        request: Request,
        deadline: float,
        scoped: asyncio.Event,
    ) -> tuple[Response, int]:
        principal = current_exchange().principal
        if principal is None:
            raise RuntimeError("an admitted personal principal is required")

        try:
            async with self.database.read_snapshot(deadline=deadline) as connection:
                namespace = await granted_namespace_id(
                    connection,
                    ns,
                    tuple(grant.namespace_id for grant in principal.namespaces),
                )
                if namespace is None:
                    return refusal(ApiErrorCode.FORBIDDEN), 0
                if re.fullmatch(f"run_{ULID_PATTERN}", run) is None:
                    return refusal(ApiErrorCode.VALIDATION), 0
                run_id = run[4:]
                if not await run_event_scope(connection, namespace, run_id):
                    return refusal(ApiErrorCode.NOT_FOUND), 0
                scoped.set()
                try:
                    query = parse_event_query(request.scope.get("query_string", b""))
                    position = decode_event_position(query, namespace, run_id)
                except ValueError:
                    return refusal(ApiErrorCode.VALIDATION), 0
                observed_at = self.clock.now()
                native_rows = await read_run_event_page(
                    connection,
                    namespace,
                    run_id,
                    None if position is None else position.ceiling,
                    None if position is None else position.after,
                    query.limit,
                )
            items = tuple(decode_run_event(row, run) for row in native_rows)
            continuation = None
            if len(items) > query.limit:
                continuation = RunEventPosition(
                    namespace=namespace,
                    run=run_id,
                    limit=query.limit,
                    ceiling=items[0].id.removeprefix("event_")
                    if position is None
                    else position.ceiling,
                    after=items[query.limit - 1].id.removeprefix("event_"),
                ).encode()
            page = RunEventPage(
                items=items[: query.limit],
                next_cursor=continuation,
                observed_at=observed_at,
            )
            content = page.model_dump_json().encode()
            if len(content) > PAGE_BYTES:
                details = ReadProjectionDetails(
                    reason="read_projection_too_large", run_id=run
                )
                return refusal(ApiErrorCode.CONFLICT, details), 0
        except (RunEventReadRejected, ValidationError):
            return refusal(ApiErrorCode.INTERNAL), 0
        except DatabaseSnapshotRejected as error:
            if error.reason is DatabaseSnapshotFailure.DEADLINE:
                return refusal(ApiErrorCode.CONFLICT, READ_BUDGET), 0
            return refusal(ApiErrorCode.UNAVAILABLE), 0
        return Response(content, media_type="application/json"), len(page.items)
