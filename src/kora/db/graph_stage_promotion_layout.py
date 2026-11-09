from enum import Enum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.types import POSTGRESQL, SQLITE

SQLITE_QUERY_ONLY: TextClause = text("PRAGMA query_only")
SQLITE_RELATIONS: TextClause = text(
    """
WITH expected(name, kind, owner) AS (
    VALUES ('graph_deployment_stage', 'table', 'graph_deployment_stage'),
           ('graph_deployment_stage_promotion_intent', 'table',
            'graph_deployment_stage_promotion_intent'),
           ('graph_deployment_stage_promotion_settlement', 'table',
            'graph_deployment_stage_promotion_settlement'),
           ('sqlite_autoindex_graph_deployment_stage_promotion_intent_1', 'index',
            'graph_deployment_stage_promotion_intent'),
           ('sqlite_autoindex_graph_deployment_stage_promotion_settlement_1', 'index',
            'graph_deployment_stage_promotion_settlement')
)
SELECT count(*) = 5 AND count(DISTINCT native.rootpage) = 5
       AND NOT EXISTS (
           SELECT 1 FROM main.sqlite_schema
           WHERE name COLLATE NOCASE IN (
               'pragma_table_xinfo', 'pragma_index_list', 'pragma_index_xinfo'
           )
       )
FROM expected
JOIN main.sqlite_schema AS native
  ON native.name = expected.name COLLATE BINARY
 AND native.type = expected.kind COLLATE BINARY
 AND native.tbl_name = expected.owner COLLATE BINARY
 AND native.rootpage > 0
"""
)
SQLITE_COLUMNS: TextClause = text(
    """
WITH relations(kind, name) AS (
    VALUES ('intent', 'graph_deployment_stage_promotion_intent'),
           ('settlement', 'graph_deployment_stage_promotion_settlement')
), expected(kind, position, name, declared_type) AS (
    VALUES ('intent', 0, 'stage_id', 'TEXT'),
           ('intent', 1, 'store_id', 'TEXT'),
           ('intent', 2, 'payload_digest', 'TEXT'),
           ('intent', 3, 'fence_incarnation', 'BIGINT'),
           ('intent', 4, 'operation_id', 'TEXT'),
           ('intent', 5, 'request_digest', 'TEXT'),
           ('intent', 6, 'content_generation', 'BIGINT'),
           ('intent', 7, 'request_bytes', 'BLOB'),
           ('settlement', 0, 'stage_id', 'TEXT'),
           ('settlement', 1, 'observation_bytes', 'BLOB')
)
SELECT count(*) = 10 AND count(expected.position) = 10
FROM relations
CROSS JOIN main.pragma_table_xinfo(relations.name, 'main') AS native
LEFT JOIN expected
  ON expected.kind = relations.kind COLLATE BINARY
 AND expected.position = native.cid
 AND expected.name = native.name COLLATE BINARY
 AND expected.declared_type = native.type COLLATE BINARY
 AND native."notnull" = 1
 AND native.dflt_value IS NULL
 AND native.pk = (native.cid = 0)
 AND native.hidden = 0
"""
)
SQLITE_KEYS: TextClause = text(
    """
WITH expected(name, index_name) AS (
    VALUES ('graph_deployment_stage_promotion_intent',
            'sqlite_autoindex_graph_deployment_stage_promotion_intent_1'),
           ('graph_deployment_stage_promotion_settlement',
            'sqlite_autoindex_graph_deployment_stage_promotion_settlement_1')
)
SELECT count(*) = 4 AND count(DISTINCT expected.name) = 2
   AND count(CASE WHEN indexed."desc" = 0 AND indexed.coll = 'BINARY' COLLATE BINARY
       AND (
           (indexed.seqno = 0 AND indexed.cid = 0 AND indexed.key = 1
            AND indexed.name = 'stage_id' COLLATE BINARY)
           OR
           (indexed.seqno = 1 AND indexed.cid = -1 AND indexed.key = 0
            AND indexed.name IS NULL)
       ) THEN 1 END) = 4
FROM expected
JOIN main.pragma_index_list(expected.name, 'main') AS native
  ON native.name = expected.index_name COLLATE BINARY
 AND native."unique" = 1
 AND native.origin = 'pk' COLLATE BINARY
 AND native.partial = 0
CROSS JOIN main.pragma_index_xinfo(expected.index_name, 'main') AS indexed
"""
)

