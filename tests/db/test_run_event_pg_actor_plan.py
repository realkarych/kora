from dataclasses import dataclass
from hashlib import sha256

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.run_event_reads import read_run_event_page
from kora.db.tables import api_token, event
from kora.db.types import JsonObject, as_json_value
from kora.models.errors import ApiErrorCode, ApiErrorEnvelope, EmptyDetails
from kora.models.run_events import DEFAULT_LIMIT, RunEventPage
from tests.api.support import api_capture, client_for
from tests.api.test_run_events import observed_events
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.db.test_grant_history_reads import plan_nodes
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_run_event_reads import (
    SYSTEM,
    USER,
    EventScope,
    near_toast_payload,
    seed_event,
    seed_run,
)

__all__ = ["api_capture"]

CANDIDATE_CAP: int = DEFAULT_LIMIT + 1
CANARY: str = "private-actor-outside-first-page"
VIEWER: str = "scoped-actor-plan-viewer"


@dataclass(frozen=True)
class ActorHistory:
    scope: EventScope
    newest: tuple[str, ...]
    canary: str


async def seed_actor_history(database: Database) -> ActorHistory:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        canary = await seed_event(
            writer, scope, {"kind": "system", "component": CANARY}, 16
        )
        newest = await seed_event_metadata(writer, scope, CANDIDATE_CAP - 1)
        compressed_actor = await seed_event(
            writer, scope, USER, 16, payload=near_toast_payload()
        )
        unrelated = await seed_run(writer, "unrelated")
        await seed_event_metadata(writer, unrelated, 2500)
        await writer.exec_driver_sql("ANALYZE event")
        token = await Seeder(writer).token(sha256(VIEWER.encode()).hexdigest())
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == token)
            .values(
                approve=False,
                namespaces=[{"namespace_id": scope.namespace_id, "transcripts": False}],
            )
        )
    return ActorHistory(scope, tuple(reversed((*newest, compressed_actor))), canary)


async def seed_event_metadata(
    writer: AsyncConnection, scope: EventScope, count: int
) -> list[str]:
    seed = Seeder(writer)
    identifiers = [seed.new_id() for _ in range(count)]
    await writer.execute(
        insert(event),
        [
            {
                "id": identity,
                "namespace_id": scope.namespace_id,
                "run_id": scope.run_id,
                "node_instance_id": scope.node_id,
                "attempt_id": scope.attempt_id,
                "kind": "run.succeeded",
                "at": START,
                "actor": SYSTEM,
                "payload": {"private": "retained-only"},
                **seed.stamps(),
            }
            for identity in identifiers
        ],
    )
    return identifiers


def output_fields(node: JsonObject) -> tuple[str, ...]:
    output = node["Output"]
    assert isinstance(output, list)
    assert all(isinstance(field, str) for field in output)
    return tuple(str(field) for field in output)


def assert_bounded_postgresql_plan(
    nodes: list[JsonObject], expected_count: int, sort_backed: bool
) -> None:
    [raw] = [node for node in nodes if node.get("Subplan Name") == "CTE candidate"]
    [expanded] = [
        node for node in nodes if node.get("Subplan Name") == "CTE expanded_actor"
    ]
    [event_scan] = [
        node for node in plan_nodes(raw) if node.get("Relation Name") == "event"
    ]
    [expansion] = [
        field for field in output_fields(expanded) if "jsonb_set_lax(" in field
    ]

    assert raw["Node Type"] == "Limit"
    assert raw["Actual Rows"] == expanded["Actual Rows"] == expected_count
    assert raw["Actual Loops"] == expanded["Actual Loops"] == 1
    assert set(output_fields(raw)) == {
        "event.id",
        "event.at",
        "event.kind",
        "event.actor",
        "event.node_instance_id",
        "event.attempt_id",
    }
    assert "jsonb_set_lax(" not in str(raw)
    assert "pg_column_compression(" not in str(raw)
    assert "pg_column_size(" not in str(raw)
    assert expanded["Node Type"] == "CTE Scan"
    assert expanded["CTE Name"] == "candidate"
    assert expansion.count("jsonb_set_lax(") == 1
    assert "CASE WHEN" in expansion
    assert "pg_column_compression(candidate.actor)" in expansion
    assert "pg_column_size(candidate.actor)" in expansion
    assert "return_target" in expansion
    assert output_fields(nodes[0])
    assert all("payload" not in field for field in output_fields(nodes[0]))
    if sort_backed:
        [sort] = [node for node in plan_nodes(raw) if node["Node Type"] == "Sort"]
        assert event_scan["Node Type"] == "Seq Scan"
        assert event_scan in plan_nodes(sort)
        if expected_count == CANDIDATE_CAP:
            matching_rows = event_scan["Actual Rows"]
            assert type(matching_rows) is int
            assert matching_rows > CANDIDATE_CAP
    else:
        assert event_scan["Node Type"] == "Index Scan"
        assert event_scan["Index Name"] == "ix_event_run_id_id"
        assert "run_id" in str(event_scan["Index Cond"])
        assert "namespace_id" in str(event_scan["Filter"])
        assert all(node["Node Type"] != "Sort" for node in plan_nodes(raw))


