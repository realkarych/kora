import re
from dataclasses import dataclass
from datetime import datetime
from typing import Never, Self

from sqlalchemy import BigInteger, LargeBinary, Select, Text, bindparam, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    is_checked_graph_attachment,
)
from kora.cel_eval.errors import raise_safe
from kora.db.connection import Dialect
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    quoted,
)
from kora.db.identifiers import is_identifier
from kora.db.types import DIGEST_LENGTH, normalise_timestamp
from kora.engine.graph_resource_references import GraphResourceReferences
from kora.engine.graph_version_identity import GraphVersionDigests

IR_PARAMETER = "publication_ir_json"
CONTRACT_PARAMETER = "publication_execution_json"
RESOURCE_PARAMETER = "publication_resources_json"
RESOLVED_DIGESTS_PARAMETER = "publication_resolved_digests_json"
DEPLOYED_BY_PARAMETER = "publication_deployed_by"
BINDING_SLOTS = 32
MAX_SQLITE_ORDINAL = (1 << 63) - 1
MAX_POSTGRESQL_ORDINAL = (1 << 31) - 1
SHA256_TEXT: re.Pattern[str] = re.compile(rf"[0-9a-f]{{{DIGEST_LENGTH}}}")
_BIND_ISSUER = object()


@dataclass(frozen=True, slots=True)
class GraphPublicationBindLimits:
    max_bind_bytes: int
    max_retained_bytes: int
    max_work: int