POSTGRES_LAYOUT: TextClause = text(
    """
WITH relations(kind, name, key_name, column_count) AS (
    VALUES ('intent', 'graph_deployment_stage_promotion_intent',
            'pk_graph_deployment_stage_promotion_intent', 8),
           ('settlement', 'graph_deployment_stage_promotion_settlement',
            'pk_graph_deployment_stage_promotion_settlement', 2)
), expected(kind, position, name, declared_type) AS (
    VALUES ('intent', 1, 'stage_id', 'pg_catalog.text'),
           ('intent', 2, 'store_id', 'pg_catalog.text'),
           ('intent', 3, 'payload_digest', 'pg_catalog.text'),
           ('intent', 4, 'fence_incarnation', 'pg_catalog.int8'),
           ('intent', 5, 'operation_id', 'pg_catalog.text'),
           ('intent', 6, 'request_digest', 'pg_catalog.text'),
           ('intent', 7, 'content_generation', 'pg_catalog.int8'),
           ('intent', 8, 'request_bytes', 'pg_catalog.bytea'),
           ('settlement', 1, 'stage_id', 'pg_catalog.text'),
           ('settlement', 2, 'observation_bytes', 'pg_catalog.bytea')
), native AS (
    SELECT relations.kind, relations.key_name, source.oid, source.relnamespace
    FROM relations
    JOIN pg_catalog.pg_class AS source
      ON source.oid OPERATOR(pg_catalog.=)
         pg_catalog.to_regclass(relations.name::pg_catalog.text)
    JOIN pg_catalog.pg_class AS stage
      ON stage.oid OPERATOR(pg_catalog.=)
         pg_catalog.to_regclass('graph_deployment_stage'::pg_catalog.text)
     AND stage.relnamespace OPERATOR(pg_catalog.=) source.relnamespace
     AND stage.relowner OPERATOR(pg_catalog.=) source.relowner
     AND stage.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
     AND stage.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
     AND NOT stage.relrowsecurity AND NOT stage.relforcerowsecurity
    JOIN pg_catalog.pg_am AS heap
      ON heap.oid OPERATOR(pg_catalog.=) source.relam
     AND heap.oid OPERATOR(pg_catalog.=) stage.relam
     AND heap.amname OPERATOR(pg_catalog.=) 'heap'::pg_catalog.name
    WHERE source.relkind OPERATOR(pg_catalog.=) 'r'::pg_catalog."char"
      AND source.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
      AND source.relnatts OPERATOR(pg_catalog.=) relations.column_count
      AND NOT source.relispartition
      AND NOT source.relrowsecurity AND NOT source.relforcerowsecurity
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_inherits AS inheritance
          WHERE inheritance.inhparent OPERATOR(pg_catalog.=) source.oid
             OR inheritance.inhrelid OPERATOR(pg_catalog.=) source.oid
      )
)
SELECT pg_catalog.current_setting('transaction_isolation'::pg_catalog.text)
           OPERATOR(pg_catalog.=) 'repeatable read'::pg_catalog.text
   AND pg_catalog.current_setting('transaction_read_only'::pg_catalog.text)
           OPERATOR(pg_catalog.=) 'on'::pg_catalog.text
   AND (SELECT pg_catalog.count(*) OPERATOR(pg_catalog.=) 2 FROM native)
   AND (
       SELECT pg_catalog.count(*) OPERATOR(pg_catalog.=) 10
          AND pg_catalog.count(expected.position) OPERATOR(pg_catalog.=) 10
       FROM native
       JOIN pg_catalog.pg_attribute AS attribute
         ON attribute.attrelid OPERATOR(pg_catalog.=) native.oid
        AND attribute.attnum OPERATOR(pg_catalog.>) 0
       LEFT JOIN expected
         ON expected.kind OPERATOR(pg_catalog.=) native.kind
        AND attribute.attnum OPERATOR(pg_catalog.=) expected.position
        AND attribute.attname::pg_catalog.text OPERATOR(pg_catalog.=) expected.name
        AND attribute.atttypid OPERATOR(pg_catalog.=)
            pg_catalog.to_regtype(expected.declared_type)
        AND attribute.attcollation OPERATOR(pg_catalog.=) (
            CASE WHEN expected.declared_type
                OPERATOR(pg_catalog.=) 'pg_catalog.text'::pg_catalog.text
                THEN pg_catalog.to_regcollation('pg_catalog."C"')::pg_catalog.oid
                ELSE 0::pg_catalog.oid
            END
        )
        AND attribute.atttypmod OPERATOR(pg_catalog.=) -1
        AND attribute.attnotnull
        AND NOT attribute.attisdropped AND NOT attribute.atthasdef
        AND attribute.attidentity OPERATOR(pg_catalog.=) ''::pg_catalog."char"
        AND attribute.attgenerated OPERATOR(pg_catalog.=) ''::pg_catalog."char"
   )
   AND (
       SELECT pg_catalog.count(*) OPERATOR(pg_catalog.=) 2
          AND pg_catalog.count(DISTINCT native.oid) OPERATOR(pg_catalog.=) 2
       FROM native
       JOIN pg_catalog.pg_index AS key
         ON key.indrelid OPERATOR(pg_catalog.=) native.oid
       JOIN pg_catalog.pg_class AS index_relation
         ON index_relation.oid OPERATOR(pg_catalog.=) key.indexrelid
        AND index_relation.relname::pg_catalog.text
            OPERATOR(pg_catalog.=) native.key_name
        AND index_relation.relnamespace OPERATOR(pg_catalog.=) native.relnamespace
        AND index_relation.relkind OPERATOR(pg_catalog.=) 'i'::pg_catalog."char"
        AND index_relation.relpersistence OPERATOR(pg_catalog.=) 'p'::pg_catalog."char"
       JOIN pg_catalog.pg_am AS btree
         ON btree.oid OPERATOR(pg_catalog.=) index_relation.relam
        AND btree.amname OPERATOR(pg_catalog.=) 'btree'::pg_catalog.name
       JOIN pg_catalog.pg_opclass AS key_class
         ON key_class.oid OPERATOR(pg_catalog.=) key.indclass[0]
        AND key_class.opcmethod OPERATOR(pg_catalog.=) btree.oid
        AND key_class.opcname OPERATOR(pg_catalog.=) 'text_ops'::pg_catalog.name
        AND key_class.opcnamespace OPERATOR(pg_catalog.=)
            pg_catalog.to_regnamespace('pg_catalog')
       WHERE key.indisprimary AND key.indisunique AND key.indimmediate
         AND key.indisvalid AND key.indisready AND key.indislive
         AND NOT key.indcheckxmin
         AND key.indnatts OPERATOR(pg_catalog.=) 1
         AND key.indnkeyatts OPERATOR(pg_catalog.=) 1
         AND key.indkey[0] OPERATOR(pg_catalog.=) 1
         AND key.indcollation[0] OPERATOR(pg_catalog.=)
             pg_catalog.to_regcollation('pg_catalog."C"')
         AND key.indoption[0] OPERATOR(pg_catalog.=) 0
         AND key.indexprs IS NULL AND key.indpred IS NULL
   )
"""
)


