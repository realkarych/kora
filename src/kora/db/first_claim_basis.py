from dataclasses import dataclass
from typing import cast

from sqlalchemy import Boolean, case, insert, literal_column, select, text
from sqlalchemy.dialects.postgresql import dialect as postgres_dialect
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import CreateTable

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.deadline_discovery_v1 import PG_CANCEL
from kora.db.first_claim_basis_v1 import (
    FIRST_CLAIM_BASIS_SQLITE_BYTES,
    MAX_CONFIGURED_STALL_US,
    first_claim_basis_domain,
)
from kora.db.grant_history import row_fits
from kora.db.run_event_page_v1 import (
    RUN_EVENT_REVISION,
    sqlite_run_event_page_available,
)
from kora.db.tables import attempt_first_claim_basis as basis
from kora.db.tables import event
from kora.db.transactions import writer_transaction
from kora.db.types import POSTGRESQL, SQLITE
from kora.db.worker_claim_poll_v1 import CANDIDATE_INDEX, CANDIDATE_ROWS

SQLITE_DDL: str = str(CreateTable(basis).compile(dialect=sqlite_dialect())).strip()
MIGRATION_REVISION = {
    SQLITE: text("""
        SELECT CASE WHEN version_num IN ('0032', '0033', :run_events)
                    THEN version_num END FROM main.alembic_version LIMIT 2
        """),
    POSTGRESQL: text("""
        SELECT CASE WHEN version_num IN ('0032', '0033', :run_events)
                    THEN version_num END FROM alembic_version LIMIT 2
        """),
}
POSTGRES_EVENT_SUCCESSOR = text("""
    SELECT pg_catalog.to_regclass('custody_frontier') IS NULL
      AND native.relnamespace = basis.relnamespace AND native.relowner = basis.relowner
      AND native.relkind = 'r' AND native.relpersistence = 'p' AND native.relnatts = 11
      AND NOT native.relrowsecurity AND NOT native.relforcerowsecurity
      AND NOT native.relhasrules
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger
                        WHERE tgrelid = native.oid AND NOT tgisinternal)
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits
                        WHERE native.oid IN (inhparent, inhrelid))
        AND ARRAY(SELECT attname::text || ':' ||
                         pg_catalog.format_type(atttypid, atttypmod)
                  FROM pg_catalog.pg_attribute
                  WHERE attrelid = native.oid AND attnum > 0 AND NOT attisdropped
                    AND NOT atthasdef AND attidentity = '' AND attgenerated = ''
                    AND attnotnull = (attnum = 1 OR attnum >= 6)
                    AND attcollation = CASE WHEN attnum <= 6
                        THEN pg_catalog.to_regcollation('pg_catalog."C"')::oid
                        ELSE 0::oid END ORDER BY attnum) = CAST(:columns AS text[])
        AND ARRAY(SELECT CASE WHEN convalidated AND NOT condeferrable
                         AND (contype <> 'f' OR confrelid IN (
                             SELECT oid FROM pg_catalog.pg_class
                             WHERE relnamespace = native.relnamespace))
                         THEN conname::text || ':' ||
                              pg_catalog.pg_get_constraintdef(c.oid, true) END
                  FROM pg_catalog.pg_constraint AS c
                  WHERE conrelid = native.oid AND contype <> 'n' ORDER BY conname
        ) = CAST(:constraints AS text[])
        AND ARRAY(SELECT CASE WHEN indisvalid AND indisready AND indislive
                         THEN pg_catalog.pg_get_indexdef(indexrelid) END
                  FROM pg_catalog.pg_index WHERE indrelid = native.oid
                  ORDER BY indexrelid::regclass::text) = ARRAY[
                    pg_catalog.format('CREATE INDEX ix_event_run_id_id ON %I.event '
                        'USING btree (run_id, id)', ns.nspname),
                    pg_catalog.format('CREATE UNIQUE INDEX pk_event ON %I.event '
                        'USING btree (id)', ns.nspname)]
    FROM pg_catalog.pg_class AS native
    JOIN pg_catalog.pg_namespace AS ns ON ns.oid = native.relnamespace
    JOIN pg_catalog.pg_class AS basis
      ON basis.oid = pg_catalog.to_regclass('attempt_first_claim_basis')
    WHERE native.oid = pg_catalog.to_regclass('event')
    """)
