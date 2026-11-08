from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.transactions import WriterTransactionError, writer_transaction
from kora.db.types import SQLITE

TABLE_LAYOUT = text("PRAGMA main.table_list('graph_version')")
XINFO_SHADOW = text(
    "SELECT 1 FROM main.sqlite_schema "
    "WHERE name = 'pragma_table_xinfo' COLLATE NOCASE LIMIT 1"
)
COLUMN_LAYOUT = text(
    """
WITH expected(cid, name, declared_type) AS (
    VALUES (0, 'id', 'VARCHAR(26)'),
           (1, 'graph_id', 'VARCHAR(26)'),
           (2, 'number', 'INTEGER'),
           (3, 'ir', 'TEXT'),
           (4, 'ir_digest', 'VARCHAR(64)'),
           (5, 'source_digest', 'VARCHAR(64)'),
           (6, 'execution_contract', 'TEXT'),
           (7, 'execution_contract_digest', 'VARCHAR(64)'),
           (8, 'deployment_digest', 'VARCHAR(64)'),
           (9, 'resolved_digests', 'TEXT'),
           (10, 'resources', 'TEXT'),
           (11, 'deployed_by', 'VARCHAR(255)'),
           (12, 'deployed_at', 'VARCHAR(32)'),
           (13, 'status', 'VARCHAR(255)'),
           (14, 'created_at', 'VARCHAR(32)'),
           (15, 'updated_at', 'VARCHAR(32)'),
           (16, 'value_encoding_version', 'INTEGER')
)
SELECT count(*) = 17 AND count(expected.cid) = 17
FROM main.pragma_table_xinfo('graph_version', 'main') AS actual
LEFT JOIN expected
  ON actual.cid = expected.cid
 AND actual.name = expected.name COLLATE BINARY
 AND actual.type = expected.declared_type COLLATE BINARY
 AND actual."notnull" = (expected.cid != 16)
 AND actual.dflt_value IS NULL
 AND actual.pk = (expected.cid = 0)
 AND actual.hidden = 0
"""
)
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
        table = (await connection.execute(TABLE_LAYOUT)).one_or_none()
        if table is None or tuple(table) != (
            "main",
            "graph_version",
            "table",
            17,
            0,
            0,
        ):
            return None
        if (await connection.execute(XINFO_SHADOW)).scalar_one_or_none() is not None:
            return None
        verified: object = (await connection.execute(COLUMN_LAYOUT)).scalar_one()
        if type(verified) is not int or verified != 1:
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
