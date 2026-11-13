from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Text, and_, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.tables import (
    graph,
    graph_version,
    inbound_event,
    node_instance,
    run,
    run_inputs,
    run_promotion_admission,
)
from kora.db.types import SQLITE
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.run_materialization import PreparedRunMaterialization
from kora.engine.start_capacity import Overlap
from kora.engine.start_records import (
    MAX_RECEIPT_BYTES,
    AcceptedStart,
    decode_start_receipt,
)
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier

INPUT_SENTINEL = "kora.run-inputs.v1"


@dataclass(frozen=True)
class PromotionReadLimits:
    parent_bytes: int
    receipt_row_bytes: int

    def __post_init__(self) -> None:
        if min(self.parent_bytes, self.receipt_row_bytes) <= 0:
            raise ValueError("promotion read limits must be positive")


class ManualPromotionHeader(BoundaryModel):
    kind: Literal["manual"]
    overlap: Overlap
    occurrence_key: Identifier


def _bounded_json(value: ColumnElement[object], limit: int) -> ColumnElement[object]:
    return case((func.octet_length(cast(value, Text())) <= limit, value))


def sqlite_record_width(columns: list[ColumnElement[object]]) -> ColumnElement[int]:
    return sum(
        (
            case(
                (func.typeof(column) == "null", 0),
                (func.typeof(column).in_(("integer", "real")), 8),
                else_=func.octet_length(column),
            )
            for column in columns
        ),
        literal(9 * (len(columns) + 1)),
    )


async def admitted_promotion_receipt(
    connection: AsyncConnection,
    scope: GraphResourceScope,
    run_id: str,
    limits: PromotionReadLimits,
) -> str | None:
    fields = run_promotion_admission.c
    statement = select(fields.receipt_id).where(
        fields.run_id == run_id,
        fields.namespace_id == scope.namespace_id,
        fields.graph_id == scope.graph_id,
        fields.graph_version_id == scope.version_id,
        fields.version == 1,
        fields.run_bytes > 0,
        fields.run_bytes <= limits.parent_bytes,
        fields.receipt_bytes > 0,
        fields.receipt_bytes <= limits.receipt_row_bytes,
    )
    value: object = await connection.scalar(statement)
    return value if type(value) is str else None


async def read_promotion_status(
    connection: AsyncConnection,
    scope: GraphResourceScope,
    pin: GraphVersionPin,
    run_id: str,
    limits: PromotionReadLimits,
) -> str | None:
    if connection.dialect.name == SQLITE:
        fits = await connection.scalar(
            select(sqlite_record_width(list(run.c)) <= limits.parent_bytes).where(
                run.c.id == run_id
            )
        )
        if fits is not True:
            return None
    statement = (
        select(run.c.status)
        .join(
            graph,
            and_(
                graph.c.id == run.c.graph_id, graph.c.namespace_id == run.c.namespace_id
            ),
        )
        .join(
            graph_version,
            and_(
                graph_version.c.id == run.c.graph_version_id,
                graph_version.c.graph_id == graph.c.id,
            ),
        )
        .where(
            run.c.id == run_id,
            run.c.namespace_id == scope.namespace_id,
            run.c.graph_id == scope.graph_id,
            run.c.graph_version_id == scope.version_id,
            graph_version.c.execution_contract_digest == pin.execution_contract_digest,
        )
    )
    value: object = await connection.scalar(statement)
    return value if type(value) is str else None


async def read_saved_manual_promotion_header(
    connection: AsyncConnection,
    scope: GraphResourceScope,
    pin: GraphVersionPin,
    materialization: PreparedRunMaterialization,
    run_id: str,
    limits: PromotionReadLimits,
    admitted_receipt_id: str,
) -> ManualPromotionHeader | None:
    has_inputs = (
        select(run_inputs.c.run_id).where(run_inputs.c.run_id == run_id).exists()
    )
    has_nodes = (
        select(node_instance.c.id).where(node_instance.c.run_id == run_id).exists()
    )
    timeout_limit = 32 + (
        0 if materialization.timeout is None else 6 * len(materialization.timeout)
    )
    statement = (
        select(
            _bounded_json(run.c.trigger, MAX_RECEIPT_BYTES),
            _bounded_json(run.c.limits, timeout_limit),
        )
        .select_from(
            run.join(
                graph,
                and_(
                    graph.c.id == run.c.graph_id,
                    graph.c.namespace_id == run.c.namespace_id,
                ),
            ).join(
                graph_version,
                and_(
                    graph_version.c.id == run.c.graph_version_id,
                    graph_version.c.graph_id == graph.c.id,
                ),
            )
        )
        .where(
            run.c.id == run_id,
            run.c.namespace_id == scope.namespace_id,
            run.c.graph_id == scope.graph_id,
            run.c.graph_version_id == scope.version_id,
            graph_version.c.execution_contract_digest == pin.execution_contract_digest,
            run.c.status == "pending",
            run.c.started_at.is_(None),
            run.c.deadline_expired_at.is_(None),
            run.c.finished_at.is_(None),
            run.c.target_held_since.is_(None),
            run.c.rerun_of_run_id.is_(None),
            run.c.input_encoding_version == 1,
            run.c.params == INPUT_SENTINEL,
            run.c.cel_params_projection == INPUT_SENTINEL,
            has_inputs,
            ~has_nodes,
        )
    )
    with await connection.execute(statement) as result:
        row = result.one_or_none()
    if row is None:
        return None
    try:
        header = ManualPromotionHeader.model_validate(row[0])
    except ValueError:
        return None
    expected_limits = (
        {} if materialization.timeout is None else {"timeout": materialization.timeout}
    )
    if (
        row[1] != expected_limits
        or header.overlap != materialization.manual_overlap
        or header.occurrence_key != admitted_receipt_id
    ):
        return None
    if connection.dialect.name == SQLITE:
        fits = await connection.scalar(
            select(
                sqlite_record_width(list(inbound_event.c)) <= limits.receipt_row_bytes
            ).where(inbound_event.c.id == header.occurrence_key)
        )
        if fits is not True:
            return None
    statement = (
        select(_bounded_json(inbound_event.c.start_outcome, MAX_RECEIPT_BYTES))
        .select_from(
            inbound_event.join(
                run, inbound_event.c.idempotency_key == run.c.idempotency_key
            )
        )
        .where(
            run.c.id == run_id,
            inbound_event.c.id == header.occurrence_key,
            inbound_event.c.namespace_id == scope.namespace_id,
            inbound_event.c.name == f"kora.start:{scope.graph_id}",
        )
    )
    value = await connection.scalar(statement)
    try:
        receipt = decode_start_receipt(value)
    except ValueError:
        return None
    response = receipt.response
    if (
        receipt.namespace_id != scope.namespace_id
        or receipt.graph_id != scope.graph_id
        or not isinstance(response, AcceptedStart)
        or response.run_id != run_id
        or response.graph_version_id != scope.version_id
    ):
        return None
    return header
