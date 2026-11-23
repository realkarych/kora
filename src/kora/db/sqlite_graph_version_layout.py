from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.transactions import WriterTransactionError, writer_transaction
from kora.db.types import SQLITE

PAGE_SIZE = text("PRAGMA main.page_size")
ENCODING = text("PRAGMA main.encoding")


@dataclass(frozen=True, slots=True)
class SQLiteGraphVersionLayout:
    page_size: int
    encoding: str


async def read_sqlite_graph_version_layout(
    connection: AsyncConnection,
) -> SQLiteGraphVersionLayout | None:
    try:
        writer = writer_transaction(connection)
        if connection.dialect.name != SQLITE:
            return None
        page_size: object = (await connection.execute(PAGE_SIZE)).scalar_one()
        encoding: object = (await connection.execute(ENCODING)).scalar_one()
        writer.require_active()
    except (WriterTransactionError, SQLAlchemyError):
        return None
    if (
        type(page_size) is not int
        or not 512 <= page_size <= 65_536
        or page_size & (page_size - 1)
        or type(encoding) is not str
        or encoding not in ("UTF-8", "UTF-16le", "UTF-16be")
    ):
        return None
    return SQLiteGraphVersionLayout(page_size, encoding.lower())
