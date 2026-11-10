from asyncio import CancelledError
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_publication_metadata import PreparedGraphPublicationMetadata
from kora.api.graph_publication_values import (
    PreparedGraphPublicationBinds,
    sqlite_graph_publication_record_measurement_statement,
)
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
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


class SQLiteGraphPublicationMeasurementUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SQLite graph publication measurement unavailable")


@dataclass(frozen=True, slots=True)
class SQLiteGraphPublicationMeasurement:
    footprint: SQLiteGraphVersionFootprint
    resource_transport_bytes: int


async def observe_sqlite_graph_publication(
    connection: AsyncConnection,
    binds: PreparedGraphPublicationBinds,
    metadata: PreparedGraphPublicationMetadata,
) -> SQLiteGraphPublicationMeasurement:
    parameters: dict[str, str] | None = None
    lease: WriterTransaction | None = None
    sizes: tuple[int, ...] | None = None
    try:
        if (
            type(binds) is not PreparedGraphPublicationBinds
            or type(metadata) is not PreparedGraphPublicationMetadata
            or metadata.dialect is not Dialect.SQLITE
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        if connection.dialect.name != SQLITE:
            raise SQLiteGraphPublicationMeasurementUnavailable()
        lease = writer_transaction(connection)
        layout = await read_sqlite_graph_version_layout(connection)
        if layout is None:
            raise SQLiteGraphPublicationMeasurementUnavailable()
        parameters = binds.parameters() | metadata.parameters()
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
            metadata,
            parameters,
            lease,
            sizes,
        )
    raise_safe(failure)
