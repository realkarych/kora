from asyncio import CancelledError
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_publication_values import (
    DEPLOYED_BY_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
    PreparedGraphPublicationBinds,
    sqlite_graph_publication_record_measurement_statement,
)
from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.sqlite_graph_version_bounds import (
    SQLiteGraphVersionFootprint,
    SQLiteGraphVersionWidths,
    bound_sqlite_graph_version_record,
)
from kora.db.sqlite_graph_version_layout import read_sqlite_graph_version_layout
from kora.db.transactions import (
    WriterTransaction,
    WriterTransactionError,
    writer_transaction,
)
from kora.db.types import SQLITE
from kora.ir.text import utf8_length


class SQLiteGraphPublicationMeasurementUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SQLite graph publication measurement unavailable")


@dataclass(frozen=True, slots=True)
class SQLiteGraphPublicationMeasurement:
    footprint: SQLiteGraphVersionFootprint
    resource_transport_bytes: int


def _admit_metadata(resolved_digests_json: str, deployed_by: str, limit: int) -> None:
    if type(resolved_digests_json) is not str or type(deployed_by) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if (
        type(limit) is not int
        or limit <= 0
        or len(resolved_digests_json) + len(deployed_by) > limit
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if (
        not resolved_digests_json
        or not deployed_by
        or "\x00" in resolved_digests_json
        or "\x00" in deployed_by
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    resolved_bytes = utf8_length(resolved_digests_json)
    actor_bytes = utf8_length(deployed_by)
    if resolved_bytes is None or actor_bytes is None:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if resolved_bytes + actor_bytes > limit:
        raise GraphValueRejected(GraphValueFailure.BUDGET)


async def observe_sqlite_graph_publication(
    connection: AsyncConnection,
    binds: PreparedGraphPublicationBinds,
    resolved_digests_json: str,
    deployed_by: str,
    *,
    max_metadata_bytes: int,
) -> SQLiteGraphPublicationMeasurement:
    parameters: dict[str, str] | None = None
    lease: WriterTransaction | None = None
    sizes: tuple[int, ...] | None = None
    try:
        if type(binds) is not PreparedGraphPublicationBinds:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        _admit_metadata(resolved_digests_json, deployed_by, max_metadata_bytes)
        if connection.dialect.name != SQLITE:
            raise SQLiteGraphPublicationMeasurementUnavailable()
        lease = writer_transaction(connection)
        layout = await read_sqlite_graph_version_layout(connection)
        if layout is None:
            raise SQLiteGraphPublicationMeasurementUnavailable()
        parameters = binds.parameters() | {
            RESOLVED_DIGESTS_PARAMETER: resolved_digests_json,
            DEPLOYED_BY_PARAMETER: deployed_by,
        }
        try:
            result = await connection.execute(
                sqlite_graph_publication_record_measurement_statement(), parameters
            )
        except OverflowError:
            raise SQLiteGraphPublicationMeasurementUnavailable() from None
        try:
            sizes = tuple(result.one())
        finally:
            del result
        lease.require_active()
        if (
            len(sizes) != 6
            or any(type(size) is not int or size <= 0 for size in sizes)
            or sizes[2] != sizes[3]
        ):
            raise SQLiteGraphPublicationMeasurementUnavailable()
        ir, execution, resources, transport, resolved, actor = sizes
        footprint = bound_sqlite_graph_version_record(
            SQLiteGraphVersionWidths(ir, execution, resources, resolved, actor),
            encoding=layout.encoding,
            page_size=layout.page_size,
        )
        return SQLiteGraphPublicationMeasurement(footprint, transport)
    except GraphValueRejected as error:
        failure = GraphValueRejected(error.reason)
    except (
        SQLiteGraphPublicationMeasurementUnavailable,
        WriterTransactionError,
        SQLAlchemyError,
    ):
        failure = SQLiteGraphPublicationMeasurementUnavailable()
    except CancelledError as error:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        raise
    finally:
        del (
            connection,
            binds,
            resolved_digests_json,
            deployed_by,
            max_metadata_bytes,
            parameters,
            lease,
            sizes,
        )
    raise_safe(failure)
