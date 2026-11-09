from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.types import IDENTIFIER_LENGTH, POSTGRESQL, SQLITE, CollatedText

TABLE_NAME: str = "run_inputs"
SchemaElement = Column[str] | Column[bytes] | Constraint


def run_input_elements_v1(dialect: str | None = None) -> tuple[SchemaElement, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported run input schema backend")
    binary_storage = (
        CheckConstraint(
            "typeof(cel_projection) = 'blob' AND typeof(params) = 'blob'",
            name=conv("ck_run_inputs_binary_storage"),
        ).ddl_if(dialect=SQLITE),
    )
    return (
        Column("run_id", CollatedText(IDENTIFIER_LENGTH), nullable=False),
        Column("cel_projection", LargeBinary(), nullable=False),
        Column("params", LargeBinary(), nullable=False),
        PrimaryKeyConstraint("run_id", name="pk_run_inputs"),
        ForeignKeyConstraint(("run_id",), ("run.id",), name="fk_run_inputs_run_id_run"),
        *(binary_storage if dialect in (None, SQLITE) else ()),
    )
