from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Integer,
    MetaData,
    Table,
    inspect,
    text,
)
from sqlalchemy.schema import CheckConstraint

from kora.db.types import JsonDocument, UsdAmount, UtcTimestamp, collated_text

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

PRICE_CHECK: str = (
    "(version IS NULL AND normalization_version IS NULL AND model IS NOT NULL) OR "
    "(version IS NOT NULL AND normalization_version IS NOT NULL AND "
    "version = 1 AND normalization_version = 1 AND model IS NULL)"
)
USAGE_CHECK: str = (
    "(normalization_version IS NULL AND reported_usd IS NULL AND "
    "known_token_fields IS NULL AND cache_creation_by_ttl IS NULL AND "
    "observation_id IS NULL AND observation_sequence IS NULL AND "
    "observed_at IS NULL AND dispatcher_id IS NULL) OR "
    "(normalization_version IS NOT NULL AND normalization_version = 1 AND "
    "known_token_fields IS NOT NULL AND observation_id IS NOT NULL AND "
    "observation_sequence IS NOT NULL AND observed_at IS NOT NULL AND "
    "observation_sequence >= 0 AND observation_sequence <= 9007199254740991 AND "
    "input_tokens >= 0 AND input_tokens <= 2147483647 AND "
    "cached_input_tokens >= 0 AND cached_input_tokens <= 2147483647 AND "
    "cache_creation_tokens >= 0 AND cache_creation_tokens <= 2147483647 AND "
    "output_tokens >= 0 AND output_tokens <= 2147483647 AND "
    "reasoning_tokens >= 0 AND reasoning_tokens <= 2147483647 AND "
    "reasoning_tokens <= output_tokens AND "
    "input_tokens = CAST(input_tokens AS BIGINT) AND "
    "cached_input_tokens = CAST(cached_input_tokens AS BIGINT) AND "
    "cache_creation_tokens = CAST(cache_creation_tokens AS BIGINT) AND "
    "output_tokens = CAST(output_tokens AS BIGINT) AND "
    "reasoning_tokens = CAST(reasoning_tokens AS BIGINT) AND "
    "observation_sequence = CAST(observation_sequence AS BIGINT))"
)
APPROVAL_CHECK: str = "run_cost_version IS NULL OR run_cost_version = 1"
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("price_table", "ck_price_table_revision", PRICE_CHECK),
    ("usage_record", "ck_usage_record_normalization", USAGE_CHECK),
    ("approval_request", "ck_approval_request_run_cost_version", APPROVAL_CHECK),
)
INDEXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "ix_price_table_default_effective",
        ("effective_from",),
        "version = 1 AND namespace_id IS NULL",
    ),
    (
        "ix_price_table_namespace_effective",
        ("namespace_id", "effective_from"),
        "version = 1 AND namespace_id IS NOT NULL",
    ),
)


def additions() -> dict[str, list[Column[object] | Column[int] | Column[str]]]:
    return {
        "price_table": [
            Column("version", Integer),
            Column("normalization_version", Integer),
        ],
        "usage_record": [
            Column("reported_usd", UsdAmount()),
            Column("known_token_fields", JsonDocument()),
            Column("cache_creation_by_ttl", JsonDocument()),
            Column("observation_id", collated_text(256)),
            Column("observation_sequence", BigInteger),
            Column("observed_at", UtcTimestamp()),
            Column("dispatcher_id", collated_text(26)),
            Column("normalization_version", Integer),
        ],
        "attempt": [Column("usage_coverage", JsonDocument())],
        "approval_request": [Column("run_cost_version", Integer)],
    }


def upgrade_sqlite(connection: Connection) -> None:
    columns = additions()
    for table_name in ("attempt", "approval_request"):
        for column in columns[table_name]:
            if table_name == "approval_request":
                column = Column(
                    "run_cost_version",
                    Integer,
                    CheckConstraint(
                        APPROVAL_CHECK,
                        name=op.f("ck_approval_request_run_cost_version"),
                    ),
                )
            op.add_column(table_name, column)
    for table_name in ("price_table", "usage_record"):
        predecessor = Table(
            table_name, MetaData(), autoload_with=connection, resolve_fks=False
        )
        with op.batch_alter_table(
            table_name, recreate="always", copy_from=predecessor
        ) as batch:
            for column in columns[table_name]:
                batch.add_column(column)
            if table_name == "price_table":
                batch.alter_column("model", nullable=True)
                batch.create_check_constraint(
                    op.f("ck_price_table_revision"), PRICE_CHECK
                )
            else:
                batch.alter_column("usd_estimate", nullable=True)
                batch.alter_column("model", type_=collated_text(256))
                batch.create_check_constraint(
                    op.f("ck_usage_record_normalization"), USAGE_CHECK
                )
    for index_name, keys, predicate in INDEXES:
        op.create_index(
            index_name,
            "price_table",
            list(keys),
            unique=True,
            sqlite_where=text(predicate),
        )
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("pricing migration foreign key verification failed")


def incompatible() -> RuntimeError:
    return RuntimeError("pricing migration found an incompatible schema object")


