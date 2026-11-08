from sqlalchemy import BigInteger, CheckConstraint, Column, SmallInteger
from sqlalchemy.schema import conv

from kora.db.types import POSTGRESQL, SQLITE

TABLE_NAME: str = "graph_deployment_preparation_capacity"


def graph_preparation_capacity_elements_v1(
    dialect: str | None = None,
) -> tuple[Column[int] | CheckConstraint, ...]:
    if dialect not in (None, POSTGRESQL, SQLITE):
        raise RuntimeError("unsupported graph preparation capacity schema backend")
    columns = (
        Column(
            "id", SmallInteger, primary_key=True, nullable=False, autoincrement=False
        ),
        Column("occupied", BigInteger, nullable=False),
    )
    checks = (
        CheckConstraint("id = 1", name=conv(f"ck_{TABLE_NAME}_singleton")),
        CheckConstraint("occupied >= 0", name=conv(f"ck_{TABLE_NAME}_nonnegative")),
    )
    if dialect in (None, SQLITE):
        checks = (
            *checks,
            CheckConstraint(
                "typeof(id) = 'integer' AND typeof(occupied) = 'integer'",
                name=conv(f"ck_{TABLE_NAME}_integer_storage"),
            ).ddl_if(dialect=SQLITE),
        )
    return (*columns, *checks)
