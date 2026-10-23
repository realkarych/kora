import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alembic import command
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    Text,
    insert,
    select,
    table,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnClause

from kora.db.identifiers import IdentifierGenerator
from kora.db.migrations.environment import alembic_config
from kora.db.tables import (
    api_token,
    attempt,
    event,
    graph,
    graph_version,
    namespace,
    node_instance,
    run,
    usage_record,
    worker,
)
from kora.db.types import TIMESTAMP_TEXT_FORMAT, JsonValue
from tests.support.warning_filters import expression_index_warnings_ignored

START: datetime = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
DIGEST: str = "0" * 64


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.moment = start

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


def fixed_randomness(value: int) -> Callable[[int], bytes]:
    def randomness(count: int) -> bytes:
        return value.to_bytes(count, "big")

    return randomness


SHARED_CLOCK: FakeClock = FakeClock()
SHARED_GENERATOR: IdentifierGenerator = IdentifierGenerator(
    SHARED_CLOCK.now, fixed_randomness(1)
)


@dataclass
class Seeder:
    connection: AsyncConnection
    clock: FakeClock = field(default_factory=lambda: SHARED_CLOCK)
    generator: IdentifierGenerator = field(default_factory=lambda: SHARED_GENERATOR)

    @classmethod
    def at(cls, connection: AsyncConnection, moment: datetime) -> "Seeder":
        clock = FakeClock(moment)
        return cls(
            connection, clock, IdentifierGenerator(clock.now, fixed_randomness(1))
        )

    def new_id(self) -> str:
        return self.generator.new()

    def stamps(self) -> dict[str, datetime]:
        now = self.clock.now()
        return {"created_at": now, "updated_at": now}

    async def namespace(self, name: str = "default") -> str:
        namespace_id = self.new_id()
        await self.connection.execute(
            insert(namespace).values(
                id=namespace_id,
                name=name,
                policy_revision=1,
                policy={"max_pending_runs": 1000},
                **self.stamps(),
            )
        )
        return namespace_id

    async def graph(self, namespace_id: str, name: str = "pipeline") -> str:
        graph_id = self.new_id()
        await self.connection.execute(
            insert(graph).values(
                id=graph_id, namespace_id=namespace_id, name=name, **self.stamps()
            )
        )
        return graph_id

    async def graph_version(
        self, graph_id: str, number: int = 1, ir: JsonValue | None = None
    ) -> str:
        version_id = self.new_id()
        await self.connection.execute(
            insert(graph_version).values(
                id=version_id,
                graph_id=graph_id,
                number=number,
                ir={"nodes": []} if ir is None else ir,
                ir_digest=DIGEST,
                source_digest=DIGEST,
                execution_contract={},
                execution_contract_digest=DIGEST,
                deployment_digest=DIGEST,
                resolved_digests={},
                resources={},
                deployed_by="owner",
                deployed_at=self.clock.now(),
                status="active",
                **self.stamps(),
            )
        )
        return version_id

    def run_values(
        self,
        namespace_id: str,
        graph_id: str,
        version_id: str,
        status: str = "running",
        needs_scheduling: bool = True,
        target_key: str | None = None,
        target_held_since: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        run_id = self.new_id()
        return {
            "id": run_id,
            "namespace_id": namespace_id,
            "graph_id": graph_id,
            "graph_version_id": version_id,
            "rerun_of_run_id": None,
            "idempotency_key": run_id if idempotency_key is None else idempotency_key,
            "target_key": target_key,
            "target_held_since": target_held_since,
            "params": {},
            "cel_params_projection": {},
            "allow_unenforced_secrets": False,
            "status": status,
            "needs_scheduling": needs_scheduling,
            "trigger": {"kind": "manual"},
            "limits": {},
            "retention": {},
            "started_at": None,
            "deadline_expired_at": None,
            "finished_at": None,
            **self.stamps(),
        }

    async def run(
        self,
        namespace_id: str,
        graph_id: str,
        version_id: str,
        status: str = "running",
        needs_scheduling: bool = True,
        target_key: str | None = None,
        target_held_since: datetime | None = None,
    ) -> str:
        values = self.run_values(
            namespace_id,
            graph_id,
            version_id,
            status,
            needs_scheduling,
            target_key,
            target_held_since,
        )
        await self.connection.execute(insert(run).values(**values))
        return str(values["id"])

    async def runs(
        self,
        namespace_id: str,
        graph_id: str,
        version_id: str,
        count: int,
        needs_scheduling: bool = True,
    ) -> list[str]:
        rows = [
            self.run_values(
                namespace_id, graph_id, version_id, needs_scheduling=needs_scheduling
            )
            for _ in range(count)
        ]
        await self.connection.execute(insert(run), rows)
        return [str(row["id"]) for row in rows]

    def node_values(
        self, run_id: str, node_path: str, status: str, top_level: bool
    ) -> dict[str, object]:
        return {
            "id": self.new_id(),
            "run_id": run_id,
            "node_path": node_path,
            "kind": "command",
            "status": status,
            "trigger_rule": "all_success",
            "subgraph_attempt": 1,
            "top_level": top_level,
            "consumed_versions": {},
            "current_attempt_id": None,
            "branch_selection": None,
            "attempt_count": 0,
            "started_at": None,
            "deadline_expired_at": None,
            "provider_wait_deadline": None,
            **self.stamps(),
        }

    async def node(
        self,
        run_id: str,
        node_path: str,
        status: str = "pending",
        top_level: bool = True,
    ) -> str:
        values = self.node_values(run_id, node_path, status, top_level)
        await self.connection.execute(insert(node_instance).values(**values))
        return str(values["id"])

    async def nodes(
        self, run_id: str, paths: list[str], status: str, top_level: bool
    ) -> list[str]:
        rows = [self.node_values(run_id, path, status, top_level) for path in paths]
        await self.connection.execute(insert(node_instance), rows)
        return [str(row["id"]) for row in rows]

    async def attempt(
        self, node_instance_id: str, status: str = "running", number: int = 1
    ) -> str:
        attempt_id = self.new_id()
        await self.connection.execute(
            insert(attempt).values(
                id=attempt_id,
                node_instance_id=node_instance_id,
                number=number,
                runtime="local",
                worker_id=None,
                execution_domain_id=None,
                execution_state="unknown",
                recovery_version=0,
                recovery_required=False,
                recovery=None,
                handle=None,
                status=status,
                desired_state="run",
                record_version=1,
                input_bindings={},
                execution_request=None,
                execution_policy=None,
                error_class=None,
                error=None,
                strategy=None,
                strategy_fallback=None,
                infrastructure_retries=0,
                lease_owner=None,
                lease_epoch=0,
                lease_until=None,
                last_heartbeat_at=None,
                counters={},
                scratch={},
                resolved_config_digest=None,
                price_table_id=None,
                image_digest=None,
                harness_version=None,
                started_at=None,
                deadline=None,
                deadline_expired_at=None,
                finished_at=None,
                **self.stamps(),
            )
        )
        return attempt_id

    async def usage(self, attempt_id: str, model: str, usd_estimate: Decimal) -> str:
        usage_id = self.new_id()
        await self.connection.execute(
            insert(usage_record).values(
                id=usage_id,
                attempt_id=attempt_id,
                model=model,
                input_tokens=1,
                cached_input_tokens=0,
                cache_creation_tokens=0,
                output_tokens=1,
                reasoning_tokens=0,
                completeness="complete",
                basis="price_table",
                billing_class="api_key",
                usd_estimate=usd_estimate,
                delta_of_session_totals=False,
                source={},
                worker_id=None,
                image_digest=None,
                **self.stamps(),
            )
        )
        return usage_id

    async def events(
        self, namespace_id: str, run_id: str | None, count: int, kind: str = "note"
    ) -> list[str]:
        rows = [
            {
                "id": self.new_id(),
                "namespace_id": namespace_id,
                "run_id": run_id,
                "node_instance_id": None,
                "attempt_id": None,
                "kind": kind,
                "actor": {"kind": "system"},
                "payload": {"index": index},
                "at": self.clock.now(),
                **self.stamps(),
            }
            for index in range(count)
        ]
        await self.connection.execute(insert(event), rows)
        return [str(row["id"]) for row in rows]

    async def token(
        self,
        digest: str,
        kind: str = "user",
        owner: str = "person",
        expires_at: datetime | None = None,
    ) -> str:
        token_id = self.new_id()
        await self.connection.execute(
            insert(api_token).values(
                id=token_id,
                kind=kind,
                digest=digest,
                owner=owner,
                namespaces=[],
                approve=True,
                platform=False,
                runtime=None,
                expires_at=expires_at,
                **self.stamps(),
            )
        )
        return token_id

    async def worker(self, token_id: str, worker_id: str = "worker-1") -> str:
        await self.connection.execute(
            insert(worker).values(
                id=worker_id,
                incarnation_id=self.new_id(),
                execution_domain_id=self.new_id(),
                runtime="local",
                version="0.1.0.dev0",
                capacity=2,
                last_seen_at=self.clock.now(),
                token_id=token_id,
                **self.stamps(),
            )
        )
        return worker_id


@dataclass(frozen=True)
class PredecessorHistory:
    columns: dict[str, tuple[str, ...]]
    rows: dict[str, tuple[dict[str, object], ...]]


def predecessor_value(column: Column[object]) -> object:
    if column.nullable:
        return None
    if column.foreign_keys:
        target = next(iter(column.foreign_keys)).column.table.name
        return f"old-{target}"
    if isinstance(column.type, Boolean):
        return False
    if isinstance(column.type, Integer):
        return 1
    if isinstance(column.type, DateTime):
        return START
    if isinstance(column.type, JSON):
        return {}
    if isinstance(column.type, Text):
        return "{}"
    if column.name in {
        "created_at",
        "updated_at",
        "effective_from",
        "at",
        "granted_at",
        "uploaded_at",
        "deployed_at",
        "last_seen_at",
    }:
        return START.strftime(TIMESTAMP_TEXT_FORMAT)
    return "predecessor"


def predecessor_snapshot(
    connection: Connection, columns: dict[str, tuple[str, ...]]
) -> PredecessorHistory:
    rows: dict[str, tuple[dict[str, object], ...]] = {}
    for table_name, names in columns.items():
        source = table(table_name, *(ColumnClause[object](name) for name in names))
        rows[table_name] = tuple(
            dict(row)
            for row in connection.execute(select(source).order_by(*source.c)).mappings()
        )
    return PredecessorHistory(columns, rows)


def seed_predecessor(connection: Connection) -> PredecessorHistory:
    command.upgrade(alembic_config(connection), "0001")
    frozen = MetaData()
    with expression_index_warnings_ignored():
        frozen.reflect(connection)
    rows: tuple[tuple[str, dict[str, object]], ...] = (
        ("namespace", {"id": "old-namespace", "name": "old"}),
        ("namespace", {"id": "other-namespace", "name": "other"}),
        ("graph", {"id": "old-graph"}),
        ("graph_version", {"id": "old-graph_version"}),
        ("run", {"id": "old-run", "status": "running"}),
        ("node_instance", {"id": "old-node_instance", "status": "running"}),
        ("api_token", {"id": "old-api_token"}),
        ("worker", {"id": "old-worker"}),
        (
            "attempt",
            {"id": "old-attempt", "price_table_id": "old-price", "status": "running"},
        ),
        (
            "attempt",
            {
                "id": "old-terminal",
                "number": 2,
                "price_table_id": "missing-price",
                "status": "failed",
            },
        ),
        (
            "attempt",
            {
                "id": "old-unpriced",
                "number": 3,
                "price_table_id": None,
                "status": "succeeded",
            },
        ),
        ("attempt_dispatcher", {"id": "old-dispatcher", "mode": "execute"}),
        ("wait", {"id": "old-wait", "attempt_id": "old-attempt"}),
        (
            "approval_request",
            {
                "id": "old-approval",
                "run_cost": {"version": 1, "arbitrary": "historical"},
            },
        ),
        (
            "approval_request",
            {"id": "old-other-approval", "run_cost": ["arbitrary", -4]},
        ),
        (
            "price_table",
            {
                "id": "old-price",
                "model": "model-a",
                "prices": {"legacy": "0.10000000000"},
            },
        ),
        (
            "price_table",
            {"id": "old-duplicate", "model": "model-b", "prices": {"legacy": 8}},
        ),
        (
            "price_table",
            {
                "id": "old-scoped",
                "namespace_id": "old-namespace",
                "model": "model-c",
                "prices": {"legacy": None},
            },
        ),
        (
            "price_table",
            {
                "id": "old-other-scoped",
                "namespace_id": "other-namespace",
                "model": "model-d",
                "prices": {},
            },
        ),
        (
            "price_table",
            {
                "id": "old-later",
                "model": "model-a",
                "effective_from": START + timedelta(days=1),
                "prices": {"changed": True},
            },
        ),
        (
            "usage_record",
            {
                "id": "old-zero",
                "model": "model-a",
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "usd_estimate": "0.0000000000",
                "source": {"old": [0, "source"]},
            },
        ),
        (
            "usage_record",
            {
                "id": "old-out-of-domain",
                "attempt_id": "old-terminal",
                "model": "model-b",
                "input_tokens": -1,
                "output_tokens": 0,
                "reasoning_tokens": 7,
                "usd_estimate": "-1.2345678901",
                "source": {"vendor": "untouched"},
            },
        ),
        ("artifact_version", {"id": "old-artifact", "attempt_id": "old-attempt"}),
        (
            "artifact_version",
            {"id": "old-artifact-child", "attempt_id": "old-terminal"},
        ),
        (
            "artifact_lineage",
            {
                "artifact_version_id": "old-artifact-child",
                "consumed_artifact_version_id": "old-artifact",
            },
        ),
        ("session_segment", {"attempt_id": "old-attempt", "offset": 0}),
        (
            "event",
            {
                "id": "old-event",
                "run_id": "old-run",
                "node_instance_id": "old-node_instance",
                "attempt_id": "old-attempt",
            },
        ),
    )
    for table_name, overrides in rows:
        table = frozen.tables[table_name]
        values = {column.name: predecessor_value(column) for column in table.columns}
        values.update(overrides)
        for key, value in values.items():
            if connection.dialect.name == "sqlite":
                if isinstance(value, dict | list):
                    values[key] = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, datetime):
                    values[key] = value.strftime(TIMESTAMP_TEXT_FORMAT)
            elif key == "usd_estimate":
                values[key] = Decimal(str(value))
        connection.execute(insert(table).values(**values))
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql(
            "UPDATE usage_record SET input_tokens = 2147483648, usd_estimate = "
            "'0.1234567890123456789012345' WHERE id = 'old-out-of-domain'"
        )
    columns = {
        name: tuple(column.name for column in frozen.tables[name].columns)
        for name in {name for name, _ in rows}
    }
    return predecessor_snapshot(connection, columns)