SQLITE_SCHEMA = text("""
    SELECT count(*) = 0,
           count(*) = 1 AND
               coalesce(max(native.type = 'table' AND native.sql = :ddl), 0),
           EXISTS (
               SELECT 1 FROM sqlite_temp_schema
               WHERE name COLLATE NOCASE IN (
                   'alembic_version', 'attempt_first_claim_basis', 'worker'
               ) OR tbl_name = 'attempt_first_claim_basis' COLLATE NOCASE
                    AND type = 'trigger'
           )
    FROM main.sqlite_schema AS native
    WHERE native.name = 'attempt_first_claim_basis' COLLATE NOCASE
       OR native.tbl_name = 'attempt_first_claim_basis' COLLATE NOCASE
          AND native.type IN ('trigger', 'index') AND native.sql IS NOT NULL
    """)
SQLITE_PREDECESSOR = text("""
    SELECT count(*) = 2 AND EXISTS (
        SELECT 1 FROM main.sqlite_schema
        WHERE type = 'index' AND tbl_name = 'attempt' AND name = :index AND sql = :shape
    ) FROM main.pragma_table_xinfo('worker', 'main')
    WHERE NOT EXISTS (
        SELECT 1 FROM main.sqlite_schema
        WHERE name = 'pragma_table_xinfo' COLLATE NOCASE
    )
    AND hidden = 0 AND "notnull" = 0 AND (
        name = 'claim_cursor' AND type = 'VARCHAR(26)' AND dflt_value IS NULL
        OR name = 'claim_cursor_generation' AND type = 'BIGINT' AND dflt_value = '''0'''
    )
    """).bindparams(
    index=CANDIDATE_INDEX,
    shape=f"CREATE INDEX {CANDIDATE_INDEX} ON attempt (runtime, id) "
    f"WHERE {CANDIDATE_ROWS}",
)
POSTGRES_SCHEMA = text("""
    SELECT
        parent.oid IS NOT NULL AND native.relnamespace = parent.relnamespace
        AND native.relowner = parent.relowner AND native.relkind = 'r'
        AND native.relpersistence = 'p' AND native.relnatts = 11
        AND NOT native.relrowsecurity AND NOT native.relforcerowsecurity
        AND NOT native.relhasrules
        AND EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS rev
            WHERE rev.oid = pg_catalog.to_regclass('alembic_version')
              AND rev.relnamespace = native.relnamespace
              AND rev.relowner = native.relowner
              AND rev.relkind = 'r' AND rev.relpersistence = 'p'
        )
        AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_inherits
            WHERE native.oid IN (inhparent, inhrelid)
        )
        AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_trigger
            WHERE tgrelid = native.oid AND NOT tgisinternal
        )
        AND ARRAY(
            SELECT attname::text || ':' || pg_catalog.format_type(atttypid, atttypmod)
            FROM pg_catalog.pg_attribute
            WHERE attrelid = native.oid AND attnum > 0 AND NOT attisdropped
              AND NOT atthasdef AND attidentity = '' AND attgenerated = ''
              AND attnotnull = (attnum <= 3)
              AND attcollation = CASE WHEN attnum IN (1, 4, 6, 8, 10)
                    THEN pg_catalog.to_regcollation('pg_catalog."C"')::oid
                    ELSE 0::oid END
            ORDER BY attnum
        ) = CAST(:columns AS text[])
        AND ARRAY(
            SELECT CASE WHEN convalidated AND NOT condeferrable
                            AND (contype <> 'f' OR confrelid = parent.oid)
                        THEN conname::text || ':' ||
                            pg_catalog.pg_get_constraintdef(oid, true)
                   END
            FROM pg_catalog.pg_constraint
            WHERE conrelid = native.oid AND contype <> 'n'
            ORDER BY conname
        ) = CAST(:constraints AS text[])
        AND (
            SELECT count(*) = 1 AND bool_and(
                indisprimary AND indisunique AND indimmediate AND indisvalid
                AND indisready AND indislive AND indnatts = 1 AND indnkeyatts = 1
                AND indkey[0] = 1 AND indpred IS NULL AND indexprs IS NULL
                AND indcollation[0] = pg_catalog.to_regcollation('pg_catalog."C"')
            )
            FROM pg_catalog.pg_index WHERE indrelid = native.oid
        ) AS matched
    FROM pg_catalog.pg_class AS native
    LEFT JOIN pg_catalog.pg_class AS parent
      ON parent.oid = pg_catalog.to_regclass('attempt_dispatcher')
    WHERE native.oid = pg_catalog.to_regclass('attempt_first_claim_basis')
    """)