async def assert_plain_actor_materialization(
    reader: AsyncConnection, query: str, arguments: tuple[object, ...], actor_id: str
) -> None:
    prefix, separator, _ = query.partition("SELECT selected.id,")
    assert separator
    witness = """
        SELECT pg_catalog.pg_typeof(expanded.actor)::text,
               pg_catalog.pg_column_compression(expanded.actor),
               pg_catalog.pg_column_size(expanded.actor),
               pg_catalog.pg_column_compression(saved.actor),
               expanded.actor = saved.actor
        FROM expanded_actor AS expanded
        JOIN event AS saved ON saved.id = expanded.id
        WHERE expanded.id =
        """
    result = await reader.exec_driver_sql(
        prefix + witness + f"${len(arguments) + 1}::varchar", (*arguments, actor_id)
    )
    materialized: tuple[object, ...] = tuple(result.one())
    datatype, expanded_codec, raw_size, saved_codec, exact = materialized

    assert datatype == "jsonb"
    assert expanded_codec is None
    assert saved_codec == "pglz"
    assert type(raw_size) is int
    assert 512 < raw_size <= 576
    assert exact is True


async def assert_postgresql_actor_plan(
    database: Database, history: ActorHistory, *, sort_backed: bool
) -> None:
    scope = history.scope
    async with database.read_snapshot() as reader:
        await reader.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        if sort_backed:
            await reader.exec_driver_sql("SET LOCAL enable_indexscan = off")
            await reader.exec_driver_sql("SET LOCAL enable_indexonlyscan = off")
        else:
            await reader.exec_driver_sql("SET LOCAL enable_seqscan = off")
        for continued in (False, True):
            ceiling = history.newest[0] if continued else None
            after = history.newest[DEFAULT_LIMIT - 1] if continued else None

            with executed_queries(reader) as statements:
                rows = await read_run_event_page(
                    reader,
                    scope.namespace_id,
                    scope.run_id,
                    ceiling,
                    after,
                    DEFAULT_LIMIT,
                )
            [(query, arguments)] = statements
            explained = await reader.exec_driver_sql(
                "EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + query, arguments
            )
            nodes = plan_nodes(as_json_value(explained.scalar_one()))

            assert query.count("jsonb_set_lax(") == 1
            assert "payload" not in query
            assert_bounded_postgresql_plan(
                nodes, 2 if continued else CANDIDATE_CAP, sort_backed
            )
            if continued:
                assert [row.id for row in rows] == [history.newest[-1], history.canary]
                assert rows[0].actor is not None
                assert rows[1].actor is None
            else:
                assert tuple(row.id for row in rows) == history.newest
                await assert_plain_actor_materialization(
                    reader, query, arguments, history.newest[0]
                )


@pytest.mark.asyncio
async def test_run_event_actor_plan_limits_native_expansion_and_later_page_failure(
    database: Database,
    api_capture: Capture,
) -> None:
    history = await seed_actor_history(database)
    app, path = observed_events(database, api_capture, history.scope.run_id)
    ledger = ConnectionLedger(database)
    headers = {"Authorization": "Bearer " + VIEWER}

    if database.dialect is Dialect.POSTGRESQL:
        await assert_postgresql_actor_plan(database, history, sort_backed=False)
        await assert_postgresql_actor_plan(database, history, sort_backed=True)
    async with client_for(app) as client:
        first = await client.get(path, headers=headers)
        page = RunEventPage.model_validate_json(first.content)
        assert page.next_cursor is not None
        continued = await client.get(
            path, params={"cursor": page.next_cursor}, headers=headers
        )
        subsequent = await client.get(path, headers=headers)

    assert first.status_code == subsequent.status_code == 200
    assert [item.id for item in page.items] == [
        f"event_{identity}" for identity in history.newest[:DEFAULT_LIMIT]
    ]
    assert page.items[0].actor.model_dump() == USER
    assert continued.status_code == 500
    error = ApiErrorEnvelope.model_validate_json(continued.content).error
    assert error.code is ApiErrorCode.INTERNAL
    assert error.message == "Internal operation failure."
    assert error.retryable is False
    assert isinstance(error.details, EmptyDetails)
    assert CANARY not in continued.text
    assert ledger.outstanding == 0
