from typing import Never, Self

from sqlalchemy.sql.elements import TextClause

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    is_checked_graph_attachment,
)
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.engine.graph_publication_owner_sql import (
    OWNER_COUNT,
    OWNER_FIELDS,
    OWNER_VERSION,
    graph_owner_statements,
)

_OwnerScalar = str | int | None
_OWNER_STATEMENT_ISSUER = object()


class PreparedGraphOwnerStatements:
    __slots__ = ("scope_ids", "dialect", "count", "insert", "compare", "_parameters")
    scope_ids: tuple[str, str, str]
    dialect: Dialect
    count: int
    insert: TextClause | None
    compare: TextClause
    _parameters: tuple[tuple[str, _OwnerScalar], ...]

    def __new__(
        cls,
        issuer: object,
        scope_ids: tuple[str, str, str],
        dialect: Dialect,
        count: int,
        insert: TextClause | None,
        compare: TextClause,
        parameters: tuple[tuple[str, _OwnerScalar], ...],
    ) -> Self:
        if issuer is not _OWNER_STATEMENT_ISSUER:
            del issuer, scope_ids, dialect, count, insert, compare, parameters
            raise TypeError("graph owner statements require the checked producer")
        prepared = super().__new__(cls)
        object.__setattr__(prepared, "scope_ids", scope_ids)
        object.__setattr__(prepared, "dialect", dialect)
        object.__setattr__(prepared, "count", count)
        object.__setattr__(prepared, "insert", insert)
        object.__setattr__(prepared, "compare", compare)
        object.__setattr__(prepared, "_parameters", parameters)
        return prepared

    def __setattr__(self, name: str, value: object) -> Never:
        del self, name, value
        raise AttributeError("graph owner statements are immutable")

    def __delattr__(self, name: str) -> Never:
        del self, name
        raise AttributeError("graph owner statements are immutable")

    def __getstate__(self) -> Never:
        del self
        raise TypeError("graph owner statements require explicit access")

    def parameters(self) -> dict[str, _OwnerScalar]:
        return dict(self._parameters)


def _prepare(
    candidate: AttachedGraphDeployment,
    scope_ids: tuple[str, str, str],
    dialect: Dialect,
    max_bind_units: int,
) -> PreparedGraphOwnerStatements:
    if type(dialect) is not Dialect or not is_checked_graph_attachment(candidate):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    rows = candidate.owner_parameters(scope_ids, max_bind_units=max_bind_units)
    count = len(rows)
    insert, compare = graph_owner_statements(dialect, count)
    parameters: tuple[tuple[str, _OwnerScalar], ...] = ((OWNER_VERSION, scope_ids[2]),)
    if count:
        parameters = (
            *parameters,
            (OWNER_COUNT, count),
            *(
                (f"{field}_{index}", row[field])
                for index, row in enumerate(rows)
                for field in OWNER_FIELDS
            ),
        )
    return PreparedGraphOwnerStatements(
        _OWNER_STATEMENT_ISSUER,
        scope_ids,
        dialect,
        count,
        insert,
        compare,
        parameters,
    )


def prepare_graph_publication_owners(
    candidate: AttachedGraphDeployment,
    scope_ids: tuple[str, str, str],
    dialect: Dialect,
    *,
    max_bind_units: int,
) -> PreparedGraphOwnerStatements:
    try:
        return _prepare(candidate, scope_ids, dialect, max_bind_units)
    except GraphValueRejected as rejected:
        reason = rejected.reason
    del candidate, scope_ids, dialect, max_bind_units
    raise_safe(GraphValueRejected(reason))