class PreparedGraphPublicationBinds:
    __slots__ = ("ir_json", "execution_json", "resources_json", "digests", "_scope")
    ir_json: str
    execution_json: str
    resources_json: str
    digests: GraphVersionDigests
    _scope: tuple[str, str, str]

    def __new__(
        cls,
        issuer: object,
        ir_json: str,
        execution_json: str,
        resources_json: str,
        digests: GraphVersionDigests,
        scope: tuple[str, str, str],
    ) -> Self:
        if issuer is not _BIND_ISSUER:
            del issuer, ir_json, execution_json, resources_json, digests, scope
            raise TypeError("graph publication binds require the checked producer")
        prepared = super().__new__(cls)
        object.__setattr__(prepared, "ir_json", ir_json)
        object.__setattr__(prepared, "execution_json", execution_json)
        object.__setattr__(prepared, "resources_json", resources_json)
        object.__setattr__(prepared, "digests", digests)
        object.__setattr__(prepared, "_scope", scope)
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

    def fixed_values(
        self, dialect: Dialect, *, number: int, source_digest: str, at: datetime
    ) -> dict[str, str | int | datetime]:
        stamp = None
        if type(at) is datetime:
            try:
                stamp = normalise_timestamp(at)
            except (TypeError, ValueError, OverflowError):
                pass
        ordinal_limit = (
            MAX_SQLITE_ORDINAL if dialect is Dialect.SQLITE else MAX_POSTGRESQL_ORDINAL
        )
        if (
            type(dialect) is not Dialect
            or type(number) is not int
            or not 0 < number <= ordinal_limit
            or stamp is None
            or stamp.year < 1000
            or not all(
                type(value) is str and is_identifier(value) for value in self._scope
            )
            or not all(
                type(value) is str and SHA256_TEXT.fullmatch(value) is not None
                for value in (
                    source_digest,
                    self.digests.ir_digest,
                    self.digests.execution_contract_digest,
                    self.digests.deployment_digest,
                )
            )
        ):
            del self, dialect, number, source_digest, at, stamp, ordinal_limit
            raise_safe(GraphValueRejected(GraphValueFailure.INVALID))
        _, graph_id, version_id = self._scope
        return {
            "id": version_id,
            "graph_id": graph_id,
            "number": number,
            "ir_digest": self.digests.ir_digest,
            "source_digest": source_digest,
            "execution_contract_digest": self.digests.execution_contract_digest,
            "deployment_digest": self.digests.deployment_digest,
            "deployed_at": stamp,
            "created_at": stamp,
            "updated_at": stamp,
            "status": "active",
            "value_encoding_version": 1,
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


def graph_publication_measurement_statement(
    dialect: Dialect,
) -> Select[int, int, int, int]:
    values = graph_publication_bind_expressions(dialect)
    ir = values["ir"]
    execution = values["execution_contract"]
    resources = values["resources"]
    transport = graph_resource_binary_projection(dialect, resources)
    if dialect is Dialect.POSTGRESQL:
        return select(
            func.pg_column_size(ir, type_=BigInteger()).label("ir_native_bytes"),
            func.pg_column_size(execution, type_=BigInteger()).label(
                "execution_native_bytes"
            ),
            func.pg_column_size(resources, type_=BigInteger()).label(
                "resources_native_bytes"
            ),
            func.octet_length(transport, type_=BigInteger()).label(
                "resource_transport_bytes"
            ),
        )
    return select(*_sqlite_publication_size_columns(ir, execution, transport))


def _sqlite_publication_size_columns(
    ir: ColumnElement[str] | ColumnElement[object],
    execution: ColumnElement[str] | ColumnElement[object],
    transport: ColumnElement[bytes],
) -> tuple[
    ColumnElement[int], ColumnElement[int], ColumnElement[int], ColumnElement[int]
]:
    sizes = select(
        func.length(sql_cast(ir, LargeBinary()), type_=BigInteger()).label(
            "ir_native_bytes"
        ),
        func.length(sql_cast(execution, LargeBinary()), type_=BigInteger()).label(
            "execution_native_bytes"
        ),
        func.length(transport, type_=BigInteger()).label("resources_native_bytes"),
    ).subquery()
    ir_width: ColumnElement[int] = sizes.c.ir_native_bytes
    execution_width: ColumnElement[int] = sizes.c.execution_native_bytes
    resource_width: ColumnElement[int] = sizes.c.resources_native_bytes
    return (
        ir_width,
        execution_width,
        resource_width,
        resource_width.label("resource_transport_bytes"),
    )


def sqlite_graph_publication_metadata_bind_expressions() -> dict[
    str, ColumnElement[str]
]:
    return {
        "resolved_digests": bindparam(
            RESOLVED_DIGESTS_PARAMETER, type_=Text(), required=True
        ),
        "deployed_by": bindparam(DEPLOYED_BY_PARAMETER, type_=Text(), required=True),
    }


def sqlite_graph_publication_record_measurement_statement() -> Select[
    int, int, int, int, int, int
]:
    values = graph_publication_bind_expressions(Dialect.SQLITE)
    metadata = sqlite_graph_publication_metadata_bind_expressions()
    transport = graph_resource_binary_projection(Dialect.SQLITE, values["resources"])
    return select(
        *_sqlite_publication_size_columns(
            values["ir"], values["execution_contract"], transport
        ),
        func.length(
            sql_cast(metadata["resolved_digests"], LargeBinary()), type_=BigInteger()
        ).label("resolved_digests_native_bytes"),
        func.length(
            sql_cast(metadata["deployed_by"], LargeBinary()), type_=BigInteger()
        ).label("deployed_by_native_bytes"),
    )


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
    scope = candidate.scope_ids
    scope_bytes = 4 * sum(map(len, scope))
    retained = (
        pair_bytes + resource_bytes + outer_pair_bound + BINDING_SLOTS + scope_bytes
    )
    work = (
        8 * pair_bytes
        + 2 * resource_bytes
        + 2 * outer_pair_bound
        + BINDING_SLOTS
        + scope_bytes
    )
    if retained > limits.max_retained_bytes or work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    current_scope = (
        candidate.scope.namespace_id,
        candidate.scope.graph_id,
        candidate.scope.version_id,
    )
    if any(type(value) is not str for value in current_scope) or current_scope != scope:
        raise GraphValueRejected(GraphValueFailure.INVALID)
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
        scope,
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