class GraphStagePromotionLayout(Enum):
    VERIFIED = "verified"
    REFUSED = "refused"


async def _sqlite_layout(connection: AsyncConnection) -> bool:
    for statement in (SQLITE_QUERY_ONLY, SQLITE_RELATIONS, SQLITE_COLUMNS, SQLITE_KEYS):
        observed: object = await connection.scalar(statement)
        if type(observed) is not int or observed != 1:
            return False
    return True


async def observe_graph_stage_promotion_layout(
    connection: AsyncConnection,
) -> GraphStagePromotionLayout:
    transaction = connection.get_transaction()
    if (
        connection.closed
        or connection.invalidated
        or transaction is None
        or not transaction.is_active
        or connection.in_nested_transaction()
    ):
        return GraphStagePromotionLayout.REFUSED
    try:
        if connection.dialect.name == SQLITE:
            verified = await _sqlite_layout(connection)
        elif connection.dialect.name == POSTGRESQL:
            verified = await connection.scalar(POSTGRES_LAYOUT) is True
        else:
            return GraphStagePromotionLayout.REFUSED
    except SQLAlchemyError:
        return GraphStagePromotionLayout.REFUSED
    return (
        GraphStagePromotionLayout.VERIFIED
        if verified
        and transaction.is_active
        and connection.get_transaction() is transaction
        else GraphStagePromotionLayout.REFUSED
    )
