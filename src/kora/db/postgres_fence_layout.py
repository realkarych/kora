from enum import Enum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.transactions import WriterTransactionError, writer_transaction
from kora.db.types import POSTGRESQL

POSTGRES_FENCE_LAYOUT = text(
    """
SELECT pg_catalog.current_setting('transaction_isolation'::pg_catalog.text)
           OPERATOR(pg_catalog.=) 'read committed'::pg_catalog.text
   AND pg_catalog.count(*) OPERATOR(pg_catalog.=) 1::pg_catalog.int8
   AND pg_catalog.bool_and(
       fence.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
       AND fence.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
       AND column_info.atttypid OPERATOR(pg_catalog.=)
           'pg_catalog.int8'::pg_catalog.regtype
       AND column_info.attidentity OPERATOR(pg_catalog.=) 'a'::pg_catalog."char"
       AND allocator.relkind OPERATOR(pg_catalog.=) 'S'::pg_catalog."char"
       AND allocator.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
       AND settings.seqtypid OPERATOR(pg_catalog.=)
           'pg_catalog.int8'::pg_catalog.regtype
       AND settings.seqstart OPERATOR(pg_catalog.=) 1::pg_catalog.int8
       AND settings.seqmin OPERATOR(pg_catalog.=) 1::pg_catalog.int8
       AND settings.seqmax OPERATOR(pg_catalog.=) 9223372036854775807::pg_catalog.int8
       AND settings.seqincrement OPERATOR(pg_catalog.=) 1::pg_catalog.int8
       AND NOT settings.seqcycle
   )
FROM pg_catalog.pg_class AS fence
JOIN pg_catalog.pg_attribute AS column_info
  ON column_info.attrelid OPERATOR(pg_catalog.=) fence.oid
 AND column_info.attname OPERATOR(pg_catalog.=) 'fence_incarnation'::pg_catalog.name
 AND column_info.attnum OPERATOR(pg_catalog.>) 0::pg_catalog.int2
 AND NOT column_info.attisdropped
JOIN pg_catalog.pg_depend AS dependency
  ON dependency.refclassid OPERATOR(pg_catalog.=)
    'pg_catalog.pg_class'::pg_catalog.regclass
 AND dependency.refobjid OPERATOR(pg_catalog.=) fence.oid
 AND dependency.refobjsubid OPERATOR(pg_catalog.=) column_info.attnum
 AND dependency.classid OPERATOR(pg_catalog.=)
    'pg_catalog.pg_class'::pg_catalog.regclass
 AND dependency.objsubid OPERATOR(pg_catalog.=) 0::pg_catalog.int4
 AND dependency.deptype OPERATOR(pg_catalog.=) 'i'::pg_catalog."char"
LEFT JOIN pg_catalog.pg_class AS allocator
  ON allocator.oid OPERATOR(pg_catalog.=) dependency.objid
LEFT JOIN pg_catalog.pg_sequence AS settings
  ON settings.seqrelid OPERATOR(pg_catalog.=) dependency.objid
WHERE fence.oid OPERATOR(pg_catalog.=)
      pg_catalog.to_regclass('storage_address_fence'::pg_catalog.text)
"""
)


class PostgresFenceLayout(Enum):
    VERIFIED = "verified"
    REFUSED = "refused"


async def check_postgres_fence_layout(
    connection: AsyncConnection,
) -> PostgresFenceLayout:
    try:
        writer = writer_transaction(connection)
        if connection.dialect.name != POSTGRESQL:
            return PostgresFenceLayout.REFUSED
        verified: object = await connection.scalar(POSTGRES_FENCE_LAYOUT)
        writer.require_active()
    except (WriterTransactionError, SQLAlchemyError):
        return PostgresFenceLayout.REFUSED
    return (
        PostgresFenceLayout.VERIFIED
        if verified is True
        else PostgresFenceLayout.REFUSED
    )