POSTGRES_COLUMNS: dict[str, tuple[str, ...]] = {
    table.name: tuple(
        f"{field.name}:{field_type.lower().replace('varchar', 'character varying')}"
        for field in table.c
        for field_type in (
            field.type.compile(dialect=postgres_dialect()).split(" COLLATE")[0],
        )
    )
    for table in (basis, event)
}
POSTGRES_EVENT_OBSERVATION: dict[str, tuple[str, ...]] = {
    "columns": POSTGRES_COLUMNS[event.name],
    "constraints": (
        "ck_event_namespace_scope:CHECK (namespace_id IS NOT NULL OR kind::text = "
        "'worker.registered'::text AND run_id IS NULL AND node_instance_id IS NULL "
        "AND attempt_id IS NULL)",
        "fk_event_attempt_id_attempt:FOREIGN KEY (attempt_id) REFERENCES attempt(id)",
        "fk_event_namespace_id_namespace:FOREIGN KEY (namespace_id) "
        "REFERENCES namespace(id)",
        "fk_event_node_instance_id_node_instance:FOREIGN KEY (node_instance_id) "
        "REFERENCES node_instance(id)",
        "fk_event_run_id_run:FOREIGN KEY (run_id) REFERENCES run(id)",
        "pk_event:PRIMARY KEY (id)",
    ),
}
POSTGRES_OBSERVATION: dict[str, str | tuple[str, ...]] = {
    "columns": POSTGRES_COLUMNS[basis.name],
    "constraints": (
        f"ck_first_claim_basis_domain:CHECK ({first_claim_basis_domain(POSTGRESQL)})",
        "fk_first_claim_dispatcher:FOREIGN KEY (dispatcher_id) "
        "REFERENCES attempt_dispatcher(id)",
        "pk_attempt_first_claim_basis:PRIMARY KEY (dispatcher_id)",
    ),
}
POSTGRES_PREDECESSOR = text("""
    SELECT count(*) = 2 FROM pg_catalog.pg_attribute AS attr
    JOIN pg_catalog.pg_class AS owner ON owner.oid = attr.attrelid
    JOIN pg_catalog.pg_class AS rev
      ON rev.oid = pg_catalog.to_regclass('alembic_version')
     AND rev.relnamespace = owner.relnamespace AND rev.relowner = owner.relowner
     AND rev.relkind = 'r' AND rev.relpersistence = 'p'
    LEFT JOIN pg_catalog.pg_attrdef AS def
      ON def.adrelid = attr.attrelid AND def.adnum = attr.attnum
    WHERE attr.attrelid = pg_catalog.to_regclass('worker')
      AND NOT attisdropped AND NOT attnotnull AND attnum > 0 AND (
        attname = 'claim_cursor' AND NOT atthasdef
            AND pg_catalog.format_type(atttypid, atttypmod) = 'character varying(26)'
            AND attcollation = pg_catalog.to_regcollation('pg_catalog."C"')
        OR attname = 'claim_cursor_generation'
            AND pg_catalog.format_type(atttypid, atttypmod) = 'bigint'
            AND pg_catalog.pg_get_expr(def.adbin, def.adrelid) = '''0''::bigint'
    )
    AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_index
        JOIN pg_catalog.pg_class AS target ON target.oid = indrelid
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = target.relnamespace
        WHERE indexrelid = pg_catalog.to_regclass(:index)
        AND indrelid = pg_catalog.to_regclass('attempt') AND ns.oid = owner.relnamespace
        AND indisvalid AND indisready AND indislive AND target.relpersistence = 'p'
        AND indcollation[0] = pg_catalog.to_regcollation('pg_catalog."C"')
        AND indcollation[1] = indcollation[0]
        AND pg_catalog.pg_get_indexdef(indexrelid) = pg_catalog.format(
            'CREATE INDEX %I ON %I.%I USING btree (runtime, id) WHERE %s',
            :index, ns.nspname, target.relname, CAST(:shape AS text))
    )
    """).bindparams(
    index=CANDIDATE_INDEX,
    shape=f"(({PG_CANCEL} OR (recovery_required IS TRUE)) IS TRUE)",
)


