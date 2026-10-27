import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from pydantic import ValidationError
from sqlalchemy import (
    Executable,
    FromClause,
    Join,
    LargeBinary,
    Text,
    case,
    func,
    select,
    text,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.tables import (
    approval_request,
    attempt,
    node_instance,
    price_table,
    run,
    usage_record,
)
from kora.db.types import JsonValue, canonical_json, parse_json
from kora.ir.cost import PriceRevision, RunCost, UsageCoverage, UsageRow, format_money
from kora.ir.json import JsonShapeError, parse_json_object

MAX_PAGE_ROWS: int = 128
JSON_TRANSPORT_MULTIPLIER: int = 6
JSON_TRANSPORT_OVERHEAD: int = 1024
SOURCE_BYTE_LIMIT: int = 8192
COVERAGE_BYTE_LIMIT: int = 8192


class CostReadRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("stored accounting value is unavailable or invalid")


class HistoricalState(enum.StrEnum):
    ABSENT = "absent"
    UNMATCHED = "unmatched"
    PREDECESSOR = "predecessor"
    VERSION_1 = "version_1"


@dataclass(frozen=True)
class PriceRead:
    state: HistoricalState
    reference: str | None
    revision: PriceRevision | None = None
    predecessor: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ApprovalCostRead:
    state: HistoricalState
    snapshot: RunCost | None = None
    predecessor: JsonValue = None


@dataclass(frozen=True)
class UsageRead:
    id: str
    attempt_id: str
    state: HistoricalState
    usd_estimate: Decimal | None
    fields: Mapping[str, object]


@dataclass(frozen=True)
class AttemptCostRead:
    id: str
    price_table_id: str | None
    status: str
    coverage: UsageCoverage | None


def byte_size(
    connection: AsyncConnection, value: ColumnElement[object]
) -> ColumnElement[int]:
    if connection.dialect.name == "postgresql":
        return func.octet_length(sql_cast(value, Text))
    return func.length(sql_cast(value, LargeBinary))


def bounded_text(
    connection: AsyncConnection, value: ColumnElement[object], maximum: int
) -> ColumnElement[str]:
    return case(
        (byte_size(connection, value) <= maximum, sql_cast(value, Text)), else_=None
    ).label(str(value.key))


def payload_projection(
    connection: AsyncConnection, value: ColumnElement[object], maximum: int
) -> tuple[ColumnElement[str], ColumnElement[int]]:
    allowance = maximum * JSON_TRANSPORT_MULTIPLIER + JSON_TRANSPORT_OVERHEAD
    return bounded_text(connection, value, allowance), byte_size(
        connection, value
    ).label(f"{value.key}_bytes")


def scalar_projection(
    connection: AsyncConnection, value: ColumnElement[object], maximum: int = 1024
) -> tuple[ColumnElement[object], ColumnElement[int]]:
    return case((byte_size(connection, value) <= maximum, value), else_=None).label(
        str(value.key)
    ), byte_size(connection, value).label(f"{value.key}_bytes")


def admit_projection(row: RowMapping) -> None:
    for key in row:
        if (
            key.endswith("_bytes")
            and row[key] is not None
            and row[key.removesuffix("_bytes")] is None
        ):
            raise CostReadRefused()


def stored_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(required_text(value))
    except ValueError as error:
        raise CostReadRefused() from error


def payload(row: RowMapping, key: str, maximum: int) -> JsonValue:
    raw = row[key]
    if row[f"{key}_bytes"] is None:
        return None
    if not isinstance(raw, str):
        raise CostReadRefused()
    try:
        decoded = parse_json(raw)
        if len(canonical_json(decoded).encode("utf-8")) > maximum:
            raise CostReadRefused()
        return decoded
    except (ValueError, RecursionError, UnicodeError) as error:
        raise CostReadRefused() from error


def required_text(value: object) -> str:
    if not isinstance(value, str):
        raise CostReadRefused()
    return value


def valid_price(row: RowMapping, prices: JsonValue) -> PriceRevision:
    if row["model"] is not None:
        raise CostReadRefused()
    try:
        return PriceRevision.model_validate(
            {
                "id": row["id"],
                "namespace_id": row["namespace_id"],
                "effective_from": row["effective_from"],
                "version": row["version"],
                "normalization_version": row["normalization_version"],
                "prices": prices,
            }
        )
    except ValidationError as error:
        raise CostReadRefused() from error


async def first_mapping(
    connection: AsyncConnection, statement: Executable
) -> RowMapping | None:
    return (await connection.execute(statement)).mappings().first()


def attempt_run_join(source: FromClause) -> Join:
    return source.join(
        node_instance, attempt.c.node_instance_id == node_instance.c.id
    ).join(run)


def price_projection(
    connection: AsyncConnection,
) -> Sequence[ColumnElement[object] | ColumnElement[str] | ColumnElement[int]]:
    return [
        *scalar_projection(connection, price_table.c.id),
        *scalar_projection(connection, price_table.c.namespace_id),
        *payload_projection(connection, price_table.c.effective_from, 128),
        *scalar_projection(connection, price_table.c.model),
        price_table.c.version,
        price_table.c.normalization_version,
        *payload_projection(connection, price_table.c.prices, PriceRevision.byte_limit),
    ]


async def read_latest_price(
    connection: AsyncConnection, namespace_id: str | None, at: datetime
) -> PriceRevision | None:
    row = await first_mapping(
        connection,
        select(*price_projection(connection))
        .where(
            price_table.c.namespace_id == namespace_id,
            text("price_table.version = 1"),
            price_table.c.effective_from <= at,
        )
        .order_by(price_table.c.effective_from.desc())
        .limit(1),
    )
    if row is None:
        return None
    admit_projection(row)
    return valid_price(row, payload(row, "prices", PriceRevision.byte_limit))


async def read_price(
    connection: AsyncConnection, namespace_id: str | None, reference: str | None
) -> PriceRead:
    if reference is None:
        return PriceRead(HistoricalState.ABSENT, None)
    row = await first_mapping(
        connection,
        select(*price_projection(connection))
        .where(
            price_table.c.id == reference,
            price_table.c.namespace_id == namespace_id,
        )
        .limit(1),
    )
    if row is None:
        return PriceRead(HistoricalState.UNMATCHED, reference)
    admit_projection(row)
    prices = payload(row, "prices", PriceRevision.byte_limit)
    if row["version"] is None and row["normalization_version"] is None:
        model = required_text(row["model"])
        return PriceRead(
            HistoricalState.PREDECESSOR,
            reference,
            predecessor=MappingProxyType(
                {
                    "id": row["id"],
                    "namespace_id": row["namespace_id"],
                    "effective_from": stored_timestamp(row["effective_from"]),
                    "model": model,
                    "prices": prices,
                }
            ),
        )
    return PriceRead(HistoricalState.VERSION_1, reference, valid_price(row, prices))


async def read_attempt_price(
    connection: AsyncConnection, namespace_id: str, attempt_id: str
) -> PriceRead:
    row = await first_mapping(
        connection,
        select(
            *scalar_projection(connection, attempt.c.price_table_id),
            *scalar_projection(connection, price_table.c.namespace_id),
        )
        .select_from(
            attempt_run_join(attempt).outerjoin(
                price_table, attempt.c.price_table_id == price_table.c.id
            )
        )
        .where(run.c.namespace_id == namespace_id, attempt.c.id == attempt_id)
        .limit(1),
    )
    if row is None:
        return PriceRead(HistoricalState.ABSENT, None)
    admit_projection(row)
    reference = cast(str | None, row["price_table_id"])
    scope = cast(str | None, row["namespace_id"])
    if scope not in (None, namespace_id):
        return PriceRead(HistoricalState.UNMATCHED, reference)
    return await read_price(connection, scope, reference)


async def read_approval_cost(
    connection: AsyncConnection, namespace_id: str, request_id: str
) -> ApprovalCostRead:
    row = await first_mapping(
        connection,
        select(
            approval_request.c.run_cost_version,
            *payload_projection(
                connection, approval_request.c.run_cost, RunCost.byte_limit
            ),
        )
        .select_from(approval_request.join(node_instance).join(run))
        .where(
            run.c.namespace_id == namespace_id,
            approval_request.c.id == request_id,
        )
        .limit(1),
    )
    if row is None:
        return ApprovalCostRead(HistoricalState.ABSENT)
    document = payload(row, "run_cost", RunCost.byte_limit)
    if row["run_cost_version"] is None:
        return ApprovalCostRead(HistoricalState.PREDECESSOR, predecessor=document)
    if row["run_cost_version"] != 1:
        raise CostReadRefused()
    try:
        return ApprovalCostRead(
            HistoricalState.VERSION_1, RunCost.model_validate(document)
        )
    except ValidationError as error:
        raise CostReadRefused() from error


def page_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS:
        raise CostReadRefused()
    return limit


USAGE_SCALARS: tuple[str, ...] = (
    "id",
    "attempt_id",
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
    "delta_of_session_totals",
    "normalization_version",
    "observation_sequence",
    "observed_at",
    "dispatcher_id",
)
USAGE_TEXT: tuple[str, ...] = (
    "model",
    "completeness",
    "basis",
    "billing_class",
    "observation_id",
)
USAGE_METADATA: tuple[str, ...] = (
    "reported_usd",
    "known_token_fields",
    "cache_creation_by_ttl",
    "observation_id",
    "observation_sequence",
    "observed_at",
    "dispatcher_id",
)


def usage_value(row: RowMapping) -> UsageRead:
    admit_projection(row)
    values: dict[str, object] = {
        name: row[name] for name in (*USAGE_SCALARS, *USAGE_TEXT)
    }
    values["observed_at"] = stored_timestamp(values["observed_at"])
    for name in USAGE_TEXT[:-1]:
        required_text(values[name])
    values["known_token_fields"] = payload(row, "known_token_fields", 512)
    values["cache_creation_by_ttl"] = payload(row, "cache_creation_by_ttl", 256)
    amount = row["usd_estimate"]
    reported = row["reported_usd"]
    if (
        row["usd_estimate_bytes"] is not None
        and amount is None
        or row["reported_usd_bytes"] is not None
        and reported is None
    ):
        raise CostReadRefused()
    try:
        estimate = None if amount is None else Decimal(required_text(amount))
        values["reported_usd"] = (
            None if reported is None else format_money(Decimal(required_text(reported)))
        )
        if row["normalization_version"] is None:
            if any(values[name] is not None for name in USAGE_METADATA):
                raise CostReadRefused()
            state = HistoricalState.PREDECESSOR
        elif row["normalization_version"] == 1:
            if estimate is not None:
                format_money(estimate)
            normalized = {
                key: value
                for key, value in values.items()
                if key in UsageRow.model_fields
            }
            normalized["source"] = {}
            UsageRow.model_validate(normalized)
            state = HistoricalState.VERSION_1
        else:
            raise CostReadRefused()
    except (ValueError, ArithmeticError) as error:
        raise CostReadRefused() from error
    return UsageRead(
        required_text(row["id"]),
        required_text(row["attempt_id"]),
        state,
        estimate,
        MappingProxyType(values),
    )


async def read_usage_page(
    connection: AsyncConnection,
    namespace_id: str,
    run_id: str,
    after_id: str | None = None,
    limit: int = 64,
) -> tuple[UsageRead, ...]:
    statement = (
        select(
            *(
                item
                for name in USAGE_SCALARS
                if name != "observed_at"
                for item in scalar_projection(connection, usage_record.c[name])
            ),
            *payload_projection(connection, usage_record.c.observed_at, 128),
            *(
                item
                for name in USAGE_TEXT
                for item in scalar_projection(connection, usage_record.c[name])
            ),
            *payload_projection(connection, usage_record.c.known_token_fields, 512),
            *payload_projection(connection, usage_record.c.cache_creation_by_ttl, 256),
            *payload_projection(connection, usage_record.c.usd_estimate, 128),
            *payload_projection(connection, usage_record.c.reported_usd, 128),
        )
        .select_from(attempt_run_join(usage_record.join(attempt)))
        .where(
            run.c.namespace_id == namespace_id,
            run.c.id == run_id,
        )
        .order_by(usage_record.c.id)
        .limit(page_limit(limit))
    )
    if after_id is not None:
        statement = statement.where(usage_record.c.id > after_id)
    rows = (await connection.execute(statement)).mappings().all()
    return tuple(usage_value(row) for row in rows)


async def read_usage_source(
    connection: AsyncConnection, namespace_id: str, usage_id: str
) -> JsonValue:
    row = await first_mapping(
        connection,
        select(
            usage_record.c.normalization_version,
            *payload_projection(connection, usage_record.c.source, SOURCE_BYTE_LIMIT),
        )
        .select_from(attempt_run_join(usage_record.join(attempt)))
        .where(run.c.namespace_id == namespace_id, usage_record.c.id == usage_id)
        .limit(1),
    )
    if row is None:
        raise CostReadRefused()
    document = payload(row, "source", SOURCE_BYTE_LIMIT)
    if row["normalization_version"] is None:
        return document
    if row["normalization_version"] != 1:
        raise CostReadRefused()
    try:
        return parse_json_object(document, max_bytes=SOURCE_BYTE_LIMIT)
    except JsonShapeError as error:
        raise CostReadRefused() from error


async def read_attempt_page(
    connection: AsyncConnection,
    namespace_id: str,
    run_id: str,
    after_id: str | None = None,
    limit: int = 64,
) -> tuple[AttemptCostRead, ...]:
    statement = (
        select(
            *scalar_projection(connection, attempt.c.id),
            *scalar_projection(connection, attempt.c.price_table_id),
            *scalar_projection(connection, attempt.c.status),
            *payload_projection(
                connection, attempt.c.usage_coverage, COVERAGE_BYTE_LIMIT
            ),
        )
        .select_from(attempt_run_join(attempt))
        .where(
            run.c.namespace_id == namespace_id,
            run.c.id == run_id,
        )
        .order_by(attempt.c.id)
        .limit(page_limit(limit))
    )
    if after_id is not None:
        statement = statement.where(attempt.c.id > after_id)
    rows = (await connection.execute(statement)).mappings().all()
    results: list[AttemptCostRead] = []
    for row in rows:
        admit_projection(row)
        document = payload(row, "usage_coverage", COVERAGE_BYTE_LIMIT)
        try:
            coverage = (
                None
                if row["usage_coverage_bytes"] is None
                else UsageCoverage.model_validate(document)
            )
        except ValidationError as error:
            raise CostReadRefused() from error
        results.append(
            AttemptCostRead(
                required_text(row["id"]),
                cast(str | None, row["price_table_id"]),
                required_text(row["status"]),
                coverage,
            )
        )
    return tuple(results)
