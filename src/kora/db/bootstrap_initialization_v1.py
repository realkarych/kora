from datetime import datetime

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer
from sqlalchemy.schema import conv

from kora.db.types import (
    IDENTIFIER_LENGTH,
    NAME_LENGTH,
    POSTGRESQL,
    SQLITE,
    UtcTimestamp,
    collated_text,
)

TABLE_NAME: str = "bootstrap_initialization"
Element = Column[int] | Column[str] | Column[datetime] | CheckConstraint


def bootstrap_initialization_elements_v1(
    dialect: str | None = None,
) -> tuple[Element, ...]:
    if dialect not in (None, POSTGRESQL, SQLITE):
        raise RuntimeError("unsupported bootstrap initialization schema backend")
    columns = (
        Column(
            "singleton",
            Integer,
            primary_key=True,
            nullable=False,
            autoincrement=False,
        ),
        Column(
            "namespace_id",
            collated_text(IDENTIFIER_LENGTH),
            ForeignKey("namespace.id"),
            nullable=False,
        ),
        Column("owner", collated_text(NAME_LENGTH), nullable=False),
        Column(
            "cli_token_id",
            collated_text(IDENTIFIER_LENGTH),
            ForeignKey("api_token.id"),
            nullable=False,
        ),
        Column(
            "mcp_token_id",
            collated_text(IDENTIFIER_LENGTH),
            ForeignKey("api_token.id"),
            nullable=False,
        ),
        Column("created_at", UtcTimestamp(), nullable=False),
        Column("updated_at", UtcTimestamp(), nullable=False),
    )
    checks = (
        CheckConstraint("singleton = 1", name=conv(f"ck_{TABLE_NAME}_singleton")),
        CheckConstraint(
            "cli_token_id != mcp_token_id",
            name=conv(f"ck_{TABLE_NAME}_distinct_tokens"),
        ),
    )
    if dialect in (None, SQLITE):
        checks = (
            *checks,
            CheckConstraint(
                "typeof(singleton) = 'integer'",
                name=conv(f"ck_{TABLE_NAME}_integer_storage"),
            ).ddl_if(dialect=SQLITE),
        )
    return (*columns, *checks)
