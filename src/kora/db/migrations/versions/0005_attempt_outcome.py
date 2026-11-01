from collections.abc import Sequence

from alembic import op
from sqlalchemy import CheckConstraint, Column, Connection, inspect, text

from kora.db.types import (
    NAME_LENGTH,
    JsonDocument,
    OutcomePayloadText,
    collated_text,
)

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

PRESENCE_CHECK: str = (
    "(outcome IS NULL AND outcome_payload IS NULL "
    "AND outcome_payload_state IS NULL) OR "
    "(outcome IS NOT NULL AND outcome_payload_state IS NOT NULL AND "
    "((outcome_payload_state = 'not_applicable' AND outcome_payload IS NULL) OR "
    "(outcome_payload_state IN ('available', 'expiring') "
    "AND outcome_payload IS NOT NULL) OR "
    "(outcome_payload_state IN ('expired', 'missing') "
    "AND outcome_payload IS NULL)))"
)
CONSTRAINT_NAME: str = "ck_attempt_outcome_payload_presence"


def incompatible() -> RuntimeError:
    return RuntimeError("attempt outcome migration found an incompatible schema")


def verify_columns(connection: Connection) -> set[str]:
    expected = {
        "outcome": str(JsonDocument().compile(dialect=connection.dialect)),
        "outcome_payload": str(
            OutcomePayloadText().compile(dialect=connection.dialect)
        ),
        "outcome_payload_state": str(
            collated_text(NAME_LENGTH).compile(dialect=connection.dialect)
        ),
    }
    found: set[str] = set()
    for column in inspect(connection).get_columns("attempt"):
        name = str(column["name"])
        if name not in expected:
            continue
        if (
            str(column["type"].compile(dialect=connection.dialect)) != expected[name]
            or not column["nullable"]
            or column.get("default") is not None
        ):
            raise incompatible()
        found.add(name)
    return found


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
        "CREATE TEMPORARY TABLE kora_0005_expected_check (LIKE attempt) ON COMMIT DROP"
    )
    connection.exec_driver_sql(
        "ALTER TABLE kora_0005_expected_check ADD CONSTRAINT expected "
        f"CHECK ({PRESENCE_CHECK})"
    )
    definition = connection.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid "
            "= 'pg_temp.kora_0005_expected_check'::regclass AND conname = 'expected'"
        )
    ).scalar_one()
    connection.exec_driver_sql("DROP TABLE kora_0005_expected_check")
    return str(definition)


def upgrade_postgresql(connection: Connection) -> None:
    found = verify_columns(connection)
    for column in (
        Column("outcome", JsonDocument()),
        Column("outcome_payload", OutcomePayloadText()),
        Column("outcome_payload_state", collated_text(NAME_LENGTH)),
    ):
        if column.name not in found:
            op.add_column("attempt", column)
    expected = expected_constraint(connection)
    state = constraint_state(connection)
    if state is None:
        connection.exec_driver_sql(
            f"ALTER TABLE attempt ADD CONSTRAINT {CONSTRAINT_NAME} "
            f"CHECK ({PRESENCE_CHECK}) NOT VALID"
        )
    elif state[0].removesuffix(" NOT VALID") != expected:
        raise incompatible()
    with op.get_context().autocommit_block():
        pass
    connection.exec_driver_sql(
        f"ALTER TABLE attempt VALIDATE CONSTRAINT {CONSTRAINT_NAME}"
    )
    if len(verify_columns(connection)) != 3 or constraint_state(connection) != (
        expected,
        True,
    ):
        raise incompatible()


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.add_column("attempt", Column("outcome", JsonDocument()))
        op.add_column("attempt", Column("outcome_payload", OutcomePayloadText()))
        op.add_column(
            "attempt",
            Column(
                "outcome_payload_state",
                collated_text(NAME_LENGTH),
                CheckConstraint(PRESENCE_CHECK, name=op.f(CONSTRAINT_NAME)),
            ),
        )
    else:
        upgrade_postgresql(connection)


def downgrade() -> None:
    raise RuntimeError("restore a compatible backup instead of downgrading")