@dataclass(frozen=True, slots=True, repr=False)
class FirstClaimBasis:
    source_revision: int
    configured_stall_us: int | None
    renderer_behavior_sha256: bytes | None
    runtime_selection_id: str | None
    runtime_selection_revision: int | None
    vcs_selection_id: str | None
    vcs_selection_revision: int | None
    harness_selection_id: str | None
    harness_selection_revision: int | None


async def first_claim_basis_available(connection: AsyncConnection) -> bool:
    root = connection.get_transaction()
    backend = connection.dialect.name
    if (
        root is None
        or connection.in_nested_transaction()
        or backend not in (SQLITE, POSTGRESQL)
    ):
        raise ClaimSourcesRejected()
    revision = (
        await connection.scalars(
            MIGRATION_REVISION[backend], {"run_events": RUN_EVENT_REVISION}
        )
    ).all()
    if backend == SQLITE:
        absent, matched, shadow = (
            await connection.execute(SQLITE_SCHEMA, {"ddl": SQLITE_DDL})
        ).one()
        if shadow:
            raise ClaimSourcesRejected()
    else:
        observed = (
            await connection.execute(POSTGRES_SCHEMA, POSTGRES_OBSERVATION)
        ).one_or_none()
        absent = observed is None
        matched = observed is not None and observed.matched is True
    legacy = (
        revision == ["0032"]
        and absent
        and await connection.scalar(
            SQLITE_PREDECESSOR if backend == SQLITE else POSTGRES_PREDECESSOR
        )
        in (True, 1)
    )
    current = revision == ["0033"] and matched
    if revision == [RUN_EVENT_REVISION] and matched:
        current = (
            await sqlite_run_event_page_available(connection)
            if backend == SQLITE
            else bool(
                await connection.scalar(
                    POSTGRES_EVENT_SUCCESSOR, POSTGRES_EVENT_OBSERVATION
                )
            )
        )
    if (
        not root.is_active
        or connection.get_transaction() is not root
        or not (legacy or current)
    ):
        raise ClaimSourcesRejected()
    return bool(current)


async def read_first_claim_basis(
    connection: AsyncConnection, dispatcher_id: str
) -> FirstClaimBasis | None:
    backend = connection.dialect.name
    domain = literal_column(first_claim_basis_domain(backend), Boolean())
    if backend == SQLITE:
        domain = case(
            (row_fits(basis, FIRST_CLAIM_BASIS_SQLITE_BYTES), domain), else_=False
        )
    values = select(
        case((domain, True), else_=False).label("valid"),
        *(
            case((domain, basis.c[field])).label(field)
            for field in FirstClaimBasis.__dataclass_fields__
        ),
    ).where(basis.c.dispatcher_id == dispatcher_id)
    row = (await connection.execute(values)).mappings().one_or_none()
    if row is None:
        return None
    if row["valid"] is not True:
        raise ClaimSourcesRejected()
    stall = cast(str | None, row["configured_stall_us"])
    return FirstClaimBasis(
        cast(int, row["source_revision"]),
        int(stall) if stall is not None else None,
        cast(bytes | None, row["renderer_behavior_sha256"]),
        cast(str | None, row["runtime_selection_id"]),
        cast(int | None, row["runtime_selection_revision"]),
        cast(str | None, row["vcs_selection_id"]),
        cast(int | None, row["vcs_selection_revision"]),
        cast(str | None, row["harness_selection_id"]),
        cast(int | None, row["harness_selection_revision"]),
    )


async def insert_first_claim_basis(
    connection: AsyncConnection,
    dispatcher_id: str,
    source_revision: int,
    configured_stall_us: int | None,
) -> None:
    writer_transaction(connection).require_active()
    if configured_stall_us is not None and not (
        1 <= configured_stall_us <= MAX_CONFIGURED_STALL_US
    ):
        raise ClaimSourcesRejected()
    stall = None if configured_stall_us is None else str(configured_stall_us)
    companion = insert(basis).values(
        dispatcher_id=dispatcher_id,
        format_version=1,
        source_revision=source_revision,
        configured_stall_us=stall,
    )
    await connection.execute(companion)
