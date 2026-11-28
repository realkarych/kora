from hashlib import sha256

from sqlalchemy import Connection, text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.schema import CreateTable

from kora.db.tables import (
    run_event_position,
    run_event_position_state,
    run_event_position_transition,
)
from kora.db.types import POSTGRESQL, SQLITE

SQLITE_DDL: dict[str, str] = {
    table.name: str(CreateTable(table).compile(dialect=sqlite_dialect())).strip()
    for table in (
        run_event_position,
        run_event_position_state,
        run_event_position_transition,
    )
}
SQLITE_SHAPE = text("""
    SELECT count(*) = 1 AND coalesce(max(type = 'table' AND sql = :ddl), 0)
        AND NOT EXISTS (
            SELECT 1 FROM sqlite_temp_schema
            WHERE name COLLATE NOCASE IN (
                :table_name, 'run', 'event', 'namespace', 'alembic_version'
            ) OR tbl_name = :table_name COLLATE NOCASE AND type = 'trigger'
        )
    FROM main.sqlite_schema
    WHERE name = :table_name COLLATE NOCASE
       OR tbl_name = :table_name COLLATE NOCASE
          AND type IN ('trigger', 'index') AND sql IS NOT NULL
    """)
POSTGRES_SHAPE = text("""
    SELECT pg_catalog.sha256(pg_catalog.convert_to(pg_catalog.jsonb_build_array(
        ARRAY(SELECT pg_catalog.jsonb_build_array(
                  a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
                  a.attnotnull, a.attisdropped, a.attidentity, a.attgenerated,
                  cn.nspname, coll.collname, pg_catalog.pg_get_expr(d.adbin, d.adrelid))
              FROM pg_catalog.pg_attribute AS a
              LEFT JOIN pg_catalog.pg_attrdef AS d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = a.attcollation
              LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = coll.collnamespace
              WHERE a.attrelid = native.oid AND a.attnum > 0 ORDER BY a.attnum),
        ARRAY(SELECT CASE WHEN c.contype <> 'f' OR c.confrelid IN (
                  SELECT oid FROM pg_catalog.pg_class
                  WHERE relnamespace = native.relnamespace
                    AND relowner = native.relowner AND relkind = 'r')
                  THEN pg_catalog.jsonb_build_array(
                      c.conname, c.convalidated, c.condeferrable, c.condeferred,
                      pg_catalog.pg_get_constraintdef(c.oid, true)) END
              FROM pg_catalog.pg_constraint AS c
              WHERE c.conrelid = native.oid AND c.contype <> 'n'
              ORDER BY c.conname::text COLLATE pg_catalog."C"),
        ARRAY(SELECT CASE WHEN indisvalid AND indisready AND indislive
                  THEN pg_catalog.replace(
                      pg_catalog.pg_get_indexdef(indexrelid),
                      ' ON ' || pg_catalog.quote_ident(ns.nspname) || '.',
                      ' ON %schema%.')
                  END
              FROM pg_catalog.pg_index WHERE indrelid = native.oid
              ORDER BY indexrelid::pg_catalog.regclass::text COLLATE pg_catalog."C")
    )::text, 'UTF8'))
    FROM pg_catalog.pg_class AS native
    JOIN pg_catalog.pg_namespace AS ns ON ns.oid = native.relnamespace
    JOIN pg_catalog.pg_class AS owner ON owner.oid = pg_catalog.to_regclass('event')
    WHERE native.oid = pg_catalog.to_regclass(:table_name)
      AND pg_catalog.to_regclass('custody_frontier') IS NULL
      AND native.relnamespace = owner.relnamespace AND native.relowner = owner.relowner
      AND native.relkind = 'r' AND native.relpersistence = 'p'
      AND NOT native.relrowsecurity AND NOT native.relforcerowsecurity
      AND NOT native.relhasrules
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger
                      WHERE tgrelid = native.oid AND NOT tgisinternal)
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits
                      WHERE native.oid IN (inhparent, inhrelid))
    """)
POSTGRES_POSITION_TABLES = tuple(SQLITE_DDL)
POSTGRES_POSITION_SIGNATURE = (
    "4f86172e02b03626c48a4f4c1e6ec5ac5eca78a1fdef3e3661b1168ba0bec0a9"
)


def postgres_shape_fingerprint(
    connection: Connection, tables: tuple[str, ...]
) -> str | None:
    signature = sha256()
    for table in tables:
        digest: object = connection.scalar(POSTGRES_SHAPE, {"table_name": table})
        if type(digest) is not bytes or len(digest) != 32:
            return None
        signature.update(digest)
    return signature.hexdigest()


def position_schema_available(connection: Connection) -> bool:
    if connection.dialect.name == SQLITE:
        return all(
            connection.scalar(SQLITE_SHAPE, {"table_name": name, "ddl": ddl}) == 1
            for name, ddl in SQLITE_DDL.items()
        )
    return (
        connection.dialect.name == POSTGRESQL
        and postgres_shape_fingerprint(connection, POSTGRES_POSITION_TABLES)
        == POSTGRES_POSITION_SIGNATURE
    )
