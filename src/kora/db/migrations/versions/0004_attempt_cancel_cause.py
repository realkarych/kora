from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, Column, Connection, inspect, text

from kora.db.types import NAME_LENGTH, collated_text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

CAUSE_CHECK: str = (
    "cancel_cause IS NULL OR cancel_cause IN "
    "('cancel', 'clear', 'supersede', 'timeout', 'stop')"
)
CONSTRAINT_NAME: str = "ck_attempt_cancel_cause"


def incompatible() -> RuntimeError:
    return RuntimeError("attempt cancellation migration found an incompatible schema")


def verify_column(connection: Connection) -> bool:
    found = next(
        (
            column
            for column in inspect(connection).get_columns("attempt")
            if column["name"] == "cancel_cause"
        ),
        None,
    )
    if found is None:
        return False
    expected = str(collated_text(NAME_LENGTH).compile(dialect=connection.dialect))
    if (
        str(found["type"].compile(dialect=connection.dialect)) != expected
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
            "AND t.relname = 'attempt' AND c.conname = :name"
        ),
        {"name": CONSTRAINT_NAME},
    ).first()
    return None if row is None else (str(row[0]), bool(row[1]))


def expected_constraint(connection: Connection) -> str:
    connection.exec_driver_sql(
        "CREATE TEMPORARY TABLE kora_0004_expected_check (LIKE attempt) ON COMMIT DROP"
    )
    connection.exec_driver_sql(
        "ALTER TABLE kora_0004_expected_check ADD CONSTRAINT expected "
        f"CHECK ({CAUSE_CHECK})"
    )
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid "
            "= 'pg_temp.kora_0004_expected_check'::regclass AND conname = 'expected'"
        )
    ).scalar_one()
    connection.exec_driver_sql("DROP TABLE kora_0004_expected_check")
    return str(definition)


def upgrade_postgresql(connection: Connection) -> None:
    if not verify_column(connection):
        op.add_column("attempt", Column("cancel_cause", collated_text(NAME_LENGTH)))
    expected = expected_constraint(connection)
    state = constraint_state(connection)
    if state is None:
        connection.exec_driver_sql(
            f"ALTER TABLE attempt ADD CONSTRAINT {CONSTRAINT_NAME} "
            f"CHECK ({CAUSE_CHECK}) NOT VALID"
        )
    elif state[0].removesuffix(" NOT VALID") != expected:
        raise incompatible()
    with op.get_context().autocommit_block():
        pass
    connection.exec_driver_sql(
        f"ALTER TABLE attempt VALIDATE CONSTRAINT {CONSTRAINT_NAME}"
    )
    if not verify_column(connection) or constraint_state(connection) != (
        expected,
        True,
    ):
        raise incompatible()


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.add_column(
            "attempt",
            Column(
                "cancel_cause",
                collated_text(NAME_LENGTH),
                CheckConstraint(CAUSE_CHECK, name=op.f(CONSTRAINT_NAME)),
            ),
        )
    else:
        upgrade_postgresql(connection)


def downgrade() -> None:
    raise RuntimeError(
        "attempt stop history cannot be downgraded; restore a compatible backup"
    )
