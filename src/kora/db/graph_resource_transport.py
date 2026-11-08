from sqlalchemy import LargeBinary, Text, func
from sqlalchemy import cast as sql_cast
from sqlalchemy.sql.elements import ColumnElement

from kora.db.connection import Dialect


def graph_resource_binary_projection(
    dialect: Dialect, value: ColumnElement[object] | ColumnElement[str]
) -> ColumnElement[bytes]:
    if dialect is Dialect.POSTGRESQL:
        return func.convert_to(
            sql_cast(value, Text()), func.getdatabaseencoding(), type_=LargeBinary()
        )
    if dialect is Dialect.SQLITE:
        return sql_cast(value, LargeBinary())
    del dialect, value
    raise ValueError("unsupported graph resource transport dialect")
