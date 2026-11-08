from dataclasses import dataclass
from typing import Never, Self

from sqlalchemy import Text, bindparam
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    is_checked_graph_attachment,
)
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    quoted,
)
from kora.engine.graph_resource_references import GraphResourceReferences
from kora.engine.graph_version_identity import GraphVersionDigests

IR_PARAMETER = "publication_ir_json"
CONTRACT_PARAMETER = "publication_execution_json"
RESOURCE_PARAMETER = "publication_resources_json"
BINDING_SLOTS = 32
_BIND_ISSUER = object()


@dataclass(frozen=True, slots=True)
class GraphPublicationBindLimits:
    max_bind_bytes: int
    max_retained_bytes: int
    max_work: int


class PreparedGraphPublicationBinds:
    __slots__ = ("ir_json", "execution_json", "resources_json", "digests")
    ir_json: str
    execution_json: str
    resources_json: str
    digests: GraphVersionDigests

    def __new__(
        cls,
        issuer: object,
        ir_json: str,
        execution_json: str,
        resources_json: str,
        digests: GraphVersionDigests,
    ) -> Self:
        if issuer is not _BIND_ISSUER:
            del issuer, ir_json, execution_json, resources_json, digests
            raise TypeError("graph publication binds require the checked producer")
        prepared = super().__new__(cls)
        object.__setattr__(prepared, "ir_json", ir_json)
        object.__setattr__(prepared, "execution_json", execution_json)
        object.__setattr__(prepared, "resources_json", resources_json)
        object.__setattr__(prepared, "digests", digests)
        return prepared

    def __setattr__(self, name: str, value: object) -> Never:
        del self, name, value
        raise AttributeError("graph publication binds are immutable")

    def __delattr__(self, name: str) -> Never:
        del self, name
        raise AttributeError("graph publication binds are immutable")

    def __getstate__(self) -> Never:
        del self
        raise TypeError("graph publication binds require explicit access")

    def parameters(self) -> dict[str, str]:
        return {
            IR_PARAMETER: self.ir_json,
            CONTRACT_PARAMETER: self.execution_json,
            RESOURCE_PARAMETER: self.resources_json,
        }


def _bound_value(
    name: str, dialect: Dialect
) -> ColumnElement[str] | ColumnElement[object]:
    bound = bindparam(name, type_=Text(), required=True)
    if dialect is Dialect.POSTGRESQL:
        return sql_cast(sql_cast(bound, Text()), JSONB())
    return bound


def graph_publication_bind_expressions(
    dialect: Dialect,
) -> dict[str, ColumnElement[str] | ColumnElement[object]]:
    if type(dialect) is not Dialect:
        del dialect
        raise_safe(GraphValueRejected(GraphValueFailure.INVALID))
    return {
        "ir": _bound_value(IR_PARAMETER, dialect),
        "execution_contract": _bound_value(CONTRACT_PARAMETER, dialect),
        "resources": _bound_value(RESOURCE_PARAMETER, dialect),
    }


def _prepare_binds(
    candidate: AttachedGraphDeployment, limits: GraphPublicationBindLimits
) -> PreparedGraphPublicationBinds:
    if (
        not is_checked_graph_attachment(candidate)
        or type(candidate.resources) is not GraphResourceReferences
        or type(candidate.digests) is not GraphVersionDigests
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if type(limits) is not GraphPublicationBindLimits or any(
        type(value) is not int or value <= 0
        for value in (limits.max_bind_bytes, limits.max_retained_bytes, limits.max_work)
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    ir_text = candidate.canonical_ir
    execution_text = candidate.canonical_contract
    resources_text = candidate.resources.text
    if any(
        type(value) is not str or not value
        for value in (ir_text, execution_text, resources_text)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    pair_bytes = len(ir_text) + len(execution_text)
    resource_bytes = len(resources_text)
    outer_pair_bound = 2 * pair_bytes + 4
    retained = pair_bytes + resource_bytes + outer_pair_bound + BINDING_SLOTS
    work = 8 * pair_bytes + 2 * resource_bytes + 2 * outer_pair_bound + BINDING_SLOTS
    if retained > limits.max_retained_bytes or work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if any(
        not value.isascii() or not value.isprintable()
        for value in (ir_text, execution_text, resources_text)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    pair_transport = (
        pair_bytes
        + 4
        + sum(
            value.count('"') + value.count("\\") for value in (ir_text, execution_text)
        )
    )
    if pair_transport + resource_bytes > limits.max_bind_bytes:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    budget = TextBudget(pair_transport)
    return PreparedGraphPublicationBinds(
        _BIND_ISSUER,
        quoted(ir_text, budget),
        quoted(execution_text, budget),
        resources_text,
        candidate.digests,
    )


def prepare_graph_publication_binds(
    candidate: AttachedGraphDeployment, *, limits: GraphPublicationBindLimits
) -> PreparedGraphPublicationBinds:
    try:
        return _prepare_binds(candidate, limits)
    except GraphValueRejected as rejected:
        reason = rejected.reason
    del candidate, limits
    raise_safe(GraphValueRejected(reason))