def verify_added_column(
    connection: Connection,
    table_name: str,
    expected: Column[object] | Column[int] | Column[str],
) -> bool:
    found = next(
        (
            column
            for column in inspect(connection).get_columns(table_name)
            if column["name"] == expected.name
        ),
        None,
    )
    if found is None:
        return False
    actual_type = str(found["type"].compile(dialect=connection.dialect))
    expected_type = str(expected.type.compile(dialect=connection.dialect))
    if (
        actual_type != expected_type
        or not found["nullable"]
        or found.get("default") is not None
    ):
        raise incompatible()
    return True


def constraint_state(
    connection: Connection, table_name: str, constraint_name: str
) -> tuple[str, bool] | None:
    row = connection.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid), c.convalidated FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid "
            "= t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = :table_name AND "
            "c.conname = :constraint_name"
        ),
        {"table_name": table_name, "constraint_name": constraint_name},
    ).first()
    return None if row is None else (str(row[0]), bool(row[1]))


def expected_constraint(
    connection: Connection, table_name: str, expression: str
) -> str:
    connection.exec_driver_sql(
        f"CREATE TEMPORARY TABLE kora_0002_expected_check (LIKE {table_name}) "
        "ON COMMIT DROP"
    )
    connection.exec_driver_sql(
        "ALTER TABLE kora_0002_expected_check ADD CONSTRAINT expected CHECK "
        f"({expression})"
    )
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid "
            "= 'pg_temp.kora_0002_expected_check'::regclass AND conname = "
            "'expected'"
        )
    ).scalar_one()
    connection.exec_driver_sql("DROP TABLE kora_0002_expected_check")
    return str(definition)


def checkpoint() -> None:
    with op.get_context().autocommit_block():
        pass


def prepare_postgresql(connection: Connection) -> None:
    for table_name, columns in additions().items():
        for column in columns:
            if not verify_added_column(connection, table_name, column):
                op.add_column(table_name, column)
    for table_name, column_name, expected_type in (
        ("price_table", "model", 'VARCHAR(255) COLLATE "C"'),
        ("usage_record", "usd_estimate", "NUMERIC(24, 10)"),
        ("usage_record", "model", 'VARCHAR(256) COLLATE "C"'),
    ):
        column = next(
            column
            for column in inspect(connection).get_columns(table_name)
            if column["name"] == column_name
        )
        if column.get("default") is not None:
            raise incompatible()
        actual = str(column["type"].compile(dialect=connection.dialect))
        if table_name == "usage_record" and column_name == "model":
            if (
                actual not in (expected_type, 'VARCHAR(255) COLLATE "C"')
                or column["nullable"]
            ):
                raise incompatible()
            if actual != expected_type:
                op.alter_column(table_name, column_name, type_=collated_text(256))
        else:
            if actual != expected_type:
                raise incompatible()
            if not column["nullable"]:
                op.alter_column(table_name, column_name, nullable=True)
    for table_name, constraint_name, expression in CHECKS:
        expected = expected_constraint(connection, table_name, expression)
        actual = constraint_state(connection, table_name, constraint_name)
        if actual is None:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({expression}) NOT VALID"
            )
        elif actual[0].removesuffix(" NOT VALID") != expected:
            raise incompatible()
    checkpoint()


def verify_index(
    connection: Connection, name: str, keys: tuple[str, ...], predicate: str
) -> bool | None:
    row = connection.execute(
        text(
            "SELECT pg_get_indexdef(c.oid), i.indisvalid FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_index i "
            "ON i.indexrelid = c.oid "
            "WHERE n.nspname = current_schema() AND c.relname = :name"
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    schema = str(connection.execute(text("SELECT current_schema()")).scalar_one())
    quoted_schema = connection.dialect.identifier_preparer.quote(schema)
    predicate_parts = predicate.split(" AND ")
    expected = (
        f"CREATE UNIQUE INDEX {name} ON {quoted_schema}.price_table "
        f"USING btree ({', '.join(keys)}) "
        f"WHERE (({predicate_parts[0]}) AND ({predicate_parts[1]}))"
    )
    if row[0] != expected:
        raise incompatible()
    return bool(row[1])


def upgrade_postgresql(connection: Connection) -> None:
    prepare_postgresql(connection)
    for table_name, constraint_name, _expression in CHECKS:
        state = constraint_state(connection, table_name, constraint_name)
        if state is None:
            raise incompatible()
        if not state[1]:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}"
            )
        checkpoint()
    for name, keys, predicate in INDEXES:
        state = verify_index(connection, name, keys, predicate)
        if state is not True:
            with op.get_context().autocommit_block():
                if state is False:
                    connection.exec_driver_sql(f"DROP INDEX CONCURRENTLY {name}")
                connection.exec_driver_sql(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {name} ON price_table "
                    f"({', '.join(keys)}) WHERE {predicate}"
                )
        if verify_index(connection, name, keys, predicate) is not True:
            raise incompatible()
    for table_name, constraint_name, expression in CHECKS:
        state = constraint_state(connection, table_name, constraint_name)
        if (
            state is None
            or not state[1]
            or state[0] != expected_constraint(connection, table_name, expression)
        ):
            raise incompatible()


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        upgrade_sqlite(connection)
    else:
        upgrade_postgresql(connection)


def downgrade() -> None:
    raise RuntimeError(
        "pricing history cannot be downgraded; restore a compatible backup"
    )
