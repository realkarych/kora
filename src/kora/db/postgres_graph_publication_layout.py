from enum import Enum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import postgresql_autocommit
from kora.db.maintenance import PostgreSQLDurabilityError, postgresql_transaction_active
from kora.db.transactions import WriterTransactionError, writer_transaction
from kora.db.types import POSTGRESQL

POSTGRES_GRAPH_PUBLICATION_LAYOUT = text(
    """
WITH expected(name) AS (
    VALUES ('graph'::pg_catalog.text), ('graph_version'),
           ('graph_version_read_admission'), ('graph_version_resource_owner'),
           ('graph_deployment_preparation'), ('idempotency_record'), ('event')
)
SELECT pg_catalog.current_setting('transaction_isolation'::pg_catalog.text)
           OPERATOR(pg_catalog.=) 'read committed'::pg_catalog.text
   AND pg_catalog.current_setting('transaction_read_only'::pg_catalog.text)
           OPERATOR(pg_catalog.=) 'off'::pg_catalog.text
   AND NOT EXISTS (
       SELECT 1
       FROM expected
       WHERE (
           SELECT relation.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
              AND relation.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
              AND method.amname OPERATOR(pg_catalog.=) 'heap'::pg_catalog.name
              AND (
                  SELECT 1 FROM pg_catalog.pg_inherits AS inheritance
                  WHERE inheritance.inhrelid OPERATOR(pg_catalog.=) relation.oid
                  LIMIT 1
              ) IS NULL
              AND (
                  SELECT 1 FROM pg_catalog.pg_inherits AS inheritance
                  WHERE inheritance.inhparent OPERATOR(pg_catalog.=) relation.oid
                  LIMIT 1
              ) IS NULL
           FROM pg_catalog.pg_class AS relation
           JOIN pg_catalog.pg_am AS method
             ON method.oid OPERATOR(pg_catalog.=) relation.relam
           WHERE relation.oid OPERATOR(pg_catalog.=)
                 pg_catalog.to_regclass(expected.name)
       ) IS NOT TRUE
   )
"""
)


class PostgresGraphPublicationLayout(Enum):
    VERIFIED = "verified"
    REFUSED = "refused"


async def check_postgres_graph_publication_layout(
    connection: AsyncConnection,
) -> PostgresGraphPublicationLayout:
    try:
        writer = writer_transaction(connection)
        native = connection.sync_connection
        if (
            connection.dialect.name != POSTGRESQL
            or native is None
            or postgresql_autocommit(native)
        ):
            return PostgresGraphPublicationLayout.REFUSED
        verified: object = await connection.scalar(POSTGRES_GRAPH_PUBLICATION_LAYOUT)
        writer.require_active()
        if verified is not True or not await connection.run_sync(
            postgresql_transaction_active
        ):
            return PostgresGraphPublicationLayout.REFUSED
        writer.require_active()
    except (WriterTransactionError, PostgreSQLDurabilityError, SQLAlchemyError):
        return PostgresGraphPublicationLayout.REFUSED
    return PostgresGraphPublicationLayout.VERIFIED
