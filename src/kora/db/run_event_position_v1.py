import sqlalchemy as sa
from sqlalchemy.schema import SchemaItem, conv

from kora.db.types import BYTE_ORDER_COLLATION, POSTGRESQL, SQLITE

POSITION_REVISION: str = "run_event_position_0035"
POSITION_TABLE: str = "run_event_position"
RUN_POSITION_STATE: str = "run_event_position_state"
POSITION_TRANSITION: str = "run_event_position_transition"
MAX_POSITION: int = (1 << 63) - 1


class RunEventPositionRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Run-event position state was not admitted.")


def _text(width: int) -> sa.String:
    return sa.String(width, collation="BINARY").with_variant(
        sa.String(width, collation=BYTE_ORDER_COLLATION), POSTGRESQL
    )


def _integer(column: str, name: str, dialect: str | None) -> tuple[SchemaItem, ...]:
    if dialect == POSTGRESQL:
        return ()
    return (
        sa.CheckConstraint(f"typeof({column}) = 'integer'", name=conv(name)).ddl_if(
            dialect=SQLITE
        ),
    )


def position_elements(dialect: str | None = None) -> tuple[SchemaItem, ...]:
    return (
        sa.Column("run_id", _text(26), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("event_id", _text(26), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "position", name="pk_run_event_position"),
        sa.UniqueConstraint("event_id", name="uq_run_event_position_event_id"),
        sa.ForeignKeyConstraint(("run_id",), ("run.id",), name="fk_revpos_run"),
        sa.ForeignKeyConstraint(("event_id",), ("event.id",), name="fk_revpos_event"),
        sa.CheckConstraint("position > 0", name=conv("ck_revpos_positive")),
        *_integer("position", "ck_revpos_integer", dialect),
    )


def position_state_elements(dialect: str | None = None) -> tuple[SchemaItem, ...]:
    return (
        sa.Column("run_id", _text(26), nullable=False),
        sa.Column("namespace_id", _text(26), nullable=False),
        sa.Column("high_water", sa.BigInteger(), nullable=False),
        sa.Column(
            "ready",
            sa.Boolean(create_constraint=True, name=conv("ck_revstate_ready")),
            nullable=False,
        ),
        sa.Column("legacy_event_id", _text(26)),
        sa.PrimaryKeyConstraint("run_id", name="pk_run_event_position_state"),
        sa.ForeignKeyConstraint(("run_id",), ("run.id",), name="fk_revstate_run"),
        sa.ForeignKeyConstraint(
            ("namespace_id",), ("namespace.id",), name="fk_revstate_namespace"
        ),
        sa.CheckConstraint("high_water >= 0", name=conv("ck_revstate_water")),
        *_integer("high_water", "ck_revstate_integer", dialect),
    )


def position_transition_elements() -> tuple[SchemaItem, ...]:
    return (
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("operation", _text(32)),
        sa.Column("proof", _text(64)),
        sa.Column("after_run_id", _text(26)),
        sa.Column(
            "ready",
            sa.Boolean(create_constraint=True, name=conv("ck_revtrans_ready")),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_event_position_transition"),
        sa.CheckConstraint("id = 1", name=conv("ck_revtrans_singleton")),
        sa.CheckConstraint(
            "(operation IS NULL) = (proof IS NULL)", name=conv("ck_revtrans_proof")
        ),
    )
