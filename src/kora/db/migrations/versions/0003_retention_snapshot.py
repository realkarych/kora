from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, Column, Connection, Integer, inspect, text

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

RETENTION_CHECK: str = "retention_version IS NULL OR retention_version = 1"
CONSTRAINT_NAME: str = "ck_run_retention_version"


def incompatible() -> RuntimeError:
    return RuntimeError("retention migration found an incompatible schema object")


def verify_column(connection: Connection) -> bool:
    found = next(
        (
            column
            for column in inspect(connection).get_columns("run")
            if column["name"] == "retention_version"
        ),
        None,
    )
    if found is None:
        return False
    if (
        str(found["type"].compile(dialect=connection.dialect)) != "INTEGER"
        or not found["nullable"]
        or found.get("default") is not None
    ):
        raise incompatible()
    return True


def constraint_state(connection: Connection) -> tuple[str, bool] | None:
    row = connection.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid), c.convalidated FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid "
            "= t.relnamespace WHERE n.nspname = current_schema() "
            "AND t.relname = 'run' AND c.conname = :name"
        ),
        {"name": CONSTRAINT_NAME},
    ).first()
    return None if row is None else (str(row[0]), bool(row[1]))


def expected_constraint(connection: Connection) -> str:
    connection.exec_driver_sql(
        "CREATE TEMPORARY TABLE kora_0003_expected_check (LIKE run) ON COMMIT DROP"
    )
    connection.exec_driver_sql(
        "ALTER TABLE kora_0003_expected_check ADD CONSTRAINT expected "
        f"CHECK ({RETENTION_CHECK})"
    )
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid "
            "= 'pg_temp.kora_0003_expected_check'::regclass AND conname = 'expected'"
        )
    ).scalar_one()
    connection.exec_driver_sql("DROP TABLE kora_0003_expected_check")
    return str(definition)


def upgrade_postgresql(connection: Connection) -> None:
    if not verify_column(connection):
        op.add_column("run", Column("retention_version", Integer))
    expected = expected_constraint(connection)
    state = constraint_state(connection)
    if state is None:
        connection.exec_driver_sql(
            f"ALTER TABLE run ADD CONSTRAINT {CONSTRAINT_NAME} "
            f"CHECK ({RETENTION_CHECK}) NOT VALID"
        )
    elif state[0].removesuffix(" NOT VALID") != expected:
        raise incompatible()
    connection.exec_driver_sql(f"ALTER TABLE run VALIDATE CONSTRAINT {CONSTRAINT_NAME}")
    if constraint_state(connection) != (expected, True):
        raise incompatible()


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.add_column(
            "run",
            Column(
                "retention_version",
                Integer,
                CheckConstraint(RETENTION_CHECK, name=op.f(CONSTRAINT_NAME)),
            ),
        )
    else:
        upgrade_postgresql(connection)


def downgrade() -> None:
    raise RuntimeError(
        "retention history cannot be downgraded; restore a compatible backup"
    )
